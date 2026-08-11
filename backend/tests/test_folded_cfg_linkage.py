from __future__ import annotations

import unittest

from utils.basic_block import Block
from utils.cfg_structure import CFG
from utils.cfg_transaction import CFGConstructor, FoldableBlockNode
from utils.extract_token_changes import (
    LINK_ARTIFACT_SCHEMA_VERSION,
    afg_to_fcfg,
    afg_to_call_tree,
    build_link_artifact,
)


def _node(node_id: int, address: str, start_pc: str) -> FoldableBlockNode:
    block = Block(start_pc=start_pc, address=address)
    block.end_pc = start_pc
    block.instructions = [(start_pc, "SLOAD")]
    node = FoldableBlockNode(block)
    node.id = node_id
    node.folded_blocks = [node_id]
    return node


class FoldedStepRangeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = CFG("0xtest")
        self.node_a = _node(1, "0xtoken", "0x10")
        self.node_b = _node(2, "0xtoken", "0x20")
        self.node_c = _node(3, "0xrouter", "0x30")
        self.cfg.nodes = [self.node_a, self.node_b, self.node_c]
        self.cfg.add_edge(self.node_a, self.node_b, "NORMAL", 2)
        self.cfg.add_edge(self.node_b, self.node_a, "NORMAL", 5)
        self.cfg.add_edge(self.node_a, self.node_c, "NORMAL", 7)

        CFGConstructor._assign_folded_step_ranges(self.cfg, trace_step_count=10)

    def test_repeated_node_keeps_separate_execution_ranges(self) -> None:
        self.assertEqual(
            self.node_a.fold_info["step_ranges"],
            [
                {"start_step": 0, "end_step": 2},
                {"start_step": 6, "end_step": 7},
            ],
        )
        self.assertEqual(
            self.node_b.fold_info["step_ranges"],
            [{"start_step": 3, "end_step": 5}],
        )
        self.assertEqual(
            self.node_c.fold_info["step_ranges"],
            [{"start_step": 8, "end_step": 9}],
        )
        self.assertNotIn("start_step", self.node_a.fold_info)
        self.assertNotIn("end_step", self.node_a.fold_info)

    def test_folded_block_export_uses_step_ranges_without_pc_range(self) -> None:
        block_info = CFGConstructor([]).build_fcfg_blocks_information(self.cfg)[1]

        self.assertEqual(block_info["schema_version"], 2)
        self.assertEqual(len(block_info["step_ranges"]), 2)
        self.assertNotIn("start_pc", block_info)
        self.assertNotIn("end_pc", block_info)
        self.assertNotIn("start_step", block_info)
        self.assertNotIn("end_step", block_info)

    def test_token_flow_uses_source_steps_for_final_folded_nodes(self) -> None:
        paired = [
            {"order": 0, "token": "ETH"},
            {"order": 1, "token": "ETH", "source_steps": [6]},
            {
                "order": 2,
                "token": "USDC",
                "source_steps": {
                    "sender_sload_step": 1,
                    "sender_sstore_step": 2,
                    "receiver_sload_step": 3,
                    "receiver_sstore_step": 4,
                },
            },
        ]
        pending = [{"order": 3, "source_steps": [7, 8]}]

        links = afg_to_fcfg(paired, pending, self.cfg)

        self.assertEqual(links[0]["mapping_status"], "complete")
        self.assertEqual(links[0]["matched_blocks"], 1)
        self.assertEqual(
            links[1]["matched_blocks"],
            {"sender": [1], "receiver": [2]},
        )
        self.assertEqual(links[2]["matched_blocks"], [1, 3])
        self.assertEqual(
            [entry["source_step"] for entry in links[1]["evidence"]],
            [1, 2, 3, 4],
        )

    def test_ambiguous_step_is_reported_instead_of_picking_first_node(self) -> None:
        self.node_c.fold_info["step_ranges"].append(
            {"start_step": 6, "end_step": 6}
        )

        links = afg_to_fcfg(
            [{"order": 1, "token": "ETH", "source_steps": [6]}],
            [],
            self.cfg,
        )

        self.assertEqual(links[0]["mapping_status"], "ambiguous")
        self.assertEqual(links[0]["matched_blocks"], [])
        self.assertEqual(links[0]["evidence"][0]["status"], "ambiguous")

    def test_plain_mode_does_not_pollute_folded_node_mapping(self) -> None:
        constructor = CFGConstructor([])
        constructor.folded_node_map = {1: [101], 2: [102], 3: [103]}
        expected_source_map = {1: [101], 2: [102], 3: [103]}

        constructor._mode_fold(self.cfg, "plain")
        _folded_cfg, folded_map = constructor._mode_fold(self.cfg, "folded")

        self.assertEqual(constructor.folded_node_map, expected_source_map)
        self.assertEqual(folded_map, expected_source_map)

    def test_folded_members_are_ids_without_source_node_references(self) -> None:
        self.node_a.merge_fold_info([self.node_b])

        self.assertEqual(self.node_a.folded_blocks, [1, 2])
        self.assertTrue(all(isinstance(block_id, int) for block_id in self.node_a.folded_blocks))
        self.assertNotIn(self.node_a, self.node_a.folded_blocks)
        self.assertNotIn(self.node_b, self.node_a.folded_blocks)

    def test_mode_fold_preserves_repeated_execution_edges(self) -> None:
        repeated_cfg = CFG("0xrepeated")
        node_a = _node(1, "0xrouter", "0x10")
        node_b = _node(2, "0xtoken", "0x20")
        repeated_cfg.nodes = [node_a, node_b]
        repeated_cfg.add_edge(node_a, node_b, "CALL", 2)
        repeated_cfg.add_edge(node_b, node_a, "RETURN", 5)
        repeated_cfg.add_edge(node_a, node_b, "CALL", 8)

        constructor = CFGConstructor([])
        constructor.folded_node_map = {1: [1], 2: [2]}
        folded_cfg, _folded_map = constructor._mode_fold(repeated_cfg, "folded")

        self.assertEqual(
            [(edge.edge_type, edge.edge_step) for edge in folded_cfg.edges],
            [("CALL", 2), ("RETURN", 5), ("CALL", 8)],
        )
        self.assertEqual(
            [(edge.source.id, edge.target.id) for edge in folded_cfg.edges],
            [(1, 2), (2, 1), (1, 2)],
        )
        self.assertTrue(all(edge not in repeated_cfg.edges for edge in folded_cfg.edges))
        self.assertTrue(all(node not in repeated_cfg.nodes for node in folded_cfg.nodes))

    def test_link_artifact_keeps_both_cfg_modes_in_one_contract(self) -> None:
        folded = [{"edge_id": 1, "matched_blocks": 11}]
        plain = [{"edge_id": 1, "matched_blocks": [101, 102]}]
        call_tree = [{"edge_id": 1, "matched_calls": [{"call_id": 2}]}]

        artifact = build_link_artifact(folded, plain, call_tree)

        self.assertEqual(artifact["schema_version"], LINK_ARTIFACT_SCHEMA_VERSION)
        self.assertEqual(artifact["edge_links"]["folded"], folded)
        self.assertEqual(artifact["edge_links"]["plain"], plain)
        self.assertEqual(artifact["edge_links"]["call_tree"], call_tree)

    def test_token_flow_maps_each_step_to_deepest_call_and_multiple_contracts(self) -> None:
        trace_tree = {
            "contract": "0xroot",
            "calls": [
                {
                    "contract": "0xrouter",
                    "entry_step": 2,
                    "exit_step": 20,
                    "calls": [
                        {
                            "contract": "0xtoken-a",
                            "entry_step": 4,
                            "exit_step": 8,
                            "calls": [],
                        },
                        {
                            "contract": "0xtoken-b",
                            "entry_step": 12,
                            "exit_step": 16,
                            "calls": [],
                        },
                    ],
                }
            ],
        }
        paired = [{
            "order": 1,
            "token": "USDC",
            "source_steps": {
                "sender_sload_step": 5,
                "sender_sstore_step": 6,
                "receiver_sload_step": 13,
                "receiver_sstore_step": 14,
            },
        }]

        links = afg_to_call_tree(paired, [], trace_tree)

        self.assertEqual(links[0]["mapping_status"], "complete")
        self.assertEqual(links[0]["matched_calls"], [
            {"call_id": 2, "contract_address": "0xtoken-a"},
            {"call_id": 3, "contract_address": "0xtoken-b"},
        ])
        self.assertEqual(links[0]["matched_contracts"], ["0xtoken-a", "0xtoken-b"])

    def test_token_flow_step_outside_child_calls_maps_to_root_contract(self) -> None:
        links = afg_to_call_tree(
            [{"order": 1, "token": "ETH", "source_steps": [1]}],
            [],
            {"contract": "0xroot", "calls": []},
        )

        self.assertEqual(links[0]["matched_calls"], [
            {"call_id": None, "contract_address": "0xroot"},
        ])


if __name__ == "__main__":
    unittest.main()
