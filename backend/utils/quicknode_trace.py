"""Fetch a Geth-compatible opcode trace from QuickNode.

QuickNode's native struct logger provides opcode, stack, and memory data.  To
avoid the very large cumulative storage maps emitted by Geth, storage is
disabled in the RPC request and the SLOAD/SSTORE deltas consumed downstream
are reconstructed from the stable stack transitions.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

import requests


logger = logging.getLogger(__name__)

DEFAULT_QUICKNODE_RPC_URL = "https://docs-demo.quiknode.pro/"
DEFAULT_TIMEOUT_SECONDS = 600.0
TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


class QuicknodeTraceError(RuntimeError):
    """A QuickNode trace request or response was invalid."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _memory_words(value: str) -> list[str]:
    """Convert a contiguous hex memory snapshot into Geth-sized words."""
    body = value.lower().removeprefix("0x")
    if not body:
        return []
    if any(character not in "0123456789abcdef" for character in body):
        raise QuicknodeTraceError("trace returned non-hex memory")
    if len(body) % 64:
        body = body.ljust(((len(body) + 63) // 64) * 64, "0")
    return ["0x" + body[index : index + 64] for index in range(0, len(body), 64)]


def _resolve_rpc_url(
    rpc_url: str | None = None,
    api_key: str | None = None,
    endpoint_name: str | None = None,
) -> str:
    configured_url = (rpc_url or os.environ.get("QUICKNODE_RPC_URL") or "").strip()
    configured_key = (api_key or os.environ.get("QUICKNODE_API_KEY") or "").strip()
    configured_endpoint = (
        endpoint_name or os.environ.get("QUICKNODE_ENDPOINT_NAME") or ""
    ).strip()

    if configured_url.startswith(("https://", "http://")):
        return configured_url
    # Keep compatibility with setups that stored the full endpoint URL in the
    # old API-key variable.
    if configured_key.startswith(("https://", "http://")):
        return configured_key
    if configured_endpoint and configured_key and not configured_key.startswith("QN_"):
        endpoint_host = configured_endpoint.removesuffix(".quiknode.pro")
        return f"https://{endpoint_host}.quiknode.pro/{configured_key}/"

    if configured_key.startswith("QN_"):
        logger.warning(
            "QUICKNODE_API_KEY is a platform key, not an RPC endpoint token; "
            "using QuickNode's official docs demo endpoint. Configure "
            "QUICKNODE_RPC_URL for a private production endpoint."
        )
    elif configured_key:
        logger.warning(
            "QUICKNODE_API_KEY cannot form an RPC URL without "
            "QUICKNODE_ENDPOINT_NAME; using QuickNode's docs demo endpoint."
        )
    else:
        logger.warning(
            "QUICKNODE_RPC_URL is not configured; using QuickNode's official "
            "docs demo endpoint."
        )
    return DEFAULT_QUICKNODE_RPC_URL


def _add_storage_deltas(struct_logs: list[dict[str, Any]]) -> None:
    """Add the per-step storage shape consumed by the analysis pipeline."""
    for index, step in enumerate(struct_logs):
        stack = step.get("stack")
        memory = step.get("memory")
        if not isinstance(stack, list):
            raise QuicknodeTraceError(f"trace step {index} has invalid stack")
        if not isinstance(memory, list):
            raise QuicknodeTraceError(f"trace step {index} has invalid memory")

        storage_delta: dict[str, str] = {}
        opcode = str(step.get("op", "")).upper()
        if opcode == "SSTORE" and len(stack) >= 2:
            storage_delta[str(stack[-1])] = str(stack[-2])
        elif opcode == "SLOAD" and stack and index + 1 < len(struct_logs):
            next_stack = struct_logs[index + 1].get("stack")
            if isinstance(next_stack, list) and next_stack:
                storage_delta[str(stack[-1])] = str(next_stack[-1])
        step["storage"] = storage_delta


def _adapt_trace_result(result: Any) -> dict[str, Any]:
    """Validate QuickNode's result and preserve the downstream trace contract."""
    if not isinstance(result, dict):
        raise QuicknodeTraceError("trace result is not an object")
    struct_logs = result.get("structLogs")
    if not isinstance(struct_logs, list) or not struct_logs:
        raise QuicknodeTraceError("trace contains no structLogs")

    required_fields = {"pc", "op", "gas", "gasCost", "depth", "stack", "memory"}
    for index, step in enumerate(struct_logs):
        if not isinstance(step, dict):
            raise QuicknodeTraceError(f"trace step {index} is not an object")
        missing = required_fields.difference(step)
        if missing:
            raise QuicknodeTraceError(
                f"trace step {index} lacks fields: {', '.join(sorted(missing))}"
            )

    _add_storage_deltas(struct_logs)
    return result


def _rpc_call(
    session: requests.Session,
    rpc_url: str,
    tx_hash: str,
    timeout: float,
    request_id: int,
) -> Any:
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "debug_traceTransaction",
        "params": [
            tx_hash,
            {
                "enableMemory": True,
                "disableStack": False,
                "disableStorage": True,
                "enableReturnData": False,
            },
        ],
    }
    try:
        response = session.post(
            rpc_url,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise QuicknodeTraceError(
            f"QuickNode HTTP request failed ({type(exc).__name__})",
            retryable=True,
        ) from exc

    try:
        body = response.json()
    except ValueError as exc:
        preview = response.text[:200].replace("\n", " ")
        raise QuicknodeTraceError(
            f"QuickNode HTTP {response.status_code} returned non-JSON data: "
            f"{preview!r}",
            retryable=response.status_code >= 500,
        ) from exc

    error = body.get("error") if isinstance(body, dict) else body
    retryable = response.status_code in {408, 429} or response.status_code >= 500
    if response.status_code >= 400 or error is not None:
        raise QuicknodeTraceError(
            f"QuickNode trace request failed: HTTP {response.status_code}, {error}",
            retryable=retryable,
        )
    if not isinstance(body, dict) or "result" not in body:
        raise QuicknodeTraceError("QuickNode JSON-RPC response has no result")
    return body["result"]


def fetch_quicknode_trace(
    tx_hash: str,
    *,
    rpc_url: str | None = None,
    api_key: str | None = None,
    endpoint_name: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = 2,
    retry_delay: float = 1.0,
) -> dict[str, Any]:
    """Fetch one complete opcode trace in the existing ``structLogs`` shape."""
    if not TX_HASH_RE.fullmatch(tx_hash):
        raise QuicknodeTraceError(
            "transaction hash must be 0x followed by 64 hex digits"
        )
    if timeout <= 0 or retries < 0 or retry_delay < 0:
        raise QuicknodeTraceError("invalid QuickNode timeout or retry settings")

    request_url = _resolve_rpc_url(rpc_url, api_key, endpoint_name)
    with requests.Session() as session:
        for attempt in range(retries + 1):
            try:
                result = _rpc_call(
                    session,
                    request_url,
                    tx_hash,
                    timeout,
                    request_id=attempt + 1,
                )
                trace = _adapt_trace_result(result)
                logger.info(
                    "QuickNode opcode trace succeeded for %s: %d steps",
                    tx_hash,
                    len(trace["structLogs"]),
                )
                return trace
            except QuicknodeTraceError as exc:
                if not exc.retryable or attempt >= retries:
                    raise
                wait_seconds = min(retry_delay * (attempt + 1), 3.0)
                logger.warning(
                    "QuickNode trace temporary failure; retrying in %s seconds "
                    "(%d/%d): %s",
                    f"{wait_seconds:g}",
                    attempt + 1,
                    retries,
                    exc,
                )
                time.sleep(wait_seconds)

    raise AssertionError("unreachable QuickNode retry state")
