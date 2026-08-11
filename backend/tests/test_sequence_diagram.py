from __future__ import annotations

import unittest
import sqlite3
import tempfile
from pathlib import Path

from signature.store import initialize_database
from utils.sequence_diagram import (
    build_call_tree_payload,
    _extract_selector,
    _flatten_memory_words,
    build_refined_hierarchical_trace,
)


TRANSFER_CALLDATA = (
    "a9059cbb"
    "00000000000000000000000047c07b7dfd8e35bd00873d0c2231411504c0b3aa"
    "000000000000000000000000000000000000000000000000000000000000002a"
)


def _memory_words(*, prefixed: bool) -> list[str]:
    memory_hex = "00" * 64 + TRANSFER_CALLDATA.ljust(64 * 3, "0")
    words = [memory_hex[index:index + 64] for index in range(0, len(memory_hex), 64)]
    return [f"0x{word}" for word in words] if prefixed else words


def _call_stack(opcode: str) -> list[str]:
    if opcode in {"CALL", "CALLCODE"}:
        return ["0x0", "0x0", "0x44", "0x40", "0x0", "0x1234", "0xffff"]
    return ["0x0", "0x0", "0x44", "0x40", "0x1234", "0xffff"]


class SequenceDiagramCalldataTest(unittest.TestCase):
    def test_flatten_memory_words_removes_each_prefix(self) -> None:
        self.assertEqual(
            _flatten_memory_words(["0x1234", "0Xabcd", "5678"]),
            "1234abcd5678",
        )

    def test_transfer_selector_is_separate_from_argument_slices(self) -> None:
        for opcode in ("CALL", "CALLCODE", "DELEGATECALL", "STATICCALL"):
            for prefixed in (False, True):
                with self.subTest(opcode=opcode, prefixed=prefixed):
                    steps = [
                        {
                            "address": "0xparent",
                            "depth": 1,
                            "opcode": opcode,
                            "stack": _call_stack(opcode),
                            "memory": _memory_words(prefixed=prefixed),
                            "gascost": 0,
                        },
                        {
                            "address": "0xtoken",
                            "depth": 2,
                            "opcode": "CALLDATALOAD",
                            "stack": ["0x0"],
                            "memory": [],
                            "gascost": 0,
                        },
                        {
                            "address": "0xtoken",
                            "depth": 2,
                            "opcode": "CALLDATALOAD",
                            "stack": ["0x4"],
                            "memory": [],
                            "gascost": 0,
                        },
                        {
                            "address": "0xtoken",
                            "depth": 2,
                            "opcode": "CALLDATALOAD",
                            "stack": ["0x24"],
                            "memory": [],
                            "gascost": 0,
                        },
                        {
                            "address": "0xtoken",
                            "depth": 2,
                            "opcode": "STOP",
                            "stack": [],
                            "memory": [],
                            "gascost": 0,
                        },
                        {
                            "address": "0xparent",
                            "depth": 1,
                            "opcode": "STOP",
                            "stack": [],
                            "memory": [],
                            "gascost": 0,
                        },
                    ]

                    trace_tree = build_refined_hierarchical_trace(steps)
                    child = trace_tree["calls"][0]

                    self.assertEqual(child["calldata_selector"], "0xa9059cbb")
                    self.assertNotIn(
                        "0xa9059cbb",
                        [segment["val"] for segment in child["calldata_active_segments"]],
                    )
                    self.assertEqual(
                        [segment["offset"] for segment in child["calldata_active_segments"]],
                        [4, 36],
                    )
                    self.assertEqual(
                        [segment["val"] for segment in child["calldata_active_segments"]],
                        [
                            "0x00000000000000000000000047c07b7dfd8e35bd00873d0c2231411504c0b3aa",
                            "0x000000000000000000000000000000000000000000000000000000000000002a",
                        ],
                    )

    def test_call_tree_payload_persists_explicit_hierarchy_and_addresses(self) -> None:
        trace_tree = {
            "contract": "0xroot",
            "calls": [
                {
                    "contract": "0xchild",
                    "entry_step": 4,
                    "exit_step": 20,
                    "entry_op": "CALL",
                    "exit_op": "RETURN",
                    "calls": [
                        {
                            "contract": "0xgrandchild",
                            "entry_step": 8,
                            "exit_step": 12,
                            "entry_op": "DELEGATECALL",
                            "exit_op": "STOP",
                            "calls": [],
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "signatures.sqlite3"
            connection = sqlite3.connect(db_path)
            initialize_database(connection)
            connection.close()
            payload = build_call_tree_payload(
                trace_tree,
                {},
                {
                    "0xroot": "Root",
                    "0xchild": "Child",
                    "0xgrandchild": "Grandchild",
                },
                db_path,
            )

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["root"], {"address": "0xroot", "name": "Root"})
        self.assertEqual(len(payload["calls"]), 2)
        first, second = payload["calls"]
        self.assertIsNone(first["parent_call_id"])
        self.assertEqual(first["depth"], 1)
        self.assertEqual(first["from_address"], "0xroot")
        self.assertEqual(first["to_address"], "0xchild")
        self.assertEqual(second["parent_call_id"], first["call_id"])
        self.assertEqual(second["depth"], 2)
        self.assertEqual(second["from_address"], "0xchild")
        self.assertEqual(second["to_address"], "0xgrandchild")


if __name__ == "__main__":
    unittest.main()
