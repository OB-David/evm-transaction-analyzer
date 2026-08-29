import copy
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from utils import plain_cfg_llm


def semantic_step(step: int, opcode: str, operands=None):
    return {
        "step_index": step,
        "address": "0x00000000000000000000000000000000000000aa",
        "rw_address": "0x00000000000000000000000000000000000000aa",
        "depth": 2,
        "pc": hex(step),
        "opcode": opcode,
        "gascost": 3,
        "stack": {
            "before_size": 2,
            "after_size": 1,
            "top_before": ["0x1", "0x2"],
            "top_after": ["0x3"],
            "after_observed": True,
            "delta": -1,
            "popped_count": 1,
            "pushed_values": [],
            "focus_operands_from_top": operands or {},
        },
        **({"opcode_operands": operands} if operands else {}),
    }


class PlainCfgContextTests(unittest.TestCase):
    def test_context_uses_target_trace_boundary_neighbors_and_active_call(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result_dir = Path(temp_dir)
            blocks = {
                "1": {
                    "block_id": 1,
                    "address": "0x00000000000000000000000000000000000000aa",
                    "start_step": 18,
                    "end_step": 19,
                    "actions": [],
                },
                "2": {
                    "block_id": 2,
                    "address": "0x00000000000000000000000000000000000000aa",
                    "start_step": 20,
                    "end_step": 21,
                    "actions": [],
                },
                "3": {
                    "block_id": 3,
                    "address": "0x00000000000000000000000000000000000000aa",
                    "start_step": 22,
                    "end_step": 23,
                    "actions": [],
                },
            }
            (result_dir / "plain_blocks_information.json").write_text(
                json.dumps(blocks), encoding="utf-8"
            )
            semantics = {
                "schema_version": 2,
                "trace_step_count": 24,
                "blocks": {
                    "1": [semantic_step(18, "PUSH1"), semantic_step(19, "JUMP")],
                    "2": [
                        semantic_step(20, "SLOAD", {"slot": "0x7"}),
                        semantic_step(21, "SSTORE", {"slot": "0x7", "value": "0x9"}),
                    ],
                    "3": [semantic_step(22, "JUMPDEST"), semantic_step(23, "STOP")],
                },
            }
            with gzip.open(result_dir / plain_cfg_llm.PLAIN_SEMANTICS_FILENAME, "wt") as handle:
                json.dump(semantics, handle)
            call_tree = {
                "schema_version": 1,
                "root": {"address": "0x01", "name": "root"},
                "calls": [
                    {
                        "call_id": 1,
                        "parent_call_id": None,
                        "depth": 1,
                        "entry_step": 9,
                        "exit_step": 30,
                        "entry_op": "CALL",
                        "exit_op": "RETURN",
                        "from_address": "0x01",
                        "to_address": "0x00000000000000000000000000000000000000aa",
                        "from_name": "root",
                        "to_name": "token",
                        "selector": "0xa9059cbb",
                        "probable_text_signatures": ["transfer(address,uint256)"],
                        "calldata": ["0x01", "0x02"],
                    }
                ],
            }
            (result_dir / "call_tree.json").write_text(json.dumps(call_tree), encoding="utf-8")

            with mock.patch.object(plain_cfg_llm, "analysis_directory", return_value=result_dir):
                payload, meta = plain_cfg_llm.build_plain_cfg_block_context("0x" + "1" * 64, 2)

            self.assertEqual([item["step"] for item in payload["target"]["execution_trace"]], [20, 21])
            self.assertEqual(
                [item["step"] for item in payload["control_context"]["predecessor_tail"]["execution_trace"]],
                [18, 19],
            )
            self.assertEqual(
                [item["step"] for item in payload["control_context"]["successor_head"]["execution_trace"]],
                [22, 23],
            )
            self.assertEqual(meta["active_call_selector"], "0xa9059cbb")
            self.assertIn("transfer(address,uint256)", meta["signature_candidates"])
            storage_facts = payload["target"]["effect_and_dependency_facts"]["storage"]
            self.assertEqual([item["op"] for item in storage_facts], ["SLOAD", "SSTORE"])


class ReconstructionValidationTests(unittest.TestCase):
    def setUp(self):
        self.context = {
            "target": {
                "step_count": 2,
                "opcode_histogram": {"SSTORE": 1, "RETURN": 1},
                "execution_trace": [
                    {"step": 10, "pc": "0xa", "op": "SSTORE"},
                    {"step": 11, "pc": "0xb", "op": "RETURN"},
                ],
            },
            "call_context": {"active_frame": {"signature_candidates": []}},
        }

    def test_accepts_grounded_statement_block(self):
        result = {
            "solidity": "{\n    assembly { sstore(0, 1) return(0, 0) }\n}",
            "confidence": "medium",
            "unresolved": ["The original storage variable name is unavailable."],
            "evidence": [
                {
                    "step_start": 10,
                    "step_end": 11,
                    "opcodes": ["SSTORE", "RETURN"],
                    "code": "sstore(0, 1) return(0, 0)",
                }
            ],
        }
        self.assertEqual(
            plain_cfg_llm._validate_generated_reconstruction(result, self.context),
            [],
        )

    def test_rejects_unobserved_or_uncovered_effects(self):
        result = {
            "solidity": "{ emit Invented(); }",
            "confidence": "high",
            "unresolved": [],
            "evidence": [
                {
                    "step_start": 10,
                    "step_end": 10,
                    "opcodes": ["LOG1"],
                    "code": "emit Invented();",
                }
            ],
        }
        errors = plain_cfg_llm._validate_generated_reconstruction(result, self.context)
        self.assertTrue(any("absent from range" in error for error in errors))
        self.assertTrue(any("LOG1 target steps=none in target" in error for error in errors))
        self.assertTrue(any("SSTORE" in error and "coverage" in error for error in errors))
        self.assertTrue(any("no LOG opcode" in error for error in errors))

    def test_rejects_contract_or_function_wrapper(self):
        errors = plain_cfg_llm._validate_solidity_statement_block(
            "contract WholeTransaction { function run() external {} }"
        )
        self.assertTrue(any("statement block" in error for error in errors))
        self.assertTrue(any("cannot contain contract" in error for error in errors))

    def test_normalizes_provable_chat_schema_deviations(self):
        context = {
            "target": {
                "step_count": 2,
                "opcode_histogram": {"MSTORE": 1, "SSTORE": 1},
                "execution_trace": [
                    {"step": 10, "pc": "0xa", "op": "MSTORE"},
                    {"step": 11, "pc": "0xb", "op": "SSTORE"},
                ],
            },
            "call_context": {"active_frame": {"signature_candidates": []}},
        }
        parsed = {
            "solidity": "{ uint256 value; assembly { mstore(0, 1) sstore(0, 1) } }",
            "confidence": "Medium confidence",
            "unresolved": "The original variable name is unavailable.",
            "evidence": [
                {
                    "step_start": 10,
                    "step_end": 10,
                    "opcodes": [],
                    "code": "mstore(0, 1)",
                },
                {
                    "step_start": 10,
                    "step_end": 10,
                    "opcodes": [],
                    "code": "uint256 value;",
                },
                {
                    "step_start": 11,
                    "step_end": 11,
                    "opcodes": "sstore",
                    "code": "sstore(0, 1)",
                },
            ],
        }

        normalized = plain_cfg_llm._normalize_generated_reconstruction(parsed, context)

        self.assertEqual(normalized["confidence"], "medium")
        self.assertEqual(normalized["unresolved"], ["The original variable name is unavailable."])
        self.assertEqual(
            [item["opcodes"] for item in normalized["evidence"]],
            [["MSTORE"], ["SSTORE"]],
        )
        self.assertEqual(
            plain_cfg_llm._validate_generated_reconstruction(normalized, context),
            [],
        )

    def test_synthesizes_trace_grounded_side_effect_evidence_when_omitted(self):
        parsed = {
            "solidity": "{\n    assembly { sstore(0, 1) return(0, 0) }\n}",
            "confidence": "medium",
            "unresolved": [],
        }

        normalized = plain_cfg_llm._normalize_generated_reconstruction(parsed, self.context)

        self.assertEqual(
            normalized["evidence"],
            [
                {
                    "step_start": 10,
                    "step_end": 11,
                    "opcodes": ["SSTORE", "RETURN"],
                    "code": parsed["solidity"],
                }
            ],
        )
        self.assertEqual(
            plain_cfg_llm._validate_generated_reconstruction(normalized, self.context),
            [],
        )

    def test_missing_evidence_does_not_hide_an_unrepresented_effect(self):
        parsed = {
            "solidity": "{ assembly { sstore(0, 1) } }",
            "confidence": "medium",
            "unresolved": [],
            "evidence": [],
        }

        normalized = plain_cfg_llm._normalize_generated_reconstruction(parsed, self.context)
        errors = plain_cfg_llm._validate_generated_reconstruction(normalized, self.context)

        self.assertEqual(normalized["evidence"][0]["opcodes"], ["SSTORE"])
        self.assertTrue(any("RETURN" in error and "coverage" in error for error in errors))
        self.assertTrue(any("observed RETURN is not represented" in error for error in errors))

    def test_comment_does_not_count_as_effect_evidence(self):
        parsed = {
            "solidity": "{ assembly { sstore(0, 1) } } // return",
            "confidence": "medium",
            "unresolved": [],
            "evidence": [],
        }

        normalized = plain_cfg_llm._normalize_generated_reconstruction(parsed, self.context)

        self.assertEqual(normalized["evidence"][0]["opcodes"], ["SSTORE"])

    def test_synthesizes_evidence_after_unprovable_items_are_dropped(self):
        parsed = {
            "solidity": "{ assembly { sstore(0, 1) return(0, 0) } }",
            "confidence": "medium",
            "unresolved": [],
            "evidence": [
                {
                    "step_start": 10,
                    "step_end": 11,
                    "opcodes": [],
                    "code": "assembly",
                }
            ],
        }

        normalized = plain_cfg_llm._normalize_generated_reconstruction(parsed, self.context)

        self.assertEqual(normalized["evidence"][0]["opcodes"], ["SSTORE", "RETURN"])
        self.assertEqual(
            plain_cfg_llm._validate_generated_reconstruction(normalized, self.context),
            [],
        )


class ReconstructionModelTests(unittest.TestCase):
    def test_parser_finds_json_object_after_gateway_text(self):
        parsed = plain_cfg_llm._parse_response_json(
            'gateway note {not-json}\n```json\n{"ok": true, "code": "{ x(); }"}\n```'
        )
        self.assertEqual(parsed, {"ok": True, "code": "{ x(); }"})

    def test_responses_mode_requests_strict_schema_and_returns_valid_result(self):
        context = {
            "target": {
                "step_count": 1,
                "opcode_histogram": {"SSTORE": 1},
                "execution_trace": [{"step": 10, "pc": "0xa", "op": "SSTORE"}],
            },
            "call_context": {"active_frame": {"signature_candidates": []}},
        }
        result = {
            "solidity": "{ assembly { sstore(0, 1) } }",
            "confidence": "medium",
            "unresolved": ["The original storage variable name is unavailable."],
            "evidence": [
                {
                    "step_start": 10,
                    "step_end": 10,
                    "opcodes": ["SSTORE"],
                    "code": "sstore(0, 1)",
                }
            ],
        }
        client = mock.Mock()
        client.responses.create.return_value = SimpleNamespace(output_text=json.dumps(result))

        with (
            mock.patch.dict(
                "os.environ",
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "DEEPSEEK_CFG_API_MODE": "responses",
                },
            ),
            mock.patch.object(plain_cfg_llm, "OpenAI", return_value=client),
        ):
            generated = plain_cfg_llm._generate_reconstruction("{}", context)

        self.assertEqual(generated, result)
        request = client.responses.create.call_args.kwargs
        self.assertTrue(request["text"]["format"]["strict"])
        self.assertEqual(
            request["text"]["format"]["schema"],
            plain_cfg_llm.RECONSTRUCTION_OUTPUT_SCHEMA,
        )

    def test_chat_retry_doubles_budget_after_truncated_json(self):
        context = {
            "target": {
                "step_count": 1,
                "opcode_histogram": {"SSTORE": 1},
                "execution_trace": [{"step": 10, "pc": "0xa", "op": "SSTORE"}],
            },
            "call_context": {"active_frame": {"signature_candidates": []}},
        }
        valid = {
            "solidity": "{ assembly { sstore(0, 1) } }",
            "confidence": "medium",
            "unresolved": [],
            "evidence": [
                {
                    "step_start": 10,
                    "step_end": 10,
                    "opcodes": ["SSTORE"],
                    "code": "sstore(0, 1)",
                }
            ],
        }
        truncated = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"solidity": "{',
                        reasoning_content="reasoning",
                    ),
                    finish_reason="length",
                )
            ]
        )
        complete = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(valid)),
                    finish_reason="stop",
                )
            ]
        )
        client = mock.Mock()
        client.chat.completions.create.side_effect = [truncated, complete]

        with (
            mock.patch.dict(
                "os.environ",
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "DEEPSEEK_CFG_API_MODE": "chat_completions",
                },
            ),
            mock.patch.object(plain_cfg_llm, "OpenAI", return_value=client),
        ):
            generated = plain_cfg_llm._generate_reconstruction("{}", context)

        self.assertEqual(generated, valid)
        budgets = [
            call.kwargs["max_tokens"]
            for call in client.chat.completions.create.call_args_list
        ]
        self.assertEqual(budgets, [16384, 32768])

    def test_validation_retry_uses_compact_opcode_step_repair(self):
        context = {
            "target": {
                "start_step": 11,
                "end_step": 12,
                "step_count": 2,
                "opcode_histogram": {"MSTORE": 1, "SSTORE": 1},
                "effect_and_dependency_facts": {"storage": []},
                "execution_trace": [
                    {"step": 11, "pc": "0xb", "op": "MSTORE"},
                    {"step": 12, "pc": "0xc", "op": "SSTORE"},
                ],
            },
            "call_context": {"active_frame": {"signature_candidates": []}},
        }
        solidity = "{ assembly { mstore(0, 1) sstore(0, 1) } }"
        invalid = {
            "solidity": solidity,
            "confidence": "medium",
            "unresolved": [],
            "evidence": [
                {
                    "step_start": 12,
                    "step_end": 12,
                    "opcodes": ["MSTORE"],
                    "code": "mstore(0, 1)",
                },
                {
                    "step_start": 12,
                    "step_end": 12,
                    "opcodes": ["SSTORE"],
                    "code": "sstore(0, 1)",
                },
            ],
        }
        repaired = copy.deepcopy(invalid)
        repaired["evidence"][0]["step_start"] = 11
        repaired["evidence"][0]["step_end"] = 11
        client = mock.Mock()
        client.chat.completions.create.side_effect = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=json.dumps(invalid)),
                        finish_reason="stop",
                    )
                ]
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=json.dumps(repaired)),
                        finish_reason="stop",
                    )
                ]
            ),
        ]

        with (
            mock.patch.dict(
                "os.environ",
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "DEEPSEEK_CFG_API_MODE": "chat_completions",
                },
            ),
            mock.patch.object(plain_cfg_llm, "OpenAI", return_value=client),
        ):
            generated = plain_cfg_llm._generate_reconstruction("FULL_CONTEXT", context)

        self.assertEqual(generated, repaired)
        repair_prompt = client.chat.completions.create.call_args_list[1].kwargs["messages"][1]["content"]
        self.assertIn("Targeted deterministic-validation repair JSON", repair_prompt)
        self.assertIn('"MSTORE":[11]', repair_prompt)
        self.assertNotIn("Evidence context JSON", repair_prompt)


if __name__ == "__main__":
    unittest.main()
