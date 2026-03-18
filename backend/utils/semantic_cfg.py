from __future__ import annotations

import json
import os
import re
from collections import defaultdict, OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI
from openai import APIStatusError
from openai import NotFoundError

from utils.cfg_transaction import FoldableBlockNode


SEMANTIC_EDGE_TYPES = {"CALL", "DELEGATECALL", "TERMINATE"}
SUPPORTED_REASONING_EFFORTS = {"minimal", "low", "medium", "high", "none"}
RESPONSES_API_FALLBACK_HINTS = (
    "/chat/completions/responses",
    "invalid url",
    "not implemented",
    "convert_request_failed",
    "/responses",
)
SEMANTIC_TASK_DESCRIPTION = (
    "Provide stable semantic labels for rule-constrained CFG regions from a single contract. "
    "Do not change membership. Prefer high-level semantic descriptions over low-level opcode mechanics. "
    "Use one umbrella label for the whole region instead of naming each micro-step. "
    "If a region combines setup, validation, decoding, and preparation for the same goal, summarize the broader intent. "
    "Avoid labels like memory buffer, pointer arithmetic, hash computation, or calldata offset unless that is truly the main intent. "
    "Use workflow-level labels such as dispatch, decode inputs, validate path, prepare swap, execute transfer, settle balances, return result. "
    "The label should usually be 2 to 4 words. The purpose should be one concise sentence."
)
SEMANTIC_JSON_OUTPUT_HINT = (
    "{\"regions\":[{\"semantic_node_id\":string,\"member_block_ids\":number[],"
    "\"label\":string,\"purpose\":string,\"confidence\":number,"
    "\"entry_conditions\":string[],\"exit_effects\":string[]}]}"
)
SEMANTIC_SYSTEM_PROMPT = (
    "You are labeling rule-constrained EVM CFG regions. "
    "Do not change region membership. "
    "Prefer high-level semantic intent over opcode-level mechanics. "
    "Use one umbrella label for the whole region, not a list of micro-operations. "
    "If setup, validation, and decoding all serve one goal, summarize that broader goal. "
    "Avoid labels like memory buffer, pointer arithmetic, hash computation, or calldata offset unless that is the dominant meaning. "
    "Use short labels, usually 2 to 4 words. "
    "Use one concise sentence for purpose. "
    f"Return only valid JSON matching this shape: {SEMANTIC_JSON_OUTPUT_HINT}."
)
SEMANTIC_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "regions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "semantic_node_id": {"type": "string"},
                    "member_block_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "label": {"type": "string"},
                    "purpose": {"type": "string"},
                    "confidence": {"type": "number"},
                    "entry_conditions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "exit_effects": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "semantic_node_id",
                    "member_block_ids",
                    "label",
                    "purpose",
                    "confidence",
                    "entry_conditions",
                    "exit_effects",
                ],
            },
        }
    },
    "required": ["regions"],
}


@dataclass
class SemanticRegionCandidate:
    semantic_node_id: str
    contract_address: str
    contract_name: str
    member_block_ids: List[int]
    member_blocks: List[Dict[str, Any]]
    start_pc: str
    end_pc: str
    total_gas: float
    entry_edge_types: List[str]
    exit_edge_types: List[str]
    trace_step_range: Dict[str, Optional[int]]
    instruction_summary: List[str]
    action_summary: List[str]

    def to_prompt_dict(self) -> Dict[str, Any]:
        return {
            "semantic_node_id": self.semantic_node_id,
            "contract_address": self.contract_address,
            "contract_name": self.contract_name,
            "member_block_ids": self.member_block_ids,
            "start_pc": self.start_pc,
            "end_pc": self.end_pc,
            "total_gas": self.total_gas,
            "entry_edge_types": self.entry_edge_types,
            "exit_edge_types": self.exit_edge_types,
            "trace_step_range": self.trace_step_range,
            "instruction_summary": self.instruction_summary,
            "action_summary": self.action_summary,
        }


class SemanticCFGBuilder:
    """Build a rule-constrained semantic CFG and let the model annotate regions."""

    def __init__(
        self,
        full_address_name_map: Dict[str, str],
        erc20_token_map: Dict[str, Any],
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        min_confidence: float = 0.45,
    ):
        self.full_name_map_lower = {
            str(addr).lower(): name for addr, name in (full_address_name_map or {}).items()
        }
        self.min_confidence = min_confidence
        self.model = model or os.environ.get("OPENAI_SEMANTIC_CFG_MODEL", "gpt-5-mini")
        api_key = api_key or os.environ.get("OPENAI_API_KEY")
        base_url = base_url or os.environ.get("OPENAI_BASE_URL")
        self.base_url = self._normalize_base_url(base_url)
        timeout_seconds = timeout_seconds or float(
            os.environ.get("OPENAI_SEMANTIC_CFG_TIMEOUT_SECONDS", "45")
        )
        self.max_prompt_chars = int(os.environ.get("OPENAI_SEMANTIC_CFG_MAX_PROMPT_CHARS", "120000"))
        self.batch_size = int(os.environ.get("OPENAI_SEMANTIC_CFG_BATCH_SIZE", "20"))
        self.coarse_group_size = int(os.environ.get("OPENAI_SEMANTIC_CFG_COARSE_GROUP_SIZE", "18"))
        self.target_node_count = int(os.environ.get("OPENAI_SEMANTIC_CFG_TARGET_NODE_COUNT", "28"))
        self.reasoning_effort = os.environ.get("OPENAI_SEMANTIC_CFG_REASONING_EFFORT", "minimal").strip().lower()
        self.api_mode = os.environ.get("OPENAI_SEMANTIC_CFG_API_MODE", "auto").strip().lower()
        self.responses_api_supported = self.api_mode != "chat_completions"
        self.client: Optional[OpenAI] = None
        if api_key:
            client_kwargs: Dict[str, Any] = {
                "api_key": api_key,
                "timeout": timeout_seconds,
            }
            if self.base_url:
                client_kwargs["base_url"] = self.base_url
            self.client = OpenAI(**client_kwargs)
            print(
                f"[INFO] Semantic CFG client initialized with base URL: {self.base_url}, "
                f"api_mode={self.api_mode}"
            )

    def build(
        self,
        cfg: Any,
        folded_blocks_map: Dict[str, Any],
        edge_step_map: Dict[str, Any],
    ) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
        if not self.client:
            print("[INFO] OPENAI_API_KEY not configured, skip semantic CFG generation.")
            return None

        visible_nodes = self._get_visible_nodes(cfg)
        visible_edges = self._get_visible_edges(cfg, visible_nodes)
        if not visible_nodes:
            print("[INFO] No visible CFG nodes found, skip semantic CFG generation.")
            return None

        candidates = self._build_region_candidates(visible_nodes, visible_edges, folded_blocks_map, edge_step_map)
        if not candidates:
            print("[INFO] No semantic region candidates found, skip semantic CFG generation.")
            return None
        candidates = self._coarsen_region_candidates(candidates)
        candidates = self._compress_to_target_node_count(candidates)
        print(f"[INFO] Semantic CFG: coarsened to {len(candidates)} semantic regions before LLM labeling.")

        annotations = self._annotate_regions_with_llm(cfg.tx_hash, candidates)
        semantic_payload, semantic_edge_step_map = self._assemble_payload(
            candidates,
            annotations,
            visible_edges,
            edge_step_map,
        )
        return semantic_payload, semantic_edge_step_map

    def export(
        self,
        result_dir: str,
        semantic_payload: Dict[str, Any],
        semantic_edge_step_map: Dict[str, Any],
    ) -> None:
        semantic_json_path = os.path.join(result_dir, "semantic_cfg.json")
        with open(semantic_json_path, "w", encoding="utf-8") as f:
            json.dump(semantic_payload, f, ensure_ascii=False, indent=2)
        print(f"[OK] Semantic CFG metadata exported: {semantic_json_path}")

        semantic_edge_path = os.path.join(result_dir, "semantic_edge_id-step.json")
        with open(semantic_edge_path, "w", encoding="utf-8") as f:
            json.dump(semantic_edge_step_map, f, ensure_ascii=False, indent=2)
        print(f"[OK] Semantic edge-step mapping exported: {semantic_edge_path}")

    def _get_visible_nodes(self, cfg: Any) -> Dict[int, FoldableBlockNode]:
        visible: Dict[int, FoldableBlockNode] = {}
        for node in getattr(cfg, "nodes", []):
            if not isinstance(node, FoldableBlockNode):
                continue
            is_fold_root = getattr(node, "is_fold_root", False)
            is_folded = getattr(node, "folded", False)
            if is_fold_root or not is_folded:
                visible[node.id] = node
        return visible

    def _get_visible_edges(self, cfg: Any, visible_nodes: Dict[int, FoldableBlockNode]) -> List[Any]:
        visible_edges = []
        for edge in getattr(cfg, "edges", []):
            if getattr(edge, "visible", True) is False:
                continue
            src = getattr(edge, "source", None)
            tgt = getattr(edge, "target", None)
            if not src or not tgt:
                continue
            if src.id in visible_nodes and tgt.id in visible_nodes:
                visible_edges.append(edge)
        return visible_edges

    def _build_region_candidates(
        self,
        visible_nodes: Dict[int, FoldableBlockNode],
        visible_edges: List[Any],
        folded_blocks_map: Dict[str, Any],
        edge_step_map: Dict[str, Any],
    ) -> List[SemanticRegionCandidate]:
        out_edges: Dict[int, List[Any]] = defaultdict(list)
        in_edges: Dict[int, List[Any]] = defaultdict(list)
        loop_nodes = set()
        for edge in visible_edges:
            out_edges[edge.source.id].append(edge)
            in_edges[edge.target.id].append(edge)
            if edge.target.id <= edge.source.id:
                loop_nodes.add(edge.source.id)
                loop_nodes.add(edge.target.id)

        sorted_nodes = sorted(visible_nodes.values(), key=lambda node: node.id)
        assigned: set[int] = set()
        candidates: List[SemanticRegionCandidate] = []
        region_idx = 1

        for node in sorted_nodes:
            if node.id in assigned:
                continue

            if self._is_anchor(node, in_edges[node.id], out_edges[node.id], loop_nodes):
                member_nodes = [node]
            else:
                member_nodes = self._expand_region(
                    node,
                    visible_nodes,
                    in_edges,
                    out_edges,
                    loop_nodes,
                    assigned,
                )

            for member in member_nodes:
                assigned.add(member.id)

            semantic_node_id = f"semantic_{region_idx}"
            region_idx += 1
            candidates.append(
                self._build_candidate(
                    semantic_node_id,
                    member_nodes,
                    in_edges,
                    out_edges,
                    folded_blocks_map,
                    edge_step_map,
                )
            )

        return candidates

    def _coarsen_region_candidates(
        self,
        candidates: List[SemanticRegionCandidate],
    ) -> List[SemanticRegionCandidate]:
        if self.coarse_group_size <= 1 or len(candidates) <= 1:
            return self._renumber_candidates(candidates)

        grouped_by_contract: "OrderedDict[str, List[SemanticRegionCandidate]]" = OrderedDict()
        for candidate in candidates:
            grouped_by_contract.setdefault(candidate.contract_address, []).append(candidate)

        merged_candidates: List[SemanticRegionCandidate] = []
        for contract_candidates in grouped_by_contract.values():
            buffer: List[SemanticRegionCandidate] = []
            for candidate in contract_candidates:
                if not buffer:
                    buffer.append(candidate)
                else:
                    if self._should_break_coarse_group(buffer, candidate):
                        merged_candidates.append(self._merge_candidate_group(buffer))
                        buffer = [candidate]
                    else:
                        buffer.append(candidate)

                if len(buffer) >= self.coarse_group_size and not self._contains_hard_boundary(candidate):
                    merged_candidates.append(self._merge_candidate_group(buffer))
                    buffer = []

                if buffer and self._contains_hard_boundary(buffer[-1]):
                    merged_candidates.append(self._merge_candidate_group(buffer))
                    buffer = []

            if buffer:
                merged_candidates.append(self._merge_candidate_group(buffer))

        return self._renumber_candidates(merged_candidates)

    def _should_break_coarse_group(
        self,
        current_group: List[SemanticRegionCandidate],
        next_candidate: SemanticRegionCandidate,
    ) -> bool:
        last_candidate = current_group[-1]
        if last_candidate.contract_address != next_candidate.contract_address:
            return True
        if self._contains_hard_boundary(last_candidate) or self._contains_hard_boundary(next_candidate):
            return True
        if len(current_group) >= self.coarse_group_size:
            return True
        return False

    def _contains_hard_boundary(self, candidate: SemanticRegionCandidate) -> bool:
        boundary_types = {"CALL", "DELEGATECALL"}
        if any(edge_type in boundary_types for edge_type in candidate.entry_edge_types + candidate.exit_edge_types):
            return True
        return False

    def _merge_candidate_group(
        self,
        candidates: List[SemanticRegionCandidate],
    ) -> SemanticRegionCandidate:
        first = candidates[0]
        last = candidates[-1]
        member_block_ids: List[int] = []
        member_blocks: List[Dict[str, Any]] = []
        total_gas = 0.0
        action_summary: List[str] = []
        instruction_fragments: List[str] = []

        for candidate in candidates:
            member_block_ids.extend(candidate.member_block_ids)
            member_blocks.extend(candidate.member_blocks)
            total_gas += candidate.total_gas
            action_summary.extend(candidate.action_summary)
            instruction_fragments.extend(candidate.instruction_summary)

        compact_instruction_summary = self._merge_instruction_summaries(instruction_fragments)
        compact_action_summary = action_summary[:3]
        entry_step_values = [c.trace_step_range.get("entry_step") for c in candidates if c.trace_step_range.get("entry_step") is not None]
        exit_step_values = [c.trace_step_range.get("exit_step") for c in candidates if c.trace_step_range.get("exit_step") is not None]

        return SemanticRegionCandidate(
            semantic_node_id=first.semantic_node_id,
            contract_address=first.contract_address,
            contract_name=first.contract_name,
            member_block_ids=member_block_ids,
            member_blocks=member_blocks,
            start_pc=first.start_pc,
            end_pc=last.end_pc,
            total_gas=total_gas,
            entry_edge_types=first.entry_edge_types,
            exit_edge_types=last.exit_edge_types,
            trace_step_range={
                "entry_step": min(entry_step_values) if entry_step_values else None,
                "exit_step": max(exit_step_values) if exit_step_values else None,
            },
            instruction_summary=compact_instruction_summary,
            action_summary=compact_action_summary,
        )

    def _merge_instruction_summaries(self, fragments: List[str]) -> List[str]:
        opcodes: List[str] = []
        seen = set()
        for fragment in fragments:
            parts = [part.strip() for part in str(fragment).split("->")]
            for part in parts:
                if not part:
                    continue
                if part not in seen:
                    opcodes.append(part)
                    seen.add(part)
                if len(opcodes) >= 5:
                    break
            if len(opcodes) >= 5:
                break

        if not opcodes:
            return []
        if len(opcodes) == 1:
            return [opcodes[0]]
        return [" -> ".join(opcodes)]

    def _renumber_candidates(
        self,
        candidates: List[SemanticRegionCandidate],
    ) -> List[SemanticRegionCandidate]:
        renumbered: List[SemanticRegionCandidate] = []
        for idx, candidate in enumerate(candidates, start=1):
            renumbered.append(
                SemanticRegionCandidate(
                    semantic_node_id=f"semantic_{idx}",
                    contract_address=candidate.contract_address,
                    contract_name=candidate.contract_name,
                    member_block_ids=candidate.member_block_ids,
                    member_blocks=candidate.member_blocks,
                    start_pc=candidate.start_pc,
                    end_pc=candidate.end_pc,
                    total_gas=candidate.total_gas,
                    entry_edge_types=candidate.entry_edge_types,
                    exit_edge_types=candidate.exit_edge_types,
                    trace_step_range=candidate.trace_step_range,
                    instruction_summary=candidate.instruction_summary,
                    action_summary=candidate.action_summary,
                )
            )
        return renumbered

    def _compress_to_target_node_count(
        self,
        candidates: List[SemanticRegionCandidate],
    ) -> List[SemanticRegionCandidate]:
        if self.target_node_count <= 0 or len(candidates) <= self.target_node_count:
            return self._renumber_candidates(candidates)
        compressed = list(candidates)
        while len(compressed) > self.target_node_count:
            best_index: Optional[int] = None
            best_score: Optional[float] = None
            for idx in range(len(compressed) - 1):
                left = compressed[idx]
                right = compressed[idx + 1]
                if left.contract_address != right.contract_address:
                    continue
                score = self._merge_penalty(left, right)
                if best_score is None or score < best_score:
                    best_score = score
                    best_index = idx

            if best_index is None:
                break

            merged = self._merge_candidate_group([compressed[best_index], compressed[best_index + 1]])
            compressed = compressed[:best_index] + [merged] + compressed[best_index + 2:]

        return self._renumber_candidates(compressed)

    def _merge_penalty(
        self,
        left: SemanticRegionCandidate,
        right: SemanticRegionCandidate,
    ) -> float:
        boundary_types = set(left.exit_edge_types + right.entry_edge_types)
        if "CALL" in boundary_types or "DELEGATECALL" in boundary_types:
            return 10000.0

        penalty = 0.0
        total_blocks = len(left.member_block_ids) + len(right.member_block_ids)
        penalty += total_blocks / 18.0

        if left.action_summary:
            penalty += 3.0
        if right.action_summary:
            penalty += 3.0
        if "TERMINATE" in boundary_types:
            penalty += 1.4
        if len(left.exit_edge_types) > 1:
            penalty += 1.0
        if len(right.entry_edge_types) > 1:
            penalty += 0.8
        if len(left.member_block_ids) <= 2:
            penalty -= 0.9
        if len(right.member_block_ids) <= 2:
            penalty -= 0.9
        if left.contract_name == right.contract_name:
            penalty -= 0.3

        return penalty

    def _is_anchor(
        self,
        node: FoldableBlockNode,
        in_edges: List[Any],
        out_edges: List[Any],
        loop_nodes: set[int],
    ) -> bool:
        if node.id in loop_nodes:
            return True
        if node.actions or node.fold_info.get("actions"):
            return True
        if len(in_edges) != 1 or len(out_edges) != 1:
            return True
        if any(edge.edge_type in SEMANTIC_EDGE_TYPES for edge in in_edges + out_edges):
            return True
        if any(edge.source.address != node.address or edge.target.address != node.address for edge in in_edges + out_edges):
            return True
        return False

    def _expand_region(
        self,
        seed: FoldableBlockNode,
        visible_nodes: Dict[int, FoldableBlockNode],
        in_edges: Dict[int, List[Any]],
        out_edges: Dict[int, List[Any]],
        loop_nodes: set[int],
        assigned: set[int],
    ) -> List[FoldableBlockNode]:
        region = [seed]

        # Expand backward to gather a whole non-anchor chain.
        current = seed
        while True:
            candidates = in_edges[current.id]
            if len(candidates) != 1:
                break
            prev_node = candidates[0].source
            if prev_node.id in assigned or prev_node.id == current.id:
                break
            if prev_node.address != seed.address:
                break
            if self._is_anchor(prev_node, in_edges[prev_node.id], out_edges[prev_node.id], loop_nodes):
                break
            if len(out_edges[prev_node.id]) != 1 or out_edges[prev_node.id][0].target.id != current.id:
                break
            region.insert(0, prev_node)
            current = prev_node

        current = region[-1]
        while True:
            candidates = out_edges[current.id]
            if len(candidates) != 1:
                break
            next_node = candidates[0].target
            if next_node.id in assigned or next_node.id in {member.id for member in region}:
                break
            if next_node.address != seed.address:
                break
            if self._is_anchor(next_node, in_edges[next_node.id], out_edges[next_node.id], loop_nodes):
                break
            if len(in_edges[next_node.id]) != 1 or in_edges[next_node.id][0].source.id != current.id:
                break
            region.append(next_node)
            current = next_node

        return region

    def _build_candidate(
        self,
        semantic_node_id: str,
        member_nodes: List[FoldableBlockNode],
        in_edges: Dict[int, List[Any]],
        out_edges: Dict[int, List[Any]],
        folded_blocks_map: Dict[str, Any],
        edge_step_map: Dict[str, Any],
    ) -> SemanticRegionCandidate:
        member_block_ids = [node.id for node in member_nodes]
        member_blocks = []
        for block_id in member_block_ids:
            block = folded_blocks_map.get(block_id)
            if block is None:
                block = folded_blocks_map.get(str(block_id))
            if block is not None:
                member_blocks.append(block)
        contract_address = member_nodes[0].address
        contract_name = self._lookup_contract_name(contract_address)
        total_gas = sum(float(block.get("gas", 0)) for block in member_blocks)
        entry_edge_types = sorted({
            edge.edge_type
            for node in member_nodes
            for edge in in_edges[node.id]
            if edge.source.id not in member_block_ids
        })
        exit_edge_types = sorted({
            edge.edge_type
            for node in member_nodes
            for edge in out_edges[node.id]
            if edge.target.id not in member_block_ids
        })
        step_values = self._collect_region_steps(member_block_ids, edge_step_map)
        actions = []
        for block in member_blocks:
            actions.extend(block.get("actions", []))

        start_pc = member_blocks[0].get("start_pc", member_nodes[0].start_pc) if member_blocks else member_nodes[0].start_pc
        end_pc = (
            member_blocks[-1].get("end_pc", member_nodes[-1].fold_info.get("end_pc", member_nodes[-1].end_pc))
            if member_blocks else
            member_nodes[-1].fold_info.get("end_pc", member_nodes[-1].end_pc)
        )

        return SemanticRegionCandidate(
            semantic_node_id=semantic_node_id,
            contract_address=contract_address,
            contract_name=contract_name,
            member_block_ids=member_block_ids,
            member_blocks=member_blocks,
            start_pc=start_pc,
            end_pc=end_pc,
            total_gas=total_gas,
            entry_edge_types=entry_edge_types,
            exit_edge_types=exit_edge_types,
            trace_step_range={
                "entry_step": min(step_values) if step_values else None,
                "exit_step": max(step_values) if step_values else None,
            },
            instruction_summary=self._summarize_instructions(member_blocks),
            action_summary=self._summarize_actions(actions),
        )

    def _collect_region_steps(self, member_block_ids: List[int], edge_step_map: Dict[str, Any]) -> List[int]:
        member_names = {f"node_{block_id}" for block_id in member_block_ids}
        steps = []
        for entry in edge_step_map.values():
            source_node = entry.get("source_node")
            target_node = entry.get("target_node")
            if source_node in member_names or target_node in member_names:
                edge_step = entry.get("edge_step")
                if isinstance(edge_step, int):
                    steps.append(edge_step)
        return steps

    def _summarize_instructions(self, member_blocks: List[Dict[str, Any]]) -> List[str]:
        opcode_samples: List[str] = []
        seen_opcodes = set()
        for block in member_blocks:
            for instr in block.get("instructions", []):
                rendered = self._format_instruction(instr)
                if not rendered:
                    continue
                opcode = rendered.split()[-1]
                if opcode not in seen_opcodes:
                    opcode_samples.append(opcode)
                    seen_opcodes.add(opcode)
                if len(opcode_samples) >= 4:
                    break
            if len(opcode_samples) >= 4:
                break

        if not opcode_samples:
            return []
        if len(opcode_samples) == 1:
            return [opcode_samples[0]]
        return [" -> ".join(opcode_samples)]

    def _summarize_actions(self, actions: List[Dict[str, Any]]) -> List[str]:
        summaries = []
        for action in actions:
            action_type = action.get("action_type", "unknown")
            if action_type == "eth_transfer" and action.get("eth_event"):
                eth_event = action["eth_event"]
                summaries.append(
                    f"ETH transfer {self._display_address(eth_event.get('from'))} -> "
                    f"{self._display_address(eth_event.get('to'))} amount={eth_event.get('amount')}"
                )
            elif action.get("erc20_events"):
                for erc20_event in action["erc20_events"]:
                    summaries.append(
                        f"{erc20_event.get('type', 'erc20')} {erc20_event.get('tokenname', 'ERC20')} "
                        f"user={self._display_address(erc20_event.get('user'))} "
                        f"balance={erc20_event.get('balance')}"
                    )
            else:
                summaries.append(action_type)
        return summaries[:2]

    def _annotate_regions_with_llm(
        self,
        tx_hash: str,
        candidates: List[SemanticRegionCandidate],
    ) -> Dict[str, Dict[str, Any]]:
        assert self.client is not None

        grouped_candidates: "OrderedDict[str, List[SemanticRegionCandidate]]" = OrderedDict()
        for candidate in candidates:
            grouped_candidates.setdefault(candidate.contract_address, []).append(candidate)

        all_annotations: Dict[str, Dict[str, Any]] = {}
        for contract_address, contract_candidates in grouped_candidates.items():
            contract_name = contract_candidates[0].contract_name if contract_candidates else contract_address
            print(
                f"[INFO] Semantic CFG: annotating {len(contract_candidates)} regions for contract "
                f"{contract_name} ({contract_address})"
            )
            candidate_batches = self._split_candidates_into_batches(contract_candidates)
            for batch_idx, candidate_batch in enumerate(candidate_batches, start=1):
                print(
                    f"[INFO] Semantic CFG: contract {contract_name} batch "
                    f"{batch_idx}/{len(candidate_batches)} with {len(candidate_batch)} regions"
                )
                batch_annotations = self._annotate_candidate_batch(
                    tx_hash=tx_hash,
                    contract_address=contract_address,
                    contract_name=contract_name,
                    candidates=candidate_batch,
                )
                all_annotations.update(batch_annotations)

        return all_annotations

    def _split_candidates_into_batches(
        self,
        candidates: List[SemanticRegionCandidate],
    ) -> List[List[SemanticRegionCandidate]]:
        if self.batch_size <= 0 or len(candidates) <= self.batch_size:
            return [candidates]
        return [
            candidates[start:start + self.batch_size]
            for start in range(0, len(candidates), self.batch_size)
        ]

    def _annotate_candidate_batch(
        self,
        tx_hash: str,
        contract_address: str,
        contract_name: str,
        candidates: List[SemanticRegionCandidate],
    ) -> Dict[str, Dict[str, Any]]:
        payload = {
            "tx_hash": tx_hash,
            "contract_address": contract_address,
            "contract_name": contract_name,
            "task": SEMANTIC_TASK_DESCRIPTION,
            "regions": [candidate.to_prompt_dict() for candidate in candidates],
        }
        serialized_payload = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(serialized_payload) > self.max_prompt_chars:
            if len(candidates) <= 1:
                raise RuntimeError(
                    "Semantic CFG prompt too large even for a single region; "
                    "skip semantic view and keep folded CFG fallback."
                )

            mid = len(candidates) // 2
            left = candidates[:mid]
            right = candidates[mid:]
            print(
                f"[INFO] Semantic CFG: batch for {contract_name} too large ({len(serialized_payload)} chars), "
                f"splitting into {len(left)} + {len(right)} regions"
            )
            annotations: Dict[str, Dict[str, Any]] = {}
            annotations.update(self._annotate_candidate_batch(tx_hash, contract_address, contract_name, left))
            annotations.update(self._annotate_candidate_batch(tx_hash, contract_address, contract_name, right))
            return annotations

        output_text = self._request_semantic_labels(serialized_payload)
        if not output_text:
            raise RuntimeError("Semantic CFG LLM returned empty output.")

        parsed = self._parse_json_response(output_text)
        print(
            f"[OK] Semantic CFG: received labels for {len(candidates)} regions "
            f"from {contract_name} ({len(serialized_payload)} chars)"
        )
        return self._validate_annotations(candidates, parsed.get("regions", []))

    def _request_semantic_labels(self, serialized_payload: str) -> str:
        if not self.responses_api_supported:
            return self._request_semantic_labels_via_chat_completions(serialized_payload)

        try:
            response = self.client.responses.create(
                model=self.model,
                temperature=0.1,
                reasoning={"effort": "low"},
                input=serialized_payload,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "semantic_cfg_regions",
                        "strict": True,
                        "schema": SEMANTIC_RESPONSE_SCHEMA,
                    }
                },
            )
            return (response.output_text or "").strip()
        except (NotFoundError, APIStatusError) as exc:
            normalized_message = str(exc).lower()
            if not any(hint in normalized_message for hint in RESPONSES_API_FALLBACK_HINTS):
                raise
            self.responses_api_supported = False
            print("[INFO] Responses API unsupported by current base URL, falling back to chat.completions.")
            return self._request_semantic_labels_via_chat_completions(serialized_payload)

    def _request_semantic_labels_via_chat_completions(self, serialized_payload: str) -> str:
        messages = [
            {"role": "system", "content": SEMANTIC_SYSTEM_PROMPT},
            {"role": "user", "content": serialized_payload},
        ]
        request_kwargs: Dict[str, Any] = {
            "model": self.model,
            "temperature": 0.1,
            "messages": messages,
        }
        if self.reasoning_effort in SUPPORTED_REASONING_EFFORTS:
            request_kwargs["reasoning_effort"] = self.reasoning_effort
        try:
            completion = self._create_chat_completion(request_kwargs, include_json_response_format=True)
        except Exception as exc:
            message = str(exc).lower()
            if "reasoning_effort" in message:
                print("[INFO] chat.completions reasoning_effort unsupported, retrying without reasoning_effort.")
                request_kwargs.pop("reasoning_effort", None)
                completion = self._create_chat_completion(request_kwargs, include_json_response_format=True)
            elif "response_format" in message or "json_object" in message:
                print("[INFO] chat.completions response_format unsupported, retrying without response_format.")
                completion = self._create_chat_completion(request_kwargs, include_json_response_format=False)
            else:
                raise
        else:
            return self._extract_chat_completion_text(completion)

        return self._extract_chat_completion_text(completion)

    def _create_chat_completion(
        self,
        request_kwargs: Dict[str, Any],
        include_json_response_format: bool,
    ) -> Any:
        if include_json_response_format:
            return self.client.chat.completions.create(
                response_format={"type": "json_object"},
                **request_kwargs,
            )
        return self.client.chat.completions.create(**request_kwargs)

    def _extract_chat_completion_text(self, completion: Any) -> str:
        content = completion.choices[0].message.content
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(str(item.get("text", "")))
            return "".join(text_parts).strip()
        return str(content or "").strip()

    def _parse_json_response(self, raw_text: str) -> Dict[str, Any]:
        text = raw_text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start:end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                cleaned = re.sub(r",(\s*[}\]])", r"\1", candidate)
                return json.loads(cleaned)

        raise RuntimeError("Semantic CFG LLM returned malformed JSON that could not be repaired.")

    def _normalize_base_url(self, base_url: Optional[str]) -> Optional[str]:
        if not base_url:
            return None

        normalized = base_url.rstrip("/")
        suffixes = [
            "/v1/chat/completions",
            "/chat/completions",
            "/v1/responses",
            "/responses",
        ]
        for suffix in suffixes:
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)]
                break

        if not normalized.endswith("/v1"):
            normalized = f"{normalized}/v1"
        return normalized

    def _validate_annotations(
        self,
        candidates: List[SemanticRegionCandidate],
        annotations: List[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        candidate_map = {candidate.semantic_node_id: candidate for candidate in candidates}
        if len(candidate_map) != len(annotations):
            raise RuntimeError("Semantic CFG annotation count mismatch.")

        validated: Dict[str, Dict[str, Any]] = {}
        for item in annotations:
            semantic_node_id = str(item.get("semantic_node_id", "")).strip()
            candidate = candidate_map.get(semantic_node_id)
            if candidate is None:
                raise RuntimeError(f"Unknown semantic node id from LLM: {semantic_node_id}")

            member_block_ids = [int(block_id) for block_id in item.get("member_block_ids", [])]
            if sorted(member_block_ids) != sorted(candidate.member_block_ids):
                raise RuntimeError(f"LLM changed region membership for {semantic_node_id}")
            member_block_ids = candidate.member_block_ids

            label = str(item.get("label", "")).strip()
            purpose = str(item.get("purpose", "")).strip()
            confidence = float(item.get("confidence", 0))
            if not label or not purpose:
                raise RuntimeError(f"LLM returned empty semantic metadata for {semantic_node_id}")
            if confidence < self.min_confidence:
                raise RuntimeError(
                    f"LLM confidence {confidence:.2f} below threshold {self.min_confidence:.2f} for {semantic_node_id}"
                )

            validated[semantic_node_id] = {
                "semantic_node_id": semantic_node_id,
                "member_block_ids": member_block_ids,
                "label": label,
                "purpose": purpose,
                "confidence": confidence,
                "entry_conditions": [str(text).strip() for text in item.get("entry_conditions", []) if str(text).strip()],
                "exit_effects": [str(text).strip() for text in item.get("exit_effects", []) if str(text).strip()],
            }

        return validated

    def _assemble_payload(
        self,
        candidates: List[SemanticRegionCandidate],
        annotations: Dict[str, Dict[str, Any]],
        visible_edges: List[Any],
        edge_step_map: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        raw_to_semantic: Dict[str, str] = {}
        nodes_map: Dict[str, Any] = {}
        pair_to_edges: Dict[Tuple[str, str], Dict[str, Any]] = {}
        semantic_edge_step_map: Dict[str, Any] = {}

        for candidate in candidates:
            annotation = annotations[candidate.semantic_node_id]
            actions = []
            for block in candidate.member_blocks:
                actions.extend(block.get("actions", []))

            node_payload = {
                "semantic_node_id": candidate.semantic_node_id,
                "label": annotation["label"],
                "purpose": annotation["purpose"],
                "confidence": annotation["confidence"],
                "contract_address": candidate.contract_address,
                "contract_name": candidate.contract_name,
                "member_block_ids": candidate.member_block_ids,
                "blocks_number": len(candidate.member_block_ids),
                "start_pc": candidate.start_pc,
                "end_pc": candidate.end_pc,
                "gas": candidate.total_gas,
                "entry_conditions": annotation["entry_conditions"],
                "exit_effects": annotation["exit_effects"],
                "entry_edge_types": candidate.entry_edge_types,
                "exit_edge_types": candidate.exit_edge_types,
                "trace_step_range": candidate.trace_step_range,
                "actions": actions,
                "instruction_summary": candidate.instruction_summary,
                "action_summary": candidate.action_summary,
                "member_blocks": candidate.member_blocks,
            }
            nodes_map[candidate.semantic_node_id] = node_payload
            for block_id in candidate.member_block_ids:
                raw_to_semantic[str(block_id)] = candidate.semantic_node_id

        semantic_edge_counter = 1
        for edge in visible_edges:
            src_semantic = raw_to_semantic.get(str(edge.source.id))
            tgt_semantic = raw_to_semantic.get(str(edge.target.id))
            raw_edge_id = getattr(edge, "edge_id", "")
            edge_step = edge_step_map.get(raw_edge_id, {}).get("edge_step", getattr(edge, "edge_step", None))

            if src_semantic:
                semantic_edge_step_map[raw_edge_id] = {
                    "edge_id": raw_edge_id,
                    "edge_step": edge_step,
                    "source_node": src_semantic,
                    "target_node": tgt_semantic or src_semantic,
                }

            if not src_semantic or not tgt_semantic or src_semantic == tgt_semantic:
                continue

            pair_key = (src_semantic, tgt_semantic)
            if pair_key not in pair_to_edges:
                pair_to_edges[pair_key] = {
                    "edge_id": f"semantic_edge_{semantic_edge_counter}",
                    "source_node": src_semantic,
                    "target_node": tgt_semantic,
                    "edge_types": set(),
                    "raw_edge_ids": [],
                    "edge_steps": [],
                }
                semantic_edge_counter += 1

            pair_entry = pair_to_edges[pair_key]
            pair_entry["edge_types"].add(edge.edge_type)
            pair_entry["raw_edge_ids"].append(raw_edge_id)
            if isinstance(edge_step, int):
                pair_entry["edge_steps"].append(edge_step)

        semantic_edges = []
        for pair_entry in pair_to_edges.values():
            semantic_edges.append(
                {
                    "edge_id": pair_entry["edge_id"],
                    "source_node": pair_entry["source_node"],
                    "target_node": pair_entry["target_node"],
                    "edge_types": sorted(pair_entry["edge_types"]),
                    "raw_edge_ids": pair_entry["raw_edge_ids"],
                    "edge_steps": sorted(pair_entry["edge_steps"]),
                }
            )

        semantic_payload = {
            "mode": "semantic",
            "model": self.model,
            "nodes": nodes_map,
            "edges": semantic_edges,
            "raw_to_semantic": raw_to_semantic,
        }
        return semantic_payload, semantic_edge_step_map

    def _lookup_contract_name(self, address: str) -> str:
        if not address:
            return "Unknown"
        return self.full_name_map_lower.get(address.lower(), self._short_addr(address))

    def _display_address(self, value: Optional[str]) -> str:
        if not value:
            return "Unknown"
        value_str = str(value)
        return self.full_name_map_lower.get(value_str.lower(), self._short_addr(value_str))

    def _short_addr(self, address: str) -> str:
        if address.startswith("0x") and len(address) > 12:
            return f"{address[:8]}...{address[-4:]}"
        return address

    def _format_instruction(self, raw_instruction: Any) -> str:
        if isinstance(raw_instruction, (list, tuple)) and len(raw_instruction) == 2:
            return f"{raw_instruction[0]} {raw_instruction[1]}"

        text = str(raw_instruction).strip()
        if text.startswith("(") and text.endswith(")"):
            inner = text[1:-1]
            parts = [segment.strip().strip("'\"") for segment in inner.split(",", 1)]
            if len(parts) == 2:
                return f"{parts[0]} {parts[1]}"
        return text


def generate_and_export_semantic_cfg(
    cfg: Any,
    result_dir: str,
    full_address_name_map: Dict[str, str],
    erc20_token_map: Dict[str, Any],
    folded_blocks_map: Dict[str, Any],
    edge_step_map: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    builder = SemanticCFGBuilder(
        full_address_name_map=full_address_name_map,
        erc20_token_map=erc20_token_map,
        min_confidence=float(os.environ.get("OPENAI_SEMANTIC_CFG_MIN_CONFIDENCE", "0.45")),
    )
    semantic_result = builder.build(
        cfg=cfg,
        folded_blocks_map=folded_blocks_map,
        edge_step_map=edge_step_map,
    )
    if not semantic_result:
        return None

    semantic_cfg, semantic_edge_step_map = semantic_result
    builder.export(result_dir, semantic_cfg, semantic_edge_step_map)
    return semantic_cfg
