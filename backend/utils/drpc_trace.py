"""Fetch a Geth-compatible opcode trace from dRPC.

dRPC requires an explicit tracer for ``debug_traceTransaction``.  The custom
tracer below sends full stack data, but only sends memory when it may have
changed.  Python expands those sparse memory snapshots back into the
``structLogs`` layout consumed by :mod:`utils.evm_information`.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

import requests


logger = logging.getLogger(__name__)

DEFAULT_DRPC_RPC_URL = "https://lb.drpc.live/ethereum"
TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

# The tracer always replays the complete transaction, but only serializes the
# requested step window. Repeated memory states are represented by null and
# restored by _adapt_trace_result() after all windows have been stitched.
# Storage is emitted as a per-step delta only for SLOAD/SSTORE; it never copies
# Geth's cumulative storage map into every struct log.
JAVASCRIPT_TRACER = (
    "{data: [], faults: [], count: 0, start: 0, limit: 5000, prevOp: '',"
    "setup: function(config) {"
    "if (typeof config === 'string') config = JSON.parse(config);"
    "this.start = config.start || 0; this.limit = config.limit || 5000;"
    "},"
    "fault: function(log) {"
    "this.faults.push({pc: log.getPC(), error: String(log.getError())});"
    "}, step: function(log) {"
    "var stepIndex = this.count++;"
    "var op = log.op.toString();"
    "if (stepIndex < this.start || stepIndex >= this.start + this.limit) {"
    "this.prevOp = op; return;"
    "}"
    "var stack = [];"
    "for (var i = log.stack.length() - 1; i >= 0; i--) {"
    "stack.push('0x' + log.stack.peek(i).toString(16));"
    "}"
    "var memory = null;"
    "var memoryOps = '|MLOAD|MSTORE|MSTORE8|SHA3|KECCAK256|CALLDATACOPY|CODECOPY|"
    "EXTCODECOPY|RETURNDATACOPY|MCOPY|LOG0|LOG1|LOG2|LOG3|LOG4|CREATE|CREATE2|"
    "CALL|CALLCODE|DELEGATECALL|STATICCALL|RETURN|REVERT|';"
    "if (this.data.length === 0 || memoryOps.indexOf('|' + this.prevOp + '|') >= 0 || "
    "log.getDepth() !== this.data[this.data.length - 1].depth) {"
    "memory = toHex(log.memory.slice(0, log.memory.length()));"
    "}"
    "var storage = {};"
    "if ((op === 'SLOAD' || op === 'SSTORE') && log.stack.length() > 0) {"
    "var slot = '0x' + log.stack.peek(0).toString(16);"
    "if (op === 'SLOAD') { storage[slot] = null; }"
    "else if (log.stack.length() > 1) {"
    "storage[slot] = '0x' + log.stack.peek(1).toString(16);"
    "}"
    "}"
    "this.data.push({"
    "contextAddress: toHex(log.contract.getAddress()),"
    "pc: log.getPC(), op: op, gas: log.getGas(), gasCost: log.getCost(),"
    "depth: log.getDepth(), stack: stack, memory: memory, storage: storage"
    "});"
    "this.prevOp = op;"
    "}, result: function() { return {"
    "structLogs: this.data, faults: this.faults, totalSteps: this.count"
    "}; }}"
)

DEFAULT_CHUNK_SIZE = 5000
DEFAULT_TRACER_TIMEOUT = "10s"
FULL_TRACE_LIMIT = 100_000_000


class DrpcTraceError(RuntimeError):
    """A dRPC trace request or response was invalid."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _memory_words(value: str) -> list[str]:
    body = value.lower().removeprefix("0x")
    if not body:
        return []
    if any(character not in "0123456789abcdef" for character in body):
        raise DrpcTraceError("dRPC tracer returned non-hex memory")
    if len(body) % 64:
        body = body.ljust(((len(body) + 63) // 64) * 64, "0")
    return ["0x" + body[index : index + 64] for index in range(0, len(body), 64)]


def _adapt_trace_result(result: Any) -> dict[str, Any]:
    """Expand sparse dRPC memory into the Geth structLogs shape."""
    if not isinstance(result, dict):
        raise DrpcTraceError("dRPC trace result is not an object")

    faults = result.get("faults", [])
    if not isinstance(faults, list):
        raise DrpcTraceError("dRPC tracer returned invalid faults metadata")
    if faults:
        # A successful top-level transaction may contain a reverted child call
        # whose failure was intentionally caught by the calling contract.
        logger.debug("dRPC tracer observed EVM faults: %s", faults[:3])

    raw_logs = result.get("structLogs")
    total_steps = result.get("totalSteps")
    if not isinstance(raw_logs, list) or not raw_logs:
        raise DrpcTraceError("dRPC trace contains no structLogs")
    if not isinstance(total_steps, int) or total_steps != len(raw_logs):
        raise DrpcTraceError(
            f"dRPC trace step count mismatch: total={total_steps}, logs={len(raw_logs)}"
        )

    current_memory: list[str] = []
    struct_logs: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_logs):
        if not isinstance(raw, dict):
            raise DrpcTraceError(f"dRPC trace step {index} is not an object")

        sparse_memory = raw.get("memory")
        if isinstance(sparse_memory, str):
            current_memory = _memory_words(sparse_memory)
        elif sparse_memory is not None:
            raise DrpcTraceError(f"dRPC trace step {index} has invalid memory")

        stack = raw.get("stack")
        if not isinstance(stack, list):
            raise DrpcTraceError(f"dRPC trace step {index} has invalid stack")
        storage = raw.get("storage", {})
        if not isinstance(storage, dict):
            raise DrpcTraceError(f"dRPC trace step {index} has invalid storage delta")

        storage_delta = dict(storage)
        if step_opcode := str(raw.get("op", "")).upper():
            if step_opcode == "SLOAD" and index + 1 < len(raw_logs):
                next_raw = raw_logs[index + 1]
                next_stack = next_raw.get("stack") if isinstance(next_raw, dict) else None
                if isinstance(next_stack, list) and next_stack:
                    storage_delta = {
                        slot: next_stack[-1] if value is None else value
                        for slot, value in storage_delta.items()
                    }

        try:
            step = {
                "address": str(raw.get("contextAddress", "")),
                "pc": int(raw.get("pc", 0)),
                "op": step_opcode,
                "gas": int(raw.get("gas", 0)),
                "gasCost": int(raw.get("gasCost", 0)),
                "depth": int(raw.get("depth", 0)),
                "stack": list(stack),
                # The list is treated as immutable downstream. Reusing it for
                # unchanged steps avoids repeating the expansion work in RAM.
                "memory": current_memory,
                # This remains an incremental delta, not a cumulative map.
                "storage": storage_delta,
            }
        except (TypeError, ValueError) as exc:
            raise DrpcTraceError(
                f"dRPC trace step {index} has invalid numeric fields"
            ) from exc
        if not step["op"] or step["depth"] <= 0:
            raise DrpcTraceError(f"dRPC trace step {index} lacks op or depth")
        struct_logs.append(step)

    return {"structLogs": struct_logs}


def _rpc_call(
    session: requests.Session,
    rpc_url: str,
    api_key_header: str | None,
    tx_hash: str,
    timeout: float,
    start: int,
    limit: int,
    tracer_timeout: str,
    request_id: int,
) -> Any:
    headers = {"Content-Type": "application/json"}
    if api_key_header:
        headers["Drpc-Key"] = api_key_header
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "debug_traceTransaction",
        "params": [
            tx_hash,
            {
                "tracer": JAVASCRIPT_TRACER,
                "tracerConfig": {"start": start, "limit": limit},
                "timeout": tracer_timeout,
            },
        ],
    }

    try:
        response = session.post(rpc_url, headers=headers, json=payload, timeout=timeout)
    except requests.exceptions.ProxyError as exc:
        if not session.trust_env:
            raise DrpcTraceError(
                f"dRPC HTTP request failed ({type(exc).__name__})",
                retryable=True,
            ) from exc
        logger.warning("dRPC system proxy failed; retrying this request directly")
        session.trust_env = False
        try:
            response = session.post(
                rpc_url, headers=headers, json=payload, timeout=timeout
            )
        except requests.RequestException as direct_exc:
            raise DrpcTraceError(
                f"dRPC direct HTTP request failed ({type(direct_exc).__name__})",
                retryable=True,
            ) from direct_exc
    except requests.RequestException as exc:
        raise DrpcTraceError(
            f"dRPC HTTP request failed ({type(exc).__name__})", retryable=True
        ) from exc

    try:
        body = response.json()
    except ValueError as exc:
        preview = response.text[:200].replace("\n", " ")
        raise DrpcTraceError(
            f"dRPC HTTP {response.status_code} returned non-JSON data: {preview!r}",
            retryable=response.status_code >= 500,
        ) from exc

    error = body.get("error") if isinstance(body, dict) else body
    if error is None and response.status_code >= 400:
        error = body
    error_code = error.get("code") if isinstance(error, dict) else None
    error_message = (
        str(error.get("message", error)) if isinstance(error, dict) else str(error)
    )
    retryable = (
        response.status_code in {408, 429}
        or (
            response.status_code >= 500
            and (error_code == 19 or "temporary" in error_message.lower())
        )
        or error_code in {19, 30}
    )
    if response.status_code >= 400:
        raise DrpcTraceError(
            f"dRPC HTTP {response.status_code}: {error}", retryable=retryable
        )
    if not isinstance(body, dict):
        raise DrpcTraceError("dRPC JSON-RPC response is not an object")
    if "error" in body:
        raise DrpcTraceError(f"dRPC JSON-RPC error: {error}", retryable=retryable)
    if "result" not in body:
        raise DrpcTraceError("dRPC JSON-RPC response has no result")
    return body["result"]


def _validate_chunk_result(
    result: Any,
    *,
    start: int,
    limit: int,
    expected_total: int | None,
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(result, dict):
        raise DrpcTraceError("dRPC trace result is not an object")

    faults = result.get("faults", [])
    if not isinstance(faults, list):
        raise DrpcTraceError("dRPC tracer returned invalid faults metadata")
    if faults:
        logger.debug("dRPC trace chunk observed EVM faults: %s", faults[:3])

    logs = result.get("structLogs")
    total_steps = result.get("totalSteps")
    if not isinstance(logs, list) or not isinstance(total_steps, int):
        raise DrpcTraceError("dRPC trace chunk lacks structLogs or totalSteps")
    if total_steps <= 0:
        raise DrpcTraceError("dRPC trace contains no EVM steps")
    if expected_total is not None and total_steps != expected_total:
        raise DrpcTraceError(
            "dRPC trace step count changed between chunks: "
            f"{expected_total} != {total_steps}"
        )

    expected_count = min(limit, max(total_steps - start, 0))
    if len(logs) != expected_count:
        raise DrpcTraceError(
            f"dRPC trace chunk {start} is incomplete: "
            f"expected {expected_count}, got {len(logs)}"
        )
    return logs, total_steps


def _call_with_retries(
    session: requests.Session,
    rpc_url: str,
    api_key_header: str | None,
    tx_hash: str,
    timeout: float,
    *,
    start: int,
    limit: int,
    tracer_timeout: str,
    retries: int,
    retry_delay: float,
    request_id: int,
    label: str,
) -> tuple[Any, int]:
    for attempt in range(retries + 1):
        try:
            return (
                _rpc_call(
                    session,
                    rpc_url,
                    api_key_header,
                    tx_hash,
                    timeout,
                    start,
                    limit,
                    tracer_timeout,
                    request_id,
                ),
                request_id + 1,
            )
        except DrpcTraceError as exc:
            request_id += 1
            if not exc.retryable or attempt >= retries:
                raise
            wait_seconds = min(retry_delay * (attempt + 1), 3.0)
            logger.warning(
                "%s temporary failure; retrying in %s seconds (%d/%d): %s",
                label,
                f"{wait_seconds:g}",
                attempt + 1,
                retries,
                exc,
            )
            time.sleep(wait_seconds)


def fetch_drpc_trace(
    tx_hash: str,
    *,
    rpc_url: str | None = None,
    api_key: str | None = None,
    timeout: float = 30.0,
    retries: int = 8,
    retry_delay: float = 1.0,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_interval: float = 1.0,
    full_trace_retries: int = 2,
    tracer_timeout: str = DEFAULT_TRACER_TIMEOUT,
    prefer_chunks: bool = False,
) -> dict[str, Any]:
    """Fetch a dRPC trace, falling back to bounded result windows when needed."""
    if not TX_HASH_RE.fullmatch(tx_hash):
        raise DrpcTraceError("transaction hash must be 0x followed by 64 hex digits")
    if (
        timeout <= 0
        or retries < 0
        or retry_delay < 0
        or chunk_size <= 0
        or chunk_interval < 0
        or full_trace_retries < 0
    ):
        raise DrpcTraceError("invalid dRPC timeout or retry settings")

    configured_url = rpc_url or os.environ.get("DRPC_RPC_URL")
    configured_key = api_key or os.environ.get("DRPC_API_KEY")
    if configured_url:
        request_url = configured_url
        api_key_header = configured_key
    else:
        if not configured_key:
            raise DrpcTraceError("DRPC_API_KEY is not configured")
        request_url = f"{DEFAULT_DRPC_RPC_URL}/{configured_key}"
        api_key_header = None

    with requests.Session() as session:
        request_id = 1
        if prefer_chunks:
            logger.info(
                "dRPC chunk mode selected for large transaction %s (%d steps/chunk)",
                tx_hash,
                chunk_size,
            )
        else:
            try:
                result, request_id = _call_with_retries(
                    session,
                    request_url,
                    api_key_header,
                    tx_hash,
                    timeout,
                    start=0,
                    limit=FULL_TRACE_LIMIT,
                    tracer_timeout=tracer_timeout,
                    retries=min(retries, full_trace_retries),
                    retry_delay=retry_delay,
                    request_id=request_id,
                    label="dRPC full trace",
                )
                logs, total_steps = _validate_chunk_result(
                    result,
                    start=0,
                    limit=FULL_TRACE_LIMIT,
                    expected_total=None,
                )
                trace = _adapt_trace_result(
                    {"structLogs": logs, "faults": [], "totalSteps": total_steps}
                )
                logger.info(
                    "dRPC full trace succeeded for %s: %d steps",
                    tx_hash,
                    len(trace["structLogs"]),
                )
                return trace
            except DrpcTraceError as exc:
                if not exc.retryable:
                    raise
                logger.warning(
                    "dRPC full trace failed; switching to %d-step chunks: %s",
                    chunk_size,
                    exc,
                )

        all_logs: list[dict[str, Any]] = []
        offset = 0
        total_steps: int | None = None
        while total_steps is None or offset < total_steps:
            if offset:
                time.sleep(chunk_interval)
            result, request_id = _call_with_retries(
                session,
                request_url,
                api_key_header,
                tx_hash,
                timeout,
                start=offset,
                limit=chunk_size,
                tracer_timeout=tracer_timeout,
                retries=retries,
                retry_delay=retry_delay,
                request_id=request_id,
                label=f"dRPC trace chunk {offset}",
            )
            logs, total_steps = _validate_chunk_result(
                result,
                start=offset,
                limit=chunk_size,
                expected_total=total_steps,
            )
            all_logs.extend(logs)
            offset += len(logs)
            logger.info(
                "dRPC chunk progress for %s: %d/%d steps",
                tx_hash,
                offset,
                total_steps,
            )

        trace = _adapt_trace_result(
            {"structLogs": all_logs, "faults": [], "totalSteps": total_steps}
        )
        logger.info(
            "dRPC chunked trace succeeded for %s: %d steps",
            tx_hash,
            len(trace["structLogs"]),
        )
        return trace
