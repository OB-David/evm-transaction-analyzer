import gzip
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from utils import plain_cfg_llm


class CfgLlmTest(unittest.TestCase):
    def tearDown(self) -> None:
        plain_cfg_llm.clear_plain_cfg_runtime_cache("0x" + "1" * 64)

    def test_folded_context_combines_compact_plain_block_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as result_dir:
            folded_info = {
                "9": {
                    "block_id": 9,
                    "address": "0xabc",
                    "step_ranges": [
                        {"start_step": 10, "end_step": 12},
                        {"start_step": 20, "end_step": 21},
                    ],
                    "folded_blocks": [3, 4],
                    "instructions": ["('0x1', 'PUSH1')"],
                    "actions": [],
                }
            }
            with open(os.path.join(result_dir, "folded_blocks_information.json"), "w", encoding="utf-8") as handle:
                json.dump(folded_info, handle)
            with gzip.open(
                os.path.join(result_dir, plain_cfg_llm.PLAIN_SEMANTICS_FILENAME),
                "wt",
                encoding="utf-8",
            ) as handle:
                json.dump({"blocks": {"3": [{"opcode": "PUSH1"}], "4": [{"opcode": "SSTORE"}]}}, handle)

            with patch.object(plain_cfg_llm, "analysis_directory", return_value=result_dir):
                payload, meta = plain_cfg_llm.build_cfg_block_context("0x" + "1" * 64, 9, "folded")

        self.assertEqual(payload["mode"], "folded_cfg_opcode_focused")
        self.assertEqual(payload["blocks"][0]["trace_steps"], [{"opcode": "PUSH1"}, {"opcode": "SSTORE"}])
        self.assertEqual(meta["cfg_mode"], "folded")
        self.assertIsNone(meta["prev_block_id"])
        self.assertIsNone(meta["next_block_id"])

    def test_plain_and_folded_cache_entries_do_not_collide(self) -> None:
        tx_hash = "0x" + "1" * 64

        def context(_tx_hash: str, block_id: str | int, mode: str):
            return (
                {"mode": mode, "target_block_id": block_id},
                {"target_block_id": block_id, "cfg_mode": mode},
            )

        with (
            patch.object(plain_cfg_llm, "build_cfg_block_context", side_effect=context),
            patch.object(
                plain_cfg_llm,
                "_generate_analysis",
                side_effect=[
                    {"title": "Plain", "description": "One sentence. Two sentences."},
                    {"title": "Folded", "description": "One sentence. Two sentences."},
                ],
            ) as generate,
        ):
            plain_cfg_llm.analyze_cfg_block(tx_hash, 7, "plain")
            plain_cfg_llm.analyze_cfg_block(tx_hash, 7, "folded")
            cached = plain_cfg_llm.analyze_cfg_block(tx_hash, 7, "plain")

        self.assertEqual(generate.call_count, 2)
        self.assertEqual(cached["source"], "cache")
        self.assertEqual(cached["analysis"]["title"], "Plain")

    def test_deepseek_key_and_official_endpoint_are_used(self) -> None:
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"title":"Intent","description":"First sentence. Second sentence."}'))]
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value=response))))
        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True),
            patch.object(plain_cfg_llm, "OpenAI", return_value=client) as openai_client,
        ):
            result = plain_cfg_llm._generate_analysis("{}")

        self.assertEqual(result["title"], "Intent")
        self.assertEqual(openai_client.call_args.kwargs["api_key"], "test-key")
        self.assertEqual(openai_client.call_args.kwargs["base_url"], "https://api.deepseek.com")
        self.assertEqual(client.chat.completions.create.call_args.kwargs["model"], "deepseek-v4-flash")


if __name__ == "__main__":
    unittest.main()
