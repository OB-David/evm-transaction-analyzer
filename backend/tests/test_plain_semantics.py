from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.plain_cfg_llm import (
    build_plain_cfg_block_context,
    build_plain_semantics_payload,
    write_plain_semantics_artifact,
)


class PlainSemanticsArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.steps = [
            {
                "address": "0xcontract",
                "RW_address": "0xcontract",
                "depth": 1,
                "pc": "0x0",
                "opcode": "PUSH1",
                "gascost": 3,
                "stack": [],
                "memory": [],
            },
            {
                "address": "0xcontract",
                "RW_address": "0xcontract",
                "depth": 1,
                "pc": "0x2",
                "opcode": "SLOAD",
                "gascost": 100,
                "stack": ["0x01"],
                "memory": [],
            },
            {
                "address": "0xcontract",
                "RW_address": "0xcontract",
                "depth": 1,
                "pc": "0x3",
                "opcode": "STOP",
                "gascost": 0,
                "stack": ["0x2a"],
                "memory": [],
            },
        ]
        self.blocks = {
            "7": {
                "block_id": 7,
                "start_step": 0,
                "end_step": 1,
            },
            "8": {
                "block_id": 8,
                "start_step": 2,
                "end_step": 2,
            },
        }

    def test_payload_is_indexed_by_plain_block(self) -> None:
        payload = build_plain_semantics_payload(self.steps, self.blocks)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["trace_step_count"], 3)
        self.assertEqual(list(payload["blocks"]), ["7", "8"])
        self.assertEqual([step["step_index"] for step in payload["blocks"]["7"]], [0, 1])
        self.assertEqual(
            payload["blocks"]["7"][1]["opcode_operands"],
            {"slot": "0x01"},
        )

    def test_gzip_writer_round_trips_without_full_trace_container(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "plain_semantics.json.gz"
            write_plain_semantics_artifact(str(output_path), self.steps, self.blocks)
            with gzip.open(output_path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)

        self.assertNotIn("steps", payload)
        self.assertEqual(payload["blocks"]["8"][0]["opcode"], "STOP")

    def test_block_context_loads_without_trace_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result_dir = Path(temp_dir)
            with (result_dir / "plain_blocks_information.json").open(
                "w", encoding="utf-8"
            ) as handle:
                json.dump(self.blocks, handle)
            write_plain_semantics_artifact(
                str(result_dir / "plain_semantics.json.gz"),
                self.steps,
                self.blocks,
            )

            with patch(
                "utils.plain_cfg_llm._resolve_result_dir",
                return_value=str(result_dir),
            ):
                context, metadata = build_plain_cfg_block_context("0xabc", 7)
            self.assertFalse((result_dir / "trace.json").exists())

        self.assertEqual(metadata["target_block_id"], 7)
        target = next(block for block in context["blocks"] if block["relation"] == "target")
        self.assertEqual([step["step_index"] for step in target["trace_steps"]], [0, 1])


if __name__ == "__main__":
    unittest.main()
