"""JSON-RPC client for Geth block-level call traces."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from .models import Trace


logger = logging.getLogger(__name__)


class GethRpcError(RuntimeError):
    pass


class GethRpcClient:
    def __init__(
        self,
        rpc_url: str,
        *,
        request_timeout: float = 300,
        tracer_timeout: str = "240s",
        max_attempts: int = 4,
        session: requests.Session | None = None,
    ):
        if not rpc_url:
            raise ValueError("rpc_url is required")
        self._rpc_url = rpc_url
        self.request_timeout = request_timeout
        self.tracer_timeout = tracer_timeout
        self.max_attempts = max(1, max_attempts)
        self.session = session or requests.Session()
        if session is None:
            # GETH_API normally points to a LAN endpoint. Do not route it
            # through the environment's public HTTP(S) proxy.
            self.session.trust_env = False
        self._request_id = 0

    def request(self, method: str, params: list[Any]) -> Any:
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        delay = 0.5
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.post(
                    self._rpc_url,
                    json=payload,
                    timeout=self.request_timeout,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {response.status_code}")
                response.raise_for_status()
                body = response.json()
            except (requests.RequestException, ValueError) as exc:
                if attempt == self.max_attempts:
                    raise GethRpcError(
                        f"{method} failed after {self.max_attempts} attempts: {exc}"
                    ) from exc
                logger.warning(
                    "%s attempt %d/%d failed; retrying in %.1fs",
                    method,
                    attempt,
                    self.max_attempts,
                    delay,
                )
                time.sleep(delay)
                delay = min(8.0, delay * 2)
                continue

            if body.get("error") is not None:
                error = body["error"]
                code = error.get("code") if isinstance(error, dict) else None
                message = error.get("message") if isinstance(error, dict) else str(error)
                raise GethRpcError(f"{method} RPC error {code}: {message}")
            if "result" not in body:
                raise GethRpcError(f"{method} response omitted result")
            return body["result"]

        raise AssertionError("unreachable")

    def chain_id(self) -> int:
        return int(self.request("eth_chainId", []), 16)

    def latest_block_number(self) -> int:
        return int(self.request("eth_blockNumber", []), 16)

    def trace_block(self, block_number: int) -> list[Trace]:
        result = self.request(
            "debug_traceBlockByNumber",
            [
                hex(block_number),
                {"tracer": "callTracer", "timeout": self.tracer_timeout},
            ],
        )
        if not isinstance(result, list):
            raise GethRpcError("debug_traceBlockByNumber returned a non-list result")

        traces: list[Trace] = []
        for transaction_position, transaction_trace in enumerate(result):
            if not isinstance(transaction_trace, dict):
                raise GethRpcError(
                    f"malformed trace at transaction position {transaction_position}"
                )
            if transaction_trace.get("error") is not None:
                raise GethRpcError(
                    f"transaction trace {transaction_position} failed: "
                    f"{transaction_trace['error']}"
                )
            transaction_hash = transaction_trace.get("txHash")
            root_frame = transaction_trace.get("result")
            if not isinstance(transaction_hash, str) or not isinstance(root_frame, dict):
                raise GethRpcError(
                    f"malformed trace at transaction position {transaction_position}"
                )
            traces.extend(
                _flatten_frame(
                    frame=root_frame,
                    block_number=block_number,
                    transaction_hash=transaction_hash.lower(),
                    transaction_position=transaction_position,
                    trace_address=[],
                )
            )
        return traces


def _flatten_frame(
    *,
    frame: dict[str, Any],
    block_number: int,
    transaction_hash: str,
    transaction_position: int,
    trace_address: list[int],
) -> list[Trace]:
    frame_type = str(frame.get("type", "CALL")).upper()
    if frame_type in {"CALL", "CALLCODE", "DELEGATECALL", "STATICCALL"}:
        trace_type = "call"
        call_type = frame_type.lower()
        from_address = _address(frame.get("from"))
        to_address = _address(frame.get("to"))
        gas = _quantity(frame.get("gas"))
        gas_used = _quantity(frame.get("gasUsed"))
        value = _quantity(frame.get("value"), default=0)
        input_data = str(frame.get("input", "0x")).lower()
    elif frame_type in {"CREATE", "CREATE2"}:
        trace_type = "create"
        call_type = frame_type.lower()
        from_address = _address(frame.get("from"))
        to_address = _address(frame.get("to"))
        gas = _quantity(frame.get("gas"))
        gas_used = _quantity(frame.get("gasUsed"))
        value = _quantity(frame.get("value"), default=0)
        input_data = str(frame.get("input", "0x")).lower()
    elif frame_type == "SELFDESTRUCT":
        trace_type = "suicide"
        call_type = None
        from_address = _address(frame.get("from"))
        to_address = _address(frame.get("to"))
        gas = None
        gas_used = None
        value = _quantity(frame.get("value"), default=0)
        input_data = "0x"
    else:
        raise GethRpcError(f"unsupported callTracer frame type: {frame_type}")

    trace = Trace(
        block_number=block_number,
        transaction_hash=transaction_hash,
        transaction_position=transaction_position,
        trace_address=list(trace_address),
        trace_type=trace_type,
        call_type=call_type,
        from_address=from_address,
        to_address=to_address,
        gas=gas,
        gas_used=gas_used,
        value=value,
        input=input_data,
        error=frame.get("error"),
    )
    flattened = [trace]
    child_frames = frame.get("calls") or []
    if not isinstance(child_frames, list):
        raise GethRpcError("callTracer frame calls field was not a list")
    for child_position, child_frame in enumerate(child_frames):
        if not isinstance(child_frame, dict):
            raise GethRpcError("callTracer child frame was not an object")
        flattened.extend(
            _flatten_frame(
                frame=child_frame,
                block_number=block_number,
                transaction_hash=transaction_hash,
                transaction_position=transaction_position,
                trace_address=[*trace_address, child_position],
            )
        )
    return flattened


def _address(value: Any) -> str | None:
    return str(value).lower() if value is not None else None


def _quantity(value: Any, *, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    return int(str(value), 16)
