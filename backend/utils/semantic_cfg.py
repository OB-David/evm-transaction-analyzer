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


SEMANTIC_EDGE_TYPES = {"CALL", "DELEGATECALL"}
EXCEPTIONAL_TERMINATE_OPCODES = {"REVERT", "INVALID"}
NORMAL_RETURN_OPCODES = {"RETURN", "STOP"}
SUPPORTED_REASONING_EFFORTS = {"minimal", "low", "medium", "high", "none"}
RESPONSES_API_FALLBACK_HINTS = (
    "/chat/completions/responses",
    "invalid url",
    "not implemented",
    "convert_request_failed",
    "/responses",
)
SEMANTIC_TASK_DESCRIPTION = (
    "You are a DeFi and MEV security expert. Summarize EVM execution CFG blocks into high-level financial intent. "
    "Do not change block membership. Prefer labels such as Flashloan Borrow, Swap, AddLiquidity, "
    "Check Slippage, Repay Debt, Extract Profit, Balance Check, Risk Guard, Settlement. "
    "De-prioritize low-level execution wording such as dispatch, jump, route, gate, decode, pointer, or return handling "
    "unless it directly explains an exception or exploit path. "
    "If a region contains both routing and business logic, label the business logic. "
    "Use concise English labels, usually 2 to 5 words. "
    "The purpose should be one short sentence in trader-friendly language."
)
SEMANTIC_JSON_OUTPUT_HINT = (
    "{\"regions\":[{\"semantic_node_id\":string,\"member_block_ids\":number[],"
    "\"label\":string,\"purpose\":string,\"confidence\":number,"
    "\"entry_conditions\":string[],\"exit_effects\":string[]}]}"
)
SEMANTIC_SYSTEM_PROMPT = (
    "You are a DeFi and MEV arbitrage analyst. "
    "You will label pre-aggregated CFG regions without changing region membership. "
    "Your goal is to express business intent, not low-level execution details. "
    "Prioritize financial vocabulary: Flashloan Borrow, Swap/Trade, AddLiquidity, RemoveLiquidity, "
    "Check Slippage, Check Profitability, Repay Debt, Transfer Profit, Extract Profit, Revert Guard. "
    "Ignore most dispatch/jump/route/gate/decode/normal-return details unless they are the main reason of a failure branch. "
    "Use concise English labels (2 to 5 words) and one short purpose sentence. "
    "Return only valid JSON matching this shape: "
    f"{SEMANTIC_JSON_OUTPUT_HINT}."
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
    sequence_hint_step: Optional[int]
    contains_exceptional_terminate: bool
    is_routing_noise: bool
    instruction_summary: List[str]
    action_summary: List[str]
    block_opcode_sequences: List[Dict[str, Any]]
    trace_state_changes: List[Dict[str, Any]]
    decision_signals: Dict[str, Any]
    neighbor_context: Dict[str, List[Dict[str, Any]]]

    def to_prompt_dict(self) -> Dict[str, Any]:
        return {
            "semantic_node_id": self.semantic_node_id,
            "contract_name": self.contract_name,
            "member_block_ids": self.member_block_ids,
            "entry_edge_types": self.entry_edge_types,
            "exit_edge_types": self.exit_edge_types,
            "contains_exceptional_terminate": self.contains_exceptional_terminate,
            "is_routing_noise": self.is_routing_noise,
            "block_opcode_sequences": self.block_opcode_sequences,
            "trace_state_changes": self.trace_state_changes,
            "decision_signals": self.decision_signals,
            "neighbor_context": self.neighbor_context,
        }


class SemanticCFGBuilder:
    """Build a rule-constrained semantic CFG and let the model annotate regions."""

    def __init__(
        self,
        full_address_name_map: Dict[str, str],
        erc20_token_map: Dict[str, Any],
        semantic_background: Optional[Dict[str, Any]] = None,
        trace_steps: Optional[List[Dict[str, Any]]] = None,
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
        self.model = model or os.environ.get("OPENAI_SEMANTIC_CFG_MODEL", "gpt-5.4-nano")
        api_key = api_key or os.environ.get("OPENAI_API_KEY")
        base_url = base_url or os.environ.get("OPENAI_BASE_URL")
        self.base_url = self._normalize_base_url(base_url)
        timeout_seconds = timeout_seconds or float(
            os.environ.get("OPENAI_SEMANTIC_CFG_TIMEOUT_SECONDS", "45")
        )
        self.max_prompt_chars = int(os.environ.get("OPENAI_SEMANTIC_CFG_MAX_PROMPT_CHARS", "120000"))
        self.batch_size = int(os.environ.get("OPENAI_SEMANTIC_CFG_BATCH_SIZE", "8"))
        self.coarse_group_size = int(os.environ.get("OPENAI_SEMANTIC_CFG_COARSE_GROUP_SIZE", "18"))
        self.target_node_count = int(os.environ.get("OPENAI_SEMANTIC_CFG_TARGET_NODE_COUNT", "28"))
        self.reasoning_effort = os.environ.get("OPENAI_SEMANTIC_CFG_REASONING_EFFORT", "minimal").strip().lower()
        self.api_mode = os.environ.get("OPENAI_SEMANTIC_CFG_API_MODE", "auto").strip().lower()
        self.responses_api_supported = self.api_mode != "chat_completions"
        self.semantic_background = semantic_background or {}
        self.trace_steps = trace_steps or []
        self.trace_steps_by_address: Dict[str, List[Tuple[int, Dict[str, Any], Optional[int]]]] = defaultdict(list)
        for idx, step in enumerate(self.trace_steps):
            address = str(step.get("address", "")).lower()
            if not address:
                continue
            pc_int = self._pc_to_int(step.get("pc"))
            self.trace_steps_by_address[address].append((idx, step, pc_int))
        if "OPENAI_SEMANTIC_CFG_COARSE_GROUP_SIZE" in os.environ:
            print("[INFO] OPENAI_SEMANTIC_CFG_COARSE_GROUP_SIZE is deprecated and ignored in single-pass aggregation.")
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

        candidates = self._aggregate_candidates_once(visible_nodes, visible_edges, folded_blocks_map, edge_step_map)
        if not candidates:
            print("[INFO] No semantic region candidates found, skip semantic CFG generation.")
            return None
        print(f"[INFO] Semantic CFG: aggregated to {len(candidates)} semantic regions before LLM labeling.")

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

    def _aggregate_candidates_once(
        self,
        visible_nodes: Dict[int, FoldableBlockNode],
        visible_edges: List[Any],
        folded_blocks_map: Dict[str, Any],
        edge_step_map: Dict[str, Any],
    ) -> List[SemanticRegionCandidate]:
        out_edges: Dict[int, List[Any]] = defaultdict(list)
        in_edges: Dict[int, List[Any]] = defaultdict(list)
        for edge in visible_edges:
            out_edges[edge.source.id].append(edge)
            in_edges[edge.target.id].append(edge)

        ordered_nodes = self._build_execution_order(visible_nodes, visible_edges, edge_step_map)
        if not ordered_nodes:
            return []

        groups: List[List[FoldableBlockNode]] = []
        idx = 0
        while idx < len(ordered_nodes):
            start = ordered_nodes[idx]
            if self._is_hard_boundary_node(start, in_edges[start.id], out_edges[start.id]):
                groups.append([start])
                idx += 1
                continue

            current_group = [start]
            idx += 1
            while idx < len(ordered_nodes):
                node = ordered_nodes[idx]
                if node.address != start.address:
                    break
                if self._is_hard_boundary_node(node, in_edges[node.id], out_edges[node.id]):
                    break
                current_group.append(node)
                idx += 1

            groups.append(current_group)

        candidates: List[SemanticRegionCandidate] = []
        for region_idx, member_nodes in enumerate(groups, start=1):
            candidate = self._build_candidate(
                semantic_node_id=f"semantic_{region_idx}",
                member_nodes=member_nodes,
                in_edges=in_edges,
                out_edges=out_edges,
                folded_blocks_map=folded_blocks_map,
                edge_step_map=edge_step_map,
            )
            candidates.append(candidate)

        candidates = self._absorb_routing_noise_candidates(candidates)
        candidates = self._merge_candidates_until_limit(candidates)
        candidates = self._renumber_candidates(candidates)
        self._attach_neighbor_context(candidates, visible_edges, edge_step_map)
        return candidates

    def _build_execution_order(
        self,
        visible_nodes: Dict[int, FoldableBlockNode],
        visible_edges: List[Any],
        edge_step_map: Dict[str, Any],
    ) -> List[FoldableBlockNode]:
        node_step_hint: Dict[str, int] = {}
        for edge in visible_edges:
            raw_edge_id = getattr(edge, "edge_id", "")
            edge_step = edge_step_map.get(raw_edge_id, {}).get("edge_step", getattr(edge, "edge_step", None))
            if not isinstance(edge_step, int):
                continue
            for node_id in (str(edge.source.id), str(edge.target.id)):
                existing = node_step_hint.get(node_id)
                if existing is None or edge_step < existing:
                    node_step_hint[node_id] = edge_step

        def sort_key(node: FoldableBlockNode) -> Tuple[int, str]:
            node_id = str(node.id)
            return (node_step_hint.get(node_id, 10**12), node_id)

        return sorted(visible_nodes.values(), key=sort_key)

    def _is_hard_boundary_node(
        self,
        node: FoldableBlockNode,
        in_edges: List[Any],
        out_edges: List[Any],
    ) -> bool:
        actions = node.fold_info.get("actions", []) if hasattr(node, "fold_info") else node.actions
        if actions:
            return True
        if self._contains_exceptional_terminate(node):
            return True
        all_incident_edges = in_edges + out_edges
        if any(edge.edge_type in SEMANTIC_EDGE_TYPES for edge in all_incident_edges):
            return True
        if any(edge.source.address != edge.target.address for edge in all_incident_edges):
            return True
        return False

    def _is_routing_noise_node(self, node: FoldableBlockNode) -> bool:
        actions = node.fold_info.get("actions", []) if hasattr(node, "fold_info") else node.actions
        if actions:
            return False
        if self._contains_exceptional_terminate(node):
            return False
        opcodes = self._extract_node_opcodes(node)
        if not opcodes:
            return True

        routing_keywords = {
            "JUMP",
            "JUMPI",
            "JUMPDEST",
            "CALLDATALOAD",
            "CALLDATASIZE",
            "CALLDATACOPY",
            "RETURNDATASIZE",
            "RETURNDATACOPY",
            "RETURN",
            "STOP",
            "DISPATCH_LOGIC_SINK",
            "MERGE_POINT_SEGMENT",
            "SELF_LOOP_DETECTED",
        }

        low_signal = 0
        for opcode in opcodes:
            normalized = opcode.upper()
            if (
                normalized in routing_keywords
                or normalized.startswith("BRANCH_SEGMENT_")
                or normalized.startswith("FEEDBACK_LOOP_")
                or normalized.startswith("PUSH")
                or normalized.startswith("DUP")
                or normalized.startswith("SWAP")
            ):
                low_signal += 1
        return low_signal / max(len(opcodes), 1) >= 0.7

    def _contains_exceptional_terminate(self, node: FoldableBlockNode) -> bool:
        opcodes = self._extract_node_opcodes(node)
        return any(opcode in EXCEPTIONAL_TERMINATE_OPCODES for opcode in opcodes)

    def _extract_node_opcodes(self, node: FoldableBlockNode) -> List[str]:
        opcodes: List[str] = []
        for raw_instruction in getattr(node, "instructions", []):
            opcode = self._extract_opcode(raw_instruction)
            if opcode:
                opcodes.append(opcode)
        return opcodes

    def _extract_opcode(self, raw_instruction: Any) -> str:
        if isinstance(raw_instruction, (list, tuple)) and len(raw_instruction) >= 2:
            return str(raw_instruction[1]).strip().upper()
        if isinstance(raw_instruction, dict):
            value = str(raw_instruction.get("opcode", "")).strip().upper()
            return value
        if isinstance(raw_instruction, str):
            text = raw_instruction.strip()
            if text.startswith("{") and "opcode" in text:
                match = re.search(r"['\"]opcode['\"]\s*:\s*['\"]([^'\"]+)['\"]", text)
                if match:
                    return match.group(1).strip().upper()
        rendered = self._format_instruction(raw_instruction)
        if not rendered:
            return ""
        parts = rendered.split()
        if not parts:
            return ""
        return str(parts[-1]).strip().upper()

    def _absorb_routing_noise_candidates(
        self,
        candidates: List[SemanticRegionCandidate],
    ) -> List[SemanticRegionCandidate]:
        if len(candidates) <= 1:
            return candidates

        merged = list(candidates)
        changed = True
        while changed and len(merged) > 1:
            changed = False
            for idx, candidate in enumerate(list(merged)):
                if not candidate.is_routing_noise or candidate.contains_exceptional_terminate:
                    continue
                if candidate.action_summary:
                    continue

                prev_idx = idx - 1 if idx > 0 else None
                next_idx = idx + 1 if idx + 1 < len(merged) else None
                target_idx: Optional[int] = None

                for probe_idx in (next_idx, prev_idx):
                    if probe_idx is None:
                        continue
                    probe = merged[probe_idx]
                    if probe.contract_address != candidate.contract_address:
                        continue
                    if probe.contains_exceptional_terminate:
                        continue
                    if not probe.is_routing_noise:
                        target_idx = probe_idx
                        break

                if target_idx is None:
                    for probe_idx in (next_idx, prev_idx):
                        if probe_idx is None:
                            continue
                        probe = merged[probe_idx]
                        if probe.contract_address != candidate.contract_address:
                            continue
                        if probe.contains_exceptional_terminate:
                            continue
                        target_idx = probe_idx
                        break

                if target_idx is None:
                    continue

                left_idx, right_idx = sorted([idx, target_idx])
                merged_candidate = self._merge_candidate_pair(merged[left_idx], merged[right_idx])
                merged = merged[:left_idx] + [merged_candidate] + merged[right_idx + 1:]
                changed = True
                break

        return merged

    def _merge_candidates_until_limit(
        self,
        candidates: List[SemanticRegionCandidate],
    ) -> List[SemanticRegionCandidate]:
        if self.target_node_count <= 0 or len(candidates) <= self.target_node_count:
            return candidates

        merged = list(candidates)
        while len(merged) > self.target_node_count:
            pair_idx = self._find_best_merge_pair(merged, allow_cross_contract=False)
            if pair_idx is None:
                pair_idx = self._find_best_merge_pair(merged, allow_cross_contract=True)
            if pair_idx is None:
                break
            left_idx, right_idx = pair_idx
            merged_candidate = self._merge_candidate_pair(merged[left_idx], merged[right_idx])
            merged = merged[:left_idx] + [merged_candidate] + merged[right_idx + 1:]
        return merged

    def _find_best_merge_pair(
        self,
        candidates: List[SemanticRegionCandidate],
        allow_cross_contract: bool,
    ) -> Optional[Tuple[int, int]]:
        best_pair: Optional[Tuple[int, int]] = None
        best_score: Optional[float] = None
        for idx in range(len(candidates) - 1):
            left = candidates[idx]
            right = candidates[idx + 1]
            if not allow_cross_contract and left.contract_address != right.contract_address:
                continue
            score = self._merge_cost(left, right)
            if best_score is None or score < best_score:
                best_score = score
                best_pair = (idx, idx + 1)
        return best_pair

    def _merge_cost(
        self,
        left: SemanticRegionCandidate,
        right: SemanticRegionCandidate,
    ) -> float:
        score = 0.0
        if left.contains_exceptional_terminate or right.contains_exceptional_terminate:
            score += 10_000
        if left.action_summary:
            score += 30
        if right.action_summary:
            score += 30
        if left.contract_address != right.contract_address:
            score += 200
        if left.is_routing_noise:
            score -= 8
        if right.is_routing_noise:
            score -= 8
        score += (len(left.member_block_ids) + len(right.member_block_ids)) / 16
        return score

    def _merge_candidate_pair(
        self,
        left: SemanticRegionCandidate,
        right: SemanticRegionCandidate,
    ) -> SemanticRegionCandidate:
        entry_step_values = [
            c.trace_step_range.get("entry_step")
            for c in (left, right)
            if c.trace_step_range.get("entry_step") is not None
        ]
        exit_step_values = [
            c.trace_step_range.get("exit_step")
            for c in (left, right)
            if c.trace_step_range.get("exit_step") is not None
        ]
        sequence_hints = [c.sequence_hint_step for c in (left, right) if c.sequence_hint_step is not None]

        return SemanticRegionCandidate(
            semantic_node_id=left.semantic_node_id,
            contract_address=(
                left.contract_address
                if left.contract_address == right.contract_address
                else "mixed"
            ),
            contract_name=(
                left.contract_name
                if left.contract_name == right.contract_name
                else "Cross-Contract"
            ),
            member_block_ids=left.member_block_ids + right.member_block_ids,
            member_blocks=left.member_blocks + right.member_blocks,
            start_pc=left.start_pc,
            end_pc=right.end_pc,
            total_gas=left.total_gas + right.total_gas,
            entry_edge_types=sorted(set(left.entry_edge_types + right.entry_edge_types)),
            exit_edge_types=sorted(set(left.exit_edge_types + right.exit_edge_types)),
            trace_step_range={
                "entry_step": min(entry_step_values) if entry_step_values else None,
                "exit_step": max(exit_step_values) if exit_step_values else None,
            },
            sequence_hint_step=min(sequence_hints) if sequence_hints else None,
            contains_exceptional_terminate=(
                left.contains_exceptional_terminate or right.contains_exceptional_terminate
            ),
            is_routing_noise=left.is_routing_noise and right.is_routing_noise,
            instruction_summary=self._merge_instruction_summaries(left.instruction_summary + right.instruction_summary),
            action_summary=(left.action_summary + right.action_summary)[:3],
            block_opcode_sequences=left.block_opcode_sequences + right.block_opcode_sequences,
            trace_state_changes=left.trace_state_changes + right.trace_state_changes,
            decision_signals=self._merge_decision_signals(left.decision_signals, right.decision_signals),
            neighbor_context={"previous": [], "next": []},
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
                if len(opcodes) >= 6:
                    break
            if len(opcodes) >= 6:
                break
        if not opcodes:
            return []
        if len(opcodes) == 1:
            return [opcodes[0]]
        return [" -> ".join(opcodes)]

    def _merge_decision_signals(
        self,
        left: Dict[str, Any],
        right: Dict[str, Any],
    ) -> Dict[str, Any]:
        merged = {
            "opcode_focus_counts": {},
            "external_calls": [],
            "state_reads": [],
            "state_writes": [],
            "token_actions": [],
            "terminal_behavior": {
                "has_revert": False,
                "has_invalid": False,
                "has_return": False,
                "has_stop": False,
            },
        }

        for source in (left, right):
            counts = source.get("opcode_focus_counts", {})
            for opcode, value in counts.items():
                merged["opcode_focus_counts"][opcode] = merged["opcode_focus_counts"].get(opcode, 0) + int(value)

            merged["external_calls"].extend(source.get("external_calls", []))
            merged["state_reads"].extend(source.get("state_reads", []))
            merged["state_writes"].extend(source.get("state_writes", []))
            merged["token_actions"].extend(source.get("token_actions", []))

            terminal = source.get("terminal_behavior", {})
            for key in merged["terminal_behavior"]:
                merged["terminal_behavior"][key] = bool(
                    merged["terminal_behavior"][key] or terminal.get(key, False)
                )

        merged["external_calls"] = merged["external_calls"][:128]
        merged["state_reads"] = merged["state_reads"][:128]
        merged["state_writes"] = merged["state_writes"][:128]
        merged["token_actions"] = merged["token_actions"][:128]
        return merged

    def _attach_neighbor_context(
        self,
        candidates: List[SemanticRegionCandidate],
        visible_edges: List[Any],
        edge_step_map: Dict[str, Any],
    ) -> None:
        block_to_candidate: Dict[int, str] = {}
        candidate_map: Dict[str, SemanticRegionCandidate] = {}
        for candidate in candidates:
            candidate_map[candidate.semantic_node_id] = candidate
            candidate.neighbor_context = {"previous": [], "next": []}
            for block_id in candidate.member_block_ids:
                block_to_candidate[block_id] = candidate.semantic_node_id

        pair_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for edge in visible_edges:
            src_id = block_to_candidate.get(edge.source.id)
            tgt_id = block_to_candidate.get(edge.target.id)
            if not src_id or not tgt_id or src_id == tgt_id:
                continue
            pair_key = (src_id, tgt_id)
            step = edge_step_map.get(getattr(edge, "edge_id", ""), {}).get("edge_step", getattr(edge, "edge_step", None))
            if pair_key not in pair_map:
                pair_map[pair_key] = {"edge_types": set(), "steps": []}
            pair_map[pair_key]["edge_types"].add(edge.edge_type)
            if isinstance(step, int):
                pair_map[pair_key]["steps"].append(step)

        for (src_id, tgt_id), payload in pair_map.items():
            src_candidate = candidate_map[src_id]
            tgt_candidate = candidate_map[tgt_id]
            next_entry = {
                "semantic_node_id": tgt_id,
                "edge_types": sorted(payload["edge_types"]),
                "step_hint": min(payload["steps"]) if payload["steps"] else None,
                "contains_exceptional_terminate": tgt_candidate.contains_exceptional_terminate,
                "is_routing_noise": tgt_candidate.is_routing_noise,
                "block_opcode_sequences": tgt_candidate.block_opcode_sequences,
            }
            prev_entry = {
                "semantic_node_id": src_id,
                "edge_types": sorted(payload["edge_types"]),
                "step_hint": min(payload["steps"]) if payload["steps"] else None,
                "contains_exceptional_terminate": src_candidate.contains_exceptional_terminate,
                "is_routing_noise": src_candidate.is_routing_noise,
                "block_opcode_sequences": src_candidate.block_opcode_sequences,
            }
            src_candidate.neighbor_context["next"].append(next_entry)
            tgt_candidate.neighbor_context["previous"].append(prev_entry)

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
                    sequence_hint_step=candidate.sequence_hint_step,
                    contains_exceptional_terminate=candidate.contains_exceptional_terminate,
                    is_routing_noise=candidate.is_routing_noise,
                    instruction_summary=candidate.instruction_summary,
                    action_summary=candidate.action_summary,
                    block_opcode_sequences=candidate.block_opcode_sequences,
                    trace_state_changes=candidate.trace_state_changes,
                    decision_signals=candidate.decision_signals,
                    neighbor_context=candidate.neighbor_context,
                )
            )
        return renumbered

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
        has_exceptional = any(self._contains_exceptional_terminate(node) for node in member_nodes)

        start_pc = member_blocks[0].get("start_pc", member_nodes[0].start_pc) if member_blocks else member_nodes[0].start_pc
        end_pc = (
            member_blocks[-1].get("end_pc", member_nodes[-1].fold_info.get("end_pc", member_nodes[-1].end_pc))
            if member_blocks else
            member_nodes[-1].fold_info.get("end_pc", member_nodes[-1].end_pc)
        )
        routing_noise_flags = [self._is_routing_noise_node(node) for node in member_nodes]
        block_opcode_sequences = self._build_block_opcode_sequences(member_blocks)
        trace_state_changes = self._build_trace_state_changes(member_nodes)
        decision_signals = self._build_decision_signals(trace_state_changes, actions)

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
            sequence_hint_step=min(step_values) if step_values else None,
            contains_exceptional_terminate=has_exceptional,
            is_routing_noise=all(routing_noise_flags) if routing_noise_flags else True,
            instruction_summary=self._summarize_instructions(member_blocks),
            action_summary=self._summarize_actions(actions),
            block_opcode_sequences=block_opcode_sequences,
            trace_state_changes=trace_state_changes,
            decision_signals=decision_signals,
            neighbor_context={"previous": [], "next": []},
        )

    def _build_trace_state_changes(self, member_nodes: List[FoldableBlockNode]) -> List[Dict[str, Any]]:
        if not self.trace_steps:
            return []

        ranges_by_address: Dict[str, List[Tuple[int, int]]] = defaultdict(list)
        for node in member_nodes:
            expanded_blocks = getattr(node, "folded_blocks", None) or [node]
            for block in expanded_blocks:
                address = str(getattr(block, "address", "")).lower()
                start_pc = self._pc_to_int(getattr(block, "start_pc", None))
                end_pc = self._pc_to_int(getattr(block, "end_pc", None))
                if not address or start_pc is None or end_pc is None:
                    continue
                ranges_by_address[address].append((start_pc, end_pc))

        matched_indices: set[int] = set()
        for address, ranges in ranges_by_address.items():
            for idx, _step, pc_int in self.trace_steps_by_address.get(address, []):
                if pc_int is None:
                    continue
                for start_pc, end_pc in ranges:
                    if start_pc <= pc_int <= end_pc:
                        matched_indices.add(idx)
                        break

        result: List[Dict[str, Any]] = []
        for idx in sorted(matched_indices):
            step = self.trace_steps[idx]
            prev_step = self.trace_steps[idx - 1] if idx > 0 else None
            current_stack = step.get("stack", [])
            current_memory = step.get("memory", [])
            prev_stack = prev_step.get("stack", []) if prev_step else []
            prev_memory = prev_step.get("memory", []) if prev_step else []
            result.append(
                {
                    "trace_index": idx,
                    "address": step.get("address"),
                    "pc": step.get("pc"),
                    "opcode": step.get("opcode"),
                    "stack": current_stack,
                    "memory": current_memory,
                    "stack_change": {
                        "before": prev_stack,
                        "after": current_stack,
                    },
                    "memory_change": {
                        "before": prev_memory,
                        "after": current_memory,
                    },
                }
            )
        return result

    def _build_decision_signals(
        self,
        trace_state_changes: List[Dict[str, Any]],
        actions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        focus_opcodes = {
            "CALL",
            "DELEGATECALL",
            "STATICCALL",
            "SLOAD",
            "SSTORE",
            "LOG0",
            "LOG1",
            "LOG2",
            "LOG3",
            "LOG4",
            "REVERT",
            "INVALID",
            "RETURN",
            "STOP",
        }
        opcode_focus_counts: Dict[str, int] = {}
        external_calls: List[Dict[str, Any]] = []
        state_reads: List[Dict[str, Any]] = []
        state_writes: List[Dict[str, Any]] = []
        token_actions: List[Dict[str, Any]] = []

        terminal_behavior = {
            "has_revert": False,
            "has_invalid": False,
            "has_return": False,
            "has_stop": False,
        }

        for step in trace_state_changes:
            opcode = str(step.get("opcode", "")).upper()
            stack = step.get("stack") or []
            trace_index = step.get("trace_index")
            if opcode in focus_opcodes:
                opcode_focus_counts[opcode] = opcode_focus_counts.get(opcode, 0) + 1

            if opcode in {"CALL", "DELEGATECALL", "STATICCALL"}:
                callee = stack[-2] if len(stack) >= 2 else None
                call_value = stack[-3] if len(stack) >= 3 else None
                external_calls.append(
                    {
                        "trace_index": trace_index,
                        "opcode": opcode,
                        "callee_raw": callee,
                        "callee_name": self._display_address(callee) if callee else None,
                        "value_raw": call_value,
                        "value_int": self._hex_to_int(call_value),
                    }
                )
            elif opcode == "SLOAD":
                slot = stack[-1] if len(stack) >= 1 else None
                state_reads.append(
                    {
                        "trace_index": trace_index,
                        "slot_raw": slot,
                        "slot_int": self._hex_to_int(slot),
                    }
                )
            elif opcode == "SSTORE":
                slot = stack[-1] if len(stack) >= 1 else None
                value = stack[-2] if len(stack) >= 2 else None
                state_writes.append(
                    {
                        "trace_index": trace_index,
                        "slot_raw": slot,
                        "slot_int": self._hex_to_int(slot),
                        "value_raw": value,
                        "value_int": self._hex_to_int(value),
                    }
                )

            if opcode == "REVERT":
                terminal_behavior["has_revert"] = True
            elif opcode == "INVALID":
                terminal_behavior["has_invalid"] = True
            elif opcode == "RETURN":
                terminal_behavior["has_return"] = True
            elif opcode == "STOP":
                terminal_behavior["has_stop"] = True

        for action in actions:
            action_type = action.get("action_type", "unknown")
            if action_type == "eth_transfer" and action.get("eth_event"):
                eth_event = action["eth_event"]
                token_actions.append(
                    {
                        "type": "eth_transfer",
                        "from": self._display_address(eth_event.get("from")),
                        "to": self._display_address(eth_event.get("to")),
                        "amount_raw": eth_event.get("amount"),
                    }
                )
            else:
                for erc20_event in action.get("erc20_events", []):
                    token_actions.append(
                        {
                            "type": erc20_event.get("type", "erc20"),
                            "token": erc20_event.get("tokenname", "ERC20"),
                            "user": self._display_address(erc20_event.get("user")),
                            "balance_raw": erc20_event.get("balance"),
                        }
                    )

        return {
            "opcode_focus_counts": opcode_focus_counts,
            "external_calls": external_calls,
            "state_reads": state_reads,
            "state_writes": state_writes,
            "token_actions": token_actions,
            "terminal_behavior": terminal_behavior,
        }

    def _build_block_opcode_sequences(self, member_blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        sequences: List[Dict[str, Any]] = []
        for block in member_blocks:
            raw_instructions = block.get("instructions", [])
            opcodes: List[str] = []
            for instruction in raw_instructions:
                opcode = self._extract_opcode(instruction)
                if opcode:
                    opcodes.append(opcode)
            sequences.append(
                {
                    "block_id": block.get("block_id"),
                    "pc_range": f"{block.get('start_pc')} - {block.get('end_pc')}",
                    "opcodes": opcodes,
                    "actions": block.get("actions", []),
                }
            )
        return sequences

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
                opcode = self._extract_opcode(instr)
                if not opcode:
                    continue
                if opcode.startswith("PUSH") or opcode.startswith("DUP") or opcode.startswith("SWAP"):
                    continue
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
        prompt_background = self._build_prompt_background(contract_address)
        payload = {
            "tx_hash": tx_hash,
            "contract_address": contract_address,
            "contract_name": contract_name,
            "task": SEMANTIC_TASK_DESCRIPTION,
            "transaction_background": prompt_background,
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

    def _build_prompt_background(self, contract_address: str) -> Dict[str, Any]:
        if not self.semantic_background:
            return {}
        background = dict(self.semantic_background)
        contract_flows = background.get("flows_by_contract", {})
        if isinstance(contract_flows, dict):
            background["contract_flow_focus"] = contract_flows.get(contract_address, [])
        return background

    def _request_semantic_labels(self, serialized_payload: str) -> str:
        if not self.responses_api_supported:
            return self._request_semantic_labels_via_chat_completions(serialized_payload)

        try:
            response = self.client.responses.create(
                model=self.model,
                temperature=0.1,
                reasoning={"effort": "low"},
                input=[
                    {"role": "system", "content": SEMANTIC_SYSTEM_PROMPT},
                    {"role": "user", "content": serialized_payload},
                ],
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

            member_block_ids = [str(block_id) for block_id in item.get("member_block_ids", [])]
            candidate_member_block_ids = [str(block_id) for block_id in candidate.member_block_ids]
            if sorted(member_block_ids) != sorted(candidate_member_block_ids):
                raise RuntimeError(f"LLM changed region membership for {semantic_node_id}")
            member_block_ids = candidate.member_block_ids

            label = str(item.get("label", "")).strip()
            purpose = str(item.get("purpose", "")).strip()
            try:
                confidence = float(item.get("confidence", 0))
            except (TypeError, ValueError):
                confidence = 0.0
            if not label or not purpose:
                raise RuntimeError(f"LLM returned empty semantic metadata for {semantic_node_id}")
            if confidence < self.min_confidence:
                print(
                    f"[WARN] LLM confidence {confidence:.2f} below threshold "
                    f"{self.min_confidence:.2f} for {semantic_node_id}; keep result."
                )

            validated[semantic_node_id] = {
                "semantic_node_id": semantic_node_id,
                "member_block_ids": member_block_ids,
                "label": label,
                "purpose": purpose,
                "confidence": confidence,
                "low_confidence": confidence < self.min_confidence,
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
                "low_confidence": annotation.get("low_confidence", False),
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
                "sequence_hint_step": candidate.sequence_hint_step,
                "actions": actions,
                "block_opcode_sequences": candidate.block_opcode_sequences,
                "trace_state_changes": candidate.trace_state_changes,
                "decision_signals": candidate.decision_signals,
                "neighbor_context": candidate.neighbor_context,
                "contains_exceptional_terminate": candidate.contains_exceptional_terminate,
                "is_routing_noise": candidate.is_routing_noise,
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
            min_step = min(pair_entry["edge_steps"]) if pair_entry["edge_steps"] else None
            semantic_edges.append(
                {
                    "edge_id": pair_entry["edge_id"],
                    "source_node": pair_entry["source_node"],
                    "target_node": pair_entry["target_node"],
                    "edge_types": sorted(pair_entry["edge_types"]),
                    "raw_edge_ids": pair_entry["raw_edge_ids"],
                    "edge_steps": sorted(pair_entry["edge_steps"]),
                    "min_edge_step": min_step,
                    "is_primary_path": False,
                }
            )

        semantic_edges.sort(
            key=lambda edge: (
                edge.get("min_edge_step", 10**12) if isinstance(edge.get("min_edge_step"), int) else 10**12,
                edge.get("edge_id", ""),
            )
        )

        sequence_map = self._compute_sequence_indices(candidates)
        for semantic_node_id, sequence_index in sequence_map.items():
            if semantic_node_id in nodes_map:
                nodes_map[semantic_node_id]["sequence_index"] = sequence_index

        primary_edge_ids = self._compute_primary_path_edge_ids(semantic_edges, sequence_map)
        for edge in semantic_edges:
            if edge["edge_id"] in primary_edge_ids:
                edge["is_primary_path"] = True

        semantic_payload = {
            "mode": "semantic",
            "model": self.model,
            "nodes": nodes_map,
            "edges": semantic_edges,
            "raw_to_semantic": raw_to_semantic,
            "background": self.semantic_background,
        }
        return semantic_payload, semantic_edge_step_map

    def _compute_sequence_indices(
        self,
        candidates: List[SemanticRegionCandidate],
    ) -> Dict[str, int]:
        sorted_candidates = sorted(
            candidates,
            key=lambda c: (
                c.trace_step_range.get("entry_step")
                if isinstance(c.trace_step_range.get("entry_step"), int)
                else 10**12,
                c.sequence_hint_step if isinstance(c.sequence_hint_step, int) else 10**12,
                c.semantic_node_id,
            ),
        )
        return {
            candidate.semantic_node_id: idx
            for idx, candidate in enumerate(sorted_candidates, start=1)
        }

    def _compute_primary_path_edge_ids(
        self,
        semantic_edges: List[Dict[str, Any]],
        sequence_map: Dict[str, int],
    ) -> set[str]:
        if not semantic_edges or not sequence_map:
            return set()

        outgoing: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for edge in semantic_edges:
            outgoing[edge["source_node"]].append(edge)

        for source_node in outgoing:
            outgoing[source_node].sort(
                key=lambda edge: (
                    edge.get("min_edge_step", 10**12) if isinstance(edge.get("min_edge_step"), int) else 10**12,
                    sequence_map.get(edge.get("target_node", ""), 10**12),
                    edge.get("edge_id", ""),
                )
            )

        start_node = min(sequence_map.items(), key=lambda item: item[1])[0]
        current = start_node
        visited_nodes = {current}
        primary_edge_ids: set[str] = set()

        while True:
            candidates = outgoing.get(current, [])
            next_edge = None
            current_seq = sequence_map.get(current, 10**12)
            for edge in candidates:
                target = edge.get("target_node")
                target_seq = sequence_map.get(target, 10**12)
                if target in visited_nodes:
                    continue
                if target_seq < current_seq:
                    continue
                next_edge = edge
                break
            if next_edge is None:
                break
            primary_edge_ids.add(next_edge["edge_id"])
            current = next_edge["target_node"]
            visited_nodes.add(current)

        return primary_edge_ids

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

    def _pc_to_int(self, value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            text = str(value).strip()
            if text.lower().startswith("0x"):
                return int(text, 16)
            return int(text)
        except Exception:
            return None

    def _hex_to_int(self, value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            text = str(value).strip()
            if not text:
                return None
            if text.lower().startswith("0x"):
                return int(text, 16)
            return int(text)
        except Exception:
            return None

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
    semantic_background: Optional[Dict[str, Any]] = None,
    trace_steps: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    builder = SemanticCFGBuilder(
        full_address_name_map=full_address_name_map,
        erc20_token_map=erc20_token_map,
        semantic_background=semantic_background,
        trace_steps=trace_steps,
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


def build_semantic_background(
    paired: List[Dict[str, Any]],
    pending_erc20: Dict[str, Dict[str, Any]],
    edge_link: List[Dict[str, Any]],
    arb_result: Optional[Dict[str, Any]] = None,
    address_balances: Optional[Dict[str, Dict[str, float]]] = None,
    full_address_name_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    full_name_map_lower = {
        str(addr).lower(): name
        for addr, name in (full_address_name_map or {}).items()
    }

    def _display_address(value: Optional[str]) -> str:
        if not value:
            return "Unknown"
        value_str = str(value)
        alias = full_name_map_lower.get(value_str.lower())
        if alias:
            return alias
        if value_str.startswith("0x") and len(value_str) > 12:
            return f"{value_str[:8]}...{value_str[-4:]}"
        return value_str

    major_flows: List[Dict[str, Any]] = []
    flows_by_contract: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for flow in sorted(paired or [], key=lambda item: int(item.get("order", 0))):
        order = int(flow.get("order", 0))
        if order <= 0:
            continue
        token = flow.get("token")
        amount = flow.get("amount")
        item = {
            "order": order,
            "token": token,
            "amount": amount,
            "from": _display_address(flow.get("from")),
            "to": _display_address(flow.get("to")),
        }
        major_flows.append(item)
        for contract_key in ("codecontract_address", "from_codecontract", "to_codecontract"):
            contract_addr = flow.get(contract_key)
            if not contract_addr:
                continue
            flows_by_contract[str(contract_addr)].append(item)

    for v in sorted((pending_erc20 or {}).values(), key=lambda item: int(item.get("order", 0))):
        raw_value = int(v.get("value", 0))
        if raw_value == 0:
            continue
        decimals = int(v.get("decimals", 18))
        amount = abs(raw_value) / (10 ** decimals)
        token = v.get("token")
        token_addr = str(v.get("token_addr", ""))
        user_addr = str(v.get("user", ""))
        if raw_value > 0:
            from_name = _display_address(token_addr)
            to_name = _display_address(user_addr)
            action = "mint"
        else:
            from_name = _display_address(user_addr)
            to_name = _display_address(token_addr)
            action = "burn"
        flow_item = {
            "order": int(v.get("order", 0)),
            "token": token,
            "amount": amount,
            "from": from_name,
            "to": to_name,
            "action": action,
        }
        major_flows.append(flow_item)
        flows_by_contract[token_addr].append(flow_item)

    major_flows = sorted(major_flows, key=lambda item: int(item.get("order", 0)))

    net_asset_changes: Dict[str, Dict[str, float]] = {}
    for address, token_map in (address_balances or {}).items():
        compact = {
            token: delta
            for token, delta in token_map.items()
            if isinstance(delta, (int, float)) and abs(delta) > 0
        }
        if compact:
            net_asset_changes[_display_address(address)] = compact
        if len(net_asset_changes) >= 20:
            break

    edge_order_sequence = [
        int(item.get("edge_id"))
        for item in (edge_link or [])
        if isinstance(item.get("edge_id"), int)
    ][:40]

    return {
        "summary": {
            "is_arbitrage": bool((arb_result or {}).get("is_arbitrage", False)),
            "cycle_count": len((arb_result or {}).get("cycles", [])),
            "flow_count": len(major_flows),
            "edge_link_count": len(edge_link or []),
        },
        "major_flows": major_flows,
        "edge_order_sequence": edge_order_sequence,
        "flows_by_contract": dict(flows_by_contract),
        "net_asset_changes": net_asset_changes,
    }
