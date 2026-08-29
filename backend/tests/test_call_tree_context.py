import tempfile
import unittest
from pathlib import Path

from utils.call_tree import build_call_tree_payload, build_refined_hierarchical_trace


class CallTreeContextTests(unittest.TestCase):
    def test_root_transaction_selector_and_calldata_are_persisted(self):
        steps = [
            {
                "address": "0x00000000000000000000000000000000000000aa",
                "depth": 1,
                "opcode": "CALLDATALOAD",
                "stack": ["0x4"],
                "memory": [],
                "gascost": 3,
            },
            {
                "address": "0x00000000000000000000000000000000000000aa",
                "depth": 1,
                "opcode": "STOP",
                "stack": [],
                "memory": [],
                "gascost": 0,
            },
        ]
        calldata = "0xa9059cbb" + ("0" * 63) + "1" + ("0" * 63) + "2"
        trace_tree = build_refined_hierarchical_trace(steps, root_calldata=calldata)

        with tempfile.TemporaryDirectory() as temp_dir:
            payload = build_call_tree_payload(
                trace_tree,
                erc20_token_map={},
                full_address_name_map={},
                signature_db_path=Path(temp_dir) / "missing.sqlite3",
            )

        self.assertEqual(payload["root"]["selector"], "0xa9059cbb")
        self.assertEqual(payload["root"]["calldata"], ["0x" + ("0" * 63) + "1"])


if __name__ == "__main__":
    unittest.main()
