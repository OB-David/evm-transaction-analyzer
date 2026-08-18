from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from utils.evm_information import (
    GETH_TRACE_START_BLOCK,
    TraceFetchError,
    TraceFormatter,
)
from utils.quicknode_trace import (
    DEFAULT_QUICKNODE_RPC_URL,
    _adapt_trace_result,
    _resolve_rpc_url,
)


def _step(op: str, stack: list[str]) -> dict:
    return {
        "pc": 0,
        "op": op,
        "gas": 100,
        "gasCost": 3,
        "depth": 1,
        "stack": stack,
        "memory": [],
    }


class QuicknodeTraceAdapterTests(unittest.TestCase):
    def test_preserves_struct_logs_and_adds_storage_deltas(self) -> None:
        trace = {
            "failed": False,
            "returnValue": "0x",
            "structLogs": [
                _step("SLOAD", ["0x01"]),
                _step("PUSH1", ["0xab"]),
                _step("SSTORE", ["0xcd", "0x02"]),
            ],
        }

        adapted = _adapt_trace_result(trace)

        self.assertIs(adapted, trace)
        self.assertEqual(adapted["structLogs"][0]["storage"], {"0x01": "0xab"})
        self.assertEqual(adapted["structLogs"][1]["storage"], {})
        self.assertEqual(adapted["structLogs"][2]["storage"], {"0x02": "0xcd"})

    def test_resolves_private_endpoint_from_name_and_token(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            url = _resolve_rpc_url(
                api_key="endpoint-token",
                endpoint_name="example-endpoint",
            )
        self.assertEqual(
            url,
            "https://example-endpoint.quiknode.pro/endpoint-token/",
        )

    def test_platform_key_falls_back_to_quicknode_docs_endpoint(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            url = _resolve_rpc_url(api_key="QN_platform-key")
        self.assertEqual(url, DEFAULT_QUICKNODE_RPC_URL)


class TraceProviderRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.formatter = object.__new__(TraceFormatter)
        self.trace = {"structLogs": [_step("STOP", [])]}

    def test_pre_boundary_transaction_uses_quicknode_directly(self) -> None:
        with (
            patch(
                "utils.evm_information.fetch_quicknode_trace",
                return_value=self.trace,
            ) as quicknode,
            patch.object(self.formatter, "_fetch_geth_trace") as geth,
        ):
            result = self.formatter._fetch_raw_trace(
                "0x" + "1" * 64,
                GETH_TRACE_START_BLOCK - 1,
            )

        self.assertIs(result, self.trace)
        quicknode.assert_called_once_with("0x" + "1" * 64)
        geth.assert_not_called()

    def test_post_boundary_geth_failure_falls_back_to_quicknode(self) -> None:
        with (
            patch.object(
                self.formatter,
                "_fetch_geth_trace",
                side_effect=TraceFetchError("geth unavailable"),
            ),
            patch(
                "utils.evm_information.fetch_quicknode_trace",
                return_value=self.trace,
            ) as quicknode,
        ):
            result = self.formatter._fetch_raw_trace(
                "0x" + "2" * 64,
                GETH_TRACE_START_BLOCK,
            )

        self.assertIs(result, self.trace)
        quicknode.assert_called_once_with("0x" + "2" * 64)


if __name__ == "__main__":
    unittest.main()
