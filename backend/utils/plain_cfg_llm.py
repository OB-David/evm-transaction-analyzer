import copy
import gzip
import hashlib
import json
import os
import re
import threading
from typing import Any

from openai import APITimeoutError, OpenAI
from signature.store import FunctionSignatureStore
from utils.analysis_paths import analysis_directory

PROMPT_VERSION = "plain_cfg_solidity_reconstruction_v2"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MAX_INPUT_CHARS = 400000
DEFAULT_LLM_TIMEOUT_SECONDS = 300.0
DEFAULT_LLM_MAX_RETRIES = 1
DEFAULT_MAX_OUTPUT_TOKENS = 16384
STACK_TOP_LIMIT = 10
MEMORY_WINDOW_MAX_BYTES = 192
CHANGED_WORD_VALUE_LIMIT = 24
BOUNDARY_CONTEXT_STEPS = 12
MAX_UNRESOLVED_ITEMS = 12
MAX_EVIDENCE_ITEMS = 64
MAX_SOLIDITY_CHARS = 60000
_RUNTIME_CACHE_LOCK = threading.Lock()
_RUNTIME_CACHE_BY_TX: dict[str, dict[str, Any]] = {}
PLAIN_SEMANTICS_FILENAME = "plain_semantics.json.gz"
PLAIN_SEMANTICS_SCHEMA_VERSION = 2

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
SIDE_EFFECT_OPCODES = {
    "SSTORE",
    "CALL",
    "CALLCODE",
    "DELEGATECALL",
    "STATICCALL",
    "CREATE",
    "CREATE2",
    "LOG0",
    "LOG1",
    "LOG2",
    "LOG3",
    "LOG4",
    "RETURN",
    "REVERT",
    "SELFDESTRUCT",
}
SEMANTIC_OPCODES = SIDE_EFFECT_OPCODES | {
    "SLOAD",
    "MLOAD",
    "MSTORE",
    "MSTORE8",
    "CALLDATALOAD",
    "CALLDATACOPY",
    "CALLDATASIZE",
    "RETURNDATACOPY",
    "RETURNDATASIZE",
    "KECCAK256",
    "SHA3",
    "JUMP",
    "JUMPI",
    "STOP",
    "INVALID",
}
ARITHMETIC_OPCODES = {
    "ADD",
    "MUL",
    "SUB",
    "DIV",
    "SDIV",
    "MOD",
    "SMOD",
    "ADDMOD",
    "MULMOD",
    "EXP",
    "SIGNEXTEND",
    "LT",
    "GT",
    "SLT",
    "SGT",
    "EQ",
    "ISZERO",
    "AND",
    "OR",
    "XOR",
    "NOT",
    "BYTE",
    "SHL",
    "SHR",
    "SAR",
}

RECONSTRUCTION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "solidity": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "unresolved": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_UNRESOLVED_ITEMS,
        },
        "evidence": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_EVIDENCE_ITEMS,
            "items": {
                "type": "object",
                "properties": {
                    "step_start": {"type": "integer"},
                    "step_end": {"type": "integer"},
                    "opcodes": {"type": "array", "items": {"type": "string"}},
                    "code": {"type": "string"},
                },
                "required": ["step_start", "step_end", "opcodes", "code"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["solidity", "confidence", "unresolved", "evidence"],
    "additionalProperties": False,
}


class PlainCfgLlmServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def analyze_cfg_block(
    tx_hash: str,
    block_id: str | int,
    mode: str = "plain",
    force_refresh: bool = False,
) -> dict[str, Any]:
    normalized_mode = _normalize_cfg_mode(mode)
    if normalized_mode != "plain":
        raise PlainCfgLlmServiceError(
            400,
            "Solidity reconstruction is available only for plain CFG blocks",
        )
    context_payload, context_meta = build_cfg_block_context(tx_hash, block_id, normalized_mode)
    target_block_id = context_meta["target_block_id"]
    context_json = json.dumps(context_payload, ensure_ascii=False, separators=(",", ":"))
    context_hash = hashlib.sha256(context_json.encode("utf-8")).hexdigest()
    _check_context_size(context_json)

    cache_data = _load_runtime_cache(tx_hash)
    block_cache_key = f"{normalized_mode}:{target_block_id}"
    cached_item = cache_data["items"].get(block_cache_key)
    if (
        not force_refresh
        and isinstance(cached_item, dict)
        and cached_item.get("prompt_version") == PROMPT_VERSION
        and cached_item.get("context_hash") == context_hash
        and isinstance(cached_item.get("reconstruction"), dict)
        and isinstance(cached_item["reconstruction"].get("solidity"), str)
        and cached_item["reconstruction"].get("solidity", "").strip()
    ):
        return {
            "status": "success",
            "source": "cache",
            "reconstruction": copy.deepcopy(cached_item["reconstruction"]),
            "context_meta": context_meta,
        }

    generated = _generate_reconstruction(context_json, context_payload)
    reconstruction = _public_reconstruction(generated, context_payload)
    model_name = os.environ.get("DEEPSEEK_CFG_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    cache_data["prompt_version"] = PROMPT_VERSION
    cache_data["items"][block_cache_key] = {
        "reconstruction": reconstruction,
        "model": model_name,
        "prompt_version": PROMPT_VERSION,
        "context_hash": context_hash,
    }
    _save_runtime_cache(tx_hash, cache_data)

    return {
        "status": "success",
        "source": "llm",
        "reconstruction": reconstruction,
        "context_meta": context_meta,
    }


def analyze_plain_cfg_block(tx_hash: str, block_id: str | int, force_refresh: bool = False) -> dict[str, Any]:
    """Reconstruct only the selected dynamic plain-CFG execution slice."""
    return analyze_cfg_block(tx_hash, block_id, "plain", force_refresh)


def build_cfg_block_context(
    tx_hash: str,
    block_id: str | int,
    mode: str = "plain",
) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized_mode = _normalize_cfg_mode(mode)
    if normalized_mode != "plain":
        raise PlainCfgLlmServiceError(
            400,
            "Solidity reconstruction is available only for plain CFG blocks",
        )
    return build_plain_cfg_block_context(tx_hash, block_id)


def build_plain_cfg_block_context(tx_hash: str, block_id: str | int) -> tuple[dict[str, Any], dict[str, Any]]:
    ordered_blocks, steps = _load_plain_cfg_context_inputs(tx_hash)
    target_idx = _find_target_index(ordered_blocks, block_id)
    if target_idx is None:
        raise PlainCfgLlmServiceError(404, f"Block {block_id} not found in plain CFG")
    return _build_context_for_index(tx_hash, steps, ordered_blocks, target_idx)


def _normalize_cfg_mode(mode: str) -> str:
    normalized = str(mode).strip().lower()
    if normalized not in {"folded", "plain"}:
        raise PlainCfgLlmServiceError(400, "CFG mode must be folded or plain")
    return normalized


def build_plain_cfg_context_preview(
    tx_hash: str, block_ids: list[str | int] | None = None, limit: int = 3
) -> dict[str, Any]:
    ordered_blocks, steps = _load_plain_cfg_context_inputs(tx_hash)
    target_indexes: list[int] = []

    if block_ids:
        for block_id in block_ids:
            target_idx = _find_target_index(ordered_blocks, block_id)
            if target_idx is None:
                raise PlainCfgLlmServiceError(404, f"Block {block_id} not found in plain CFG")
            target_indexes.append(target_idx)
    else:
        if limit <= 0:
            raise PlainCfgLlmServiceError(400, "limit must be greater than 0")
        target_indexes = list(range(min(limit, len(ordered_blocks))))

    previews: list[dict[str, Any]] = []
    for target_idx in target_indexes:
        context_payload, context_meta = _build_context_for_index(tx_hash, steps, ordered_blocks, target_idx)
        previews.append(
            {
                "block_id": context_meta["target_block_id"],
                "context_payload": context_payload,
                "context_meta": context_meta,
            }
        )

    return {
        "tx_hash": tx_hash,
        "preview_count": len(previews),
        "previews": previews,
    }


def _resolve_result_dir(tx_hash: str) -> str:
    result_dir = str(analysis_directory(tx_hash))
    if not os.path.isdir(result_dir):
        raise PlainCfgLlmServiceError(404, "Transaction result directory not found")
    return result_dir


def _load_json_file(file_path: str) -> dict[str, Any]:
    if not os.path.isfile(file_path):
        raise PlainCfgLlmServiceError(404, f"Required file not found: {os.path.basename(file_path)}")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        raise PlainCfgLlmServiceError(400, f"Failed to parse {os.path.basename(file_path)}: {exc}") from exc
    if not isinstance(data, dict):
        raise PlainCfgLlmServiceError(400, f"{os.path.basename(file_path)} must be a JSON object")
    return data


def _load_plain_cfg_context_inputs(
    tx_hash: str,
) -> tuple[list[dict[str, Any]], list[Any] | dict[str, Any]]:
    result_dir = _resolve_result_dir(tx_hash)
    plain_info_path = os.path.join(result_dir, "plain_blocks_information.json")
    semantics_path = os.path.join(result_dir, PLAIN_SEMANTICS_FILENAME)
    trace_path = os.path.join(result_dir, "trace.json")

    plain_info = _load_json_file(plain_info_path)
    if os.path.isfile(semantics_path):
        semantics = _load_gzip_json_file(semantics_path)
        blocks = semantics.get("blocks")
        if not isinstance(blocks, dict):
            raise PlainCfgLlmServiceError(
                400,
                f"{PLAIN_SEMANTICS_FILENAME} does not contain valid block semantics",
            )
        return _build_ordered_plain_blocks(plain_info), blocks

    # Compatibility for analyses created before compact semantics were introduced.
    trace_data = _load_json_file(trace_path)
    steps = trace_data.get("steps")
    if not isinstance(steps, list):
        raise PlainCfgLlmServiceError(404, "trace.json is missing valid steps data")

    return _build_ordered_plain_blocks(plain_info), steps


def _load_gzip_json_file(file_path: str) -> dict[str, Any]:
    try:
        with gzip.open(file_path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        raise PlainCfgLlmServiceError(
            400,
            f"Failed to parse {os.path.basename(file_path)}: {exc}",
        ) from exc
    if not isinstance(data, dict):
        raise PlainCfgLlmServiceError(
            400,
            f"{os.path.basename(file_path)} must be a JSON object",
        )
    return data


def _build_context_for_index(
    tx_hash: str,
    steps: list[Any] | dict[str, Any],
    ordered_blocks: list[dict[str, Any]],
    target_idx: int,
    mode: str = "plain",
    include_neighbors: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if mode != "plain":
        raise PlainCfgLlmServiceError(
            400,
            "Solidity reconstruction is available only for plain CFG blocks",
        )
    prev_block = ordered_blocks[target_idx - 1] if include_neighbors and target_idx > 0 else None
    target_block = ordered_blocks[target_idx]
    next_block = (
        ordered_blocks[target_idx + 1]
        if include_neighbors and target_idx + 1 < len(ordered_blocks)
        else None
    )
    return _build_context_payload(
        tx_hash=tx_hash,
        steps=steps,
        target_block=target_block,
        prev_block=prev_block,
        next_block=next_block,
        call_tree=_load_optional_call_tree(tx_hash),
    )


def _build_ordered_plain_blocks(plain_info: dict[str, Any]) -> list[dict[str, Any]]:
    ordered_blocks: list[dict[str, Any]] = []
    for raw in plain_info.values():
        if not isinstance(raw, dict):
            continue
        if "block_id" not in raw or "start_step" not in raw or "end_step" not in raw:
            continue
        try:
            start_step = int(raw["start_step"])
            end_step = int(raw["end_step"])
            block_id = raw["block_id"]
        except (TypeError, ValueError):
            continue
        ordered_blocks.append(
            {
                "block_id": block_id,
                "address": raw.get("address", ""),
                "start_step": start_step,
                "end_step": end_step,
                "actions": _normalize_actions(raw.get("actions")),
            }
        )

    if not ordered_blocks:
        raise PlainCfgLlmServiceError(404, "plain_blocks_information.json does not contain valid blocks")

    ordered_blocks.sort(key=lambda b: (b["start_step"], str(b["block_id"])))
    return ordered_blocks


def _find_target_index(ordered_blocks: list[dict[str, Any]], block_id: str | int) -> int | None:
    target_key = str(block_id).strip()
    if not target_key:
        raise PlainCfgLlmServiceError(400, "block_id cannot be empty")
    for idx, block in enumerate(ordered_blocks):
        if str(block["block_id"]) == target_key:
            return idx
    return None


def _normalize_actions(raw_actions: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_actions, list):
        return []
    return [copy.deepcopy(item) for item in raw_actions if isinstance(item, dict)]


def _extract_block_steps(
    steps: list[Any] | dict[str, Any],
    block: dict[str, Any],
) -> list[Any]:
    if isinstance(steps, dict):
        block_steps = steps.get(str(block["block_id"]))
        if not isinstance(block_steps, list):
            raise PlainCfgLlmServiceError(
                404,
                f"Compact semantics are missing for block {block['block_id']}",
            )
        return copy.deepcopy(block_steps)

    ranges = block.get("step_ranges") or [
        {"start_step": block["start_step"], "end_step": block["end_step"]}
    ]
    block_steps: list[Any] = []
    for step_range in ranges:
        start = step_range["start_step"]
        end = step_range["end_step"]
        if start < 0 or end < start:
            raise PlainCfgLlmServiceError(
                400, f"Invalid step range for block {block['block_id']}: {start}..{end}"
            )
        if end >= len(steps):
            raise PlainCfgLlmServiceError(
                400, f"Step range exceeds trace bounds for block {block['block_id']}: {start}..{end}"
            )
        block_steps.extend(_build_opcode_step_context(steps, idx) for idx in range(start, end + 1))
    return block_steps


def _build_context_payload(
    tx_hash: str,
    steps: list[Any] | dict[str, Any],
    target_block: dict[str, Any],
    prev_block: dict[str, Any] | None,
    next_block: dict[str, Any] | None,
    call_tree: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    target_steps = _extract_block_steps(steps, target_block)
    call_context = _build_call_context(call_tree or {}, target_block)
    predecessor = _build_boundary_context(steps, prev_block, relation="predecessor")
    successor = _build_boundary_context(steps, next_block, relation="successor")

    context_payload = {
        "task": {
            "kind": "plain_cfg_local_solidity_reconstruction",
            "scope": "selected_dynamic_execution_slice_only",
            "output_kind": "solidity_statement_block",
            "tx_hash": tx_hash,
            "target_block_id": target_block["block_id"],
        },
        "plain_cfg_semantics": {
            "description": (
                "This node is a chronological slice of the executed transaction trace. "
                "It may merge a linear chain of static EVM basic blocks, but it never "
                "represents unexecuted branches or the complete contract function."
            ),
            "base_split_rules": {
                "new_block_at": ["JUMPDEST"],
                "end_block_after": [
                    "JUMP",
                    "JUMPI",
                    "CALL",
                    "CALLCODE",
                    "DELEGATECALL",
                    "STATICCALL",
                    "CREATE",
                    "CREATE2",
                    "STOP",
                    "RETURN",
                    "REVERT",
                    "INVALID",
                    "SELFDESTRUCT",
                ],
                "plain_view_merge_rule": (
                    "Chronological non-fixed chains are merged; storage, log, external-call, "
                    "creation, terminal, and selected topology-hub blocks remain boundaries."
                ),
            },
        },
        "call_context": call_context,
        "control_context": {
            "predecessor_tail": predecessor,
            "successor_head": successor,
            "usage": "Boundary context only; never emit predecessor or successor behavior as target code.",
        },
        "target": {
            "block_id": target_block["block_id"],
            "address": target_block.get("address", ""),
            "start_step": target_block["start_step"],
            "end_step": target_block["end_step"],
            "step_count": len(target_steps),
            "actions": copy.deepcopy(target_block.get("actions", [])),
            "entry_stack_top": _stack_edge(target_steps, "top_before", first=True),
            "exit_stack_top": _stack_edge(target_steps, "top_after", first=False),
            "opcode_histogram": _opcode_histogram(target_steps),
            "effect_and_dependency_facts": _build_dependency_summary(target_steps),
            "execution_trace": [_compact_trace_step(step) for step in target_steps],
        },
    }
    context_meta = {
        "target_block_id": target_block["block_id"],
        "prev_block_id": prev_block["block_id"] if prev_block else None,
        "next_block_id": next_block["block_id"] if next_block else None,
        "step_ranges": {
            "prev": (
                {
                    "block_id": prev_block["block_id"],
                    "start_step": prev_block["start_step"],
                    "end_step": prev_block["end_step"],
                }
                if prev_block
                else None
            ),
            "target": {
                "block_id": target_block["block_id"],
                "start_step": target_block["start_step"],
                "end_step": target_block["end_step"],
            },
            "next": (
                {
                    "block_id": next_block["block_id"],
                    "start_step": next_block["start_step"],
                    "end_step": next_block["end_step"],
                }
                if next_block
                else None
            ),
        },
        "cfg_mode": "plain",
        "active_call_selector": call_context.get("active_frame", {}).get("selector"),
        "signature_candidates": copy.deepcopy(
            call_context.get("active_frame", {}).get("signature_candidates", [])
        ),
    }
    return context_payload, context_meta


def _load_optional_call_tree(tx_hash: str) -> dict[str, Any]:
    call_tree_path = os.path.join(_resolve_result_dir(tx_hash), "call_tree.json")
    if not os.path.isfile(call_tree_path):
        return {}
    try:
        return _load_json_file(call_tree_path)
    except PlainCfgLlmServiceError:
        return {}


def _build_boundary_context(
    steps: list[Any] | dict[str, Any],
    block: dict[str, Any] | None,
    *,
    relation: str,
) -> dict[str, Any] | None:
    if block is None:
        return None
    block_steps = _extract_block_steps(steps, block)
    if relation == "predecessor":
        selected = block_steps[-BOUNDARY_CONTEXT_STEPS:]
    else:
        selected = block_steps[:BOUNDARY_CONTEXT_STEPS]
    return {
        "block_id": block["block_id"],
        "address": block.get("address", ""),
        "start_step": block["start_step"],
        "end_step": block["end_step"],
        "shown_step_count": len(selected),
        "total_step_count": len(block_steps),
        "execution_trace": [_compact_trace_step(step) for step in selected],
    }


def _compact_trace_step(raw_step: Any) -> dict[str, Any]:
    step = raw_step if isinstance(raw_step, dict) else {}
    opcode = str(step.get("opcode", "")).upper()
    compact: dict[str, Any] = {
        "step": _safe_int(step.get("step_index")),
        "pc": step.get("pc"),
        "op": opcode,
    }
    operands = step.get("opcode_operands")
    if isinstance(operands, dict) and operands:
        compact["operands"] = copy.deepcopy(operands)

    stack = step.get("stack") if isinstance(step.get("stack"), dict) else {}
    pushed = _as_str_list(stack.get("pushed_values"))
    if opcode.startswith("PUSH") and pushed:
        compact["push"] = pushed[-1]
    elif opcode in ARITHMETIC_OPCODES:
        before = _as_str_list(stack.get("top_before"))
        after = _as_str_list(stack.get("top_after"))
        if before:
            compact["in"] = before[-3:]
        if after and bool(stack.get("after_observed", True)):
            compact["out"] = after[-1]
    elif opcode in SEMANTIC_OPCODES:
        before = _as_str_list(stack.get("top_before"))
        after = _as_str_list(stack.get("top_after"))
        if before:
            compact["stack_in_top"] = before[-7:]
        if after and bool(stack.get("after_observed", True)):
            compact["stack_out_top"] = after[-4:]

    memory = step.get("memory")
    if isinstance(memory, dict):
        relevant_memory = {
            key: copy.deepcopy(memory[key])
            for key in ("reads", "writes")
            if isinstance(memory.get(key), list) and memory[key]
        }
        if relevant_memory:
            compact["memory"] = relevant_memory
    return compact


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opcode_histogram(steps: list[Any]) -> dict[str, int]:
    histogram: dict[str, int] = {}
    for raw_step in steps:
        if not isinstance(raw_step, dict):
            continue
        opcode = str(raw_step.get("opcode", "")).upper()
        if opcode:
            histogram[opcode] = histogram.get(opcode, 0) + 1
    return dict(sorted(histogram.items()))


def _stack_edge(steps: list[Any], key: str, *, first: bool) -> list[str]:
    if not steps:
        return []
    raw_step = steps[0] if first else steps[-1]
    if not isinstance(raw_step, dict) or not isinstance(raw_step.get("stack"), dict):
        return []
    return _as_str_list(raw_step["stack"].get(key))


def _build_dependency_summary(steps: list[Any]) -> dict[str, Any]:
    summary: dict[str, list[dict[str, Any]]] = {
        "calldata": [],
        "storage": [],
        "memory_hashing": [],
        "external_calls_and_creation": [],
        "logs": [],
        "control_and_exit": [],
        "environment": [],
    }
    environment_opcodes = {
        "ADDRESS",
        "BALANCE",
        "ORIGIN",
        "CALLER",
        "CALLVALUE",
        "CODESIZE",
        "EXTCODESIZE",
        "EXTCODEHASH",
        "GASPRICE",
        "BLOCKHASH",
        "COINBASE",
        "TIMESTAMP",
        "NUMBER",
        "PREVRANDAO",
        "DIFFICULTY",
        "GASLIMIT",
        "CHAINID",
        "SELFBALANCE",
        "BASEFEE",
        "BLOBHASH",
        "BLOBBASEFEE",
    }
    for raw_step in steps:
        if not isinstance(raw_step, dict):
            continue
        opcode = str(raw_step.get("opcode", "")).upper()
        fact = _compact_trace_step(raw_step)
        if opcode.startswith("CALLDATA"):
            summary["calldata"].append(fact)
        if opcode in {"SLOAD", "SSTORE"}:
            summary["storage"].append(fact)
        if opcode in {"KECCAK256", "SHA3"}:
            summary["memory_hashing"].append(fact)
        if opcode in {
            "CALL",
            "CALLCODE",
            "DELEGATECALL",
            "STATICCALL",
            "CREATE",
            "CREATE2",
        }:
            summary["external_calls_and_creation"].append(fact)
        if opcode.startswith("LOG") and len(opcode) == 4:
            summary["logs"].append(fact)
        if opcode in {"JUMP", "JUMPI", "RETURN", "REVERT", "STOP", "INVALID", "SELFDESTRUCT"}:
            summary["control_and_exit"].append(fact)
        if opcode in environment_opcodes:
            summary["environment"].append(fact)
    return summary


def _build_call_context(call_tree: dict[str, Any], target_block: dict[str, Any]) -> dict[str, Any]:
    root = call_tree.get("root") if isinstance(call_tree.get("root"), dict) else {}
    calls = [item for item in call_tree.get("calls", []) if isinstance(item, dict)]
    start_step = int(target_block["start_step"])
    end_step = int(target_block["end_step"])
    target_address = str(target_block.get("address", "")).lower()

    containing = []
    for frame in calls:
        entry_step = _safe_int(frame.get("entry_step"))
        exit_step = _safe_int(frame.get("exit_step"))
        if entry_step is None or exit_step is None:
            continue
        if not (entry_step < start_step and end_step <= exit_step):
            continue
        if target_address and str(frame.get("to_address", "")).lower() != target_address:
            continue
        containing.append(frame)

    active = max(
        containing,
        key=lambda frame: (
            _safe_int(frame.get("depth")) or 0,
            -((_safe_int(frame.get("exit_step")) or 0) - (_safe_int(frame.get("entry_step")) or 0)),
        ),
        default=None,
    )
    frame_by_id = {
        int(frame["call_id"]): frame
        for frame in calls
        if _safe_int(frame.get("call_id")) is not None
    }

    signature_cache: dict[str, list[str]] = {}
    with FunctionSignatureStore() as signature_store:
        def signature_candidates(frame: dict[str, Any]) -> list[str]:
            selector = str(frame.get("selector") or "").lower()
            if selector in signature_cache:
                return signature_cache[selector]
            candidates: list[str] = []
            if selector and signature_store.available:
                seen: set[str] = set()
                for record in signature_store.lookup(selector):
                    if record.text_signature not in seen:
                        seen.add(record.text_signature)
                        candidates.append(record.text_signature)
            if not candidates:
                candidates = _as_str_list(frame.get("probable_text_signatures"))
            signature_cache[selector] = candidates[:12]
            return signature_cache[selector]

        def public_frame(frame: dict[str, Any], *, include_calldata: bool) -> dict[str, Any]:
            result: dict[str, Any] = {
                "call_id": frame.get("call_id"),
                "parent_call_id": frame.get("parent_call_id"),
                "depth": frame.get("depth"),
                "entry_step": frame.get("entry_step"),
                "exit_step": frame.get("exit_step"),
                "entry_op": frame.get("entry_op"),
                "exit_op": frame.get("exit_op"),
                "from_address": frame.get("from_address"),
                "to_address": frame.get("to_address"),
                "from_name": frame.get("from_name"),
                "to_name": frame.get("to_name"),
                "selector": frame.get("selector"),
                "signature_candidates": signature_candidates(frame),
            }
            if include_calldata:
                calldata = _as_str_list(frame.get("calldata"))
                result["calldata_words"] = calldata[:32]
                result["calldata_word_count"] = len(calldata)
                result["calldata_truncated"] = len(calldata) > 32
            return result

        if active is not None:
            ancestors: list[dict[str, Any]] = []
            parent_id = _safe_int(active.get("parent_call_id"))
            while parent_id is not None and parent_id in frame_by_id:
                parent = frame_by_id[parent_id]
                ancestors.append(public_frame(parent, include_calldata=False))
                parent_id = _safe_int(parent.get("parent_call_id"))
            ancestors.reverse()
            active_frame = public_frame(active, include_calldata=True)
            active_call_id = _safe_int(active.get("call_id"))
        else:
            root_frame = {
                "call_id": None,
                "parent_call_id": None,
                "depth": 0,
                "entry_op": "TRANSACTION",
                "exit_op": None,
                "from_address": None,
                "to_address": root.get("address"),
                "from_name": "Transaction sender",
                "to_name": root.get("name"),
                "selector": root.get("selector"),
                "probable_text_signatures": root.get("probable_text_signatures", []),
                "calldata": root.get("calldata", []),
            }
            active_frame = public_frame(root_frame, include_calldata=True)
            ancestors = []
            active_call_id = None

        child_calls = []
        for frame in calls:
            entry_step = _safe_int(frame.get("entry_step"))
            if entry_step is None or not (start_step <= entry_step <= end_step):
                continue
            if _safe_int(frame.get("parent_call_id")) != active_call_id:
                continue
            child_calls.append(public_frame(frame, include_calldata=True))

    return {
        "signature_warning": (
            "Selector matches are candidates from a signature database, not proof of the original ABI."
        ),
        "ancestors": ancestors,
        "active_frame": active_frame,
        "child_calls_entered_by_target": child_calls,
    }


def _check_context_size(context_json: str) -> None:
    raw_limit = os.environ.get("DEEPSEEK_CFG_MAX_INPUT_CHARS", "").strip()
    try:
        limit = int(raw_limit) if raw_limit else DEFAULT_MAX_INPUT_CHARS
    except ValueError:
        limit = DEFAULT_MAX_INPUT_CHARS

    if len(context_json) > limit:
        raise PlainCfgLlmServiceError(
            413,
            (
                f"LLM context exceeds limit: {len(context_json)} chars > {limit} chars. "
                "Strict context mode is enabled; no truncation was applied."
            ),
        )


def clear_plain_cfg_runtime_cache(tx_hash: str) -> None:
    normalized = _normalize_tx_hash(tx_hash)
    with _RUNTIME_CACHE_LOCK:
        _RUNTIME_CACHE_BY_TX.pop(normalized, None)


def _normalize_tx_hash(tx_hash: str) -> str:
    return str(tx_hash).strip().lower()


def _load_runtime_cache(tx_hash: str) -> dict[str, Any]:
    normalized = _normalize_tx_hash(tx_hash)
    with _RUNTIME_CACHE_LOCK:
        raw = _RUNTIME_CACHE_BY_TX.get(normalized)
        if not isinstance(raw, dict):
            return {"prompt_version": PROMPT_VERSION, "items": {}}

    items = raw.get("items")
    if not isinstance(items, dict):
        items = {}
    return {
        "prompt_version": str(raw.get("prompt_version", PROMPT_VERSION)),
        "items": dict(items),
    }


def _save_runtime_cache(tx_hash: str, cache_data: dict[str, Any]) -> None:
    normalized = _normalize_tx_hash(tx_hash)
    items = cache_data.get("items")
    if not isinstance(items, dict):
        items = {}
    payload = {
        "prompt_version": str(cache_data.get("prompt_version", PROMPT_VERSION)),
        "items": dict(items),
    }
    with _RUNTIME_CACHE_LOCK:
        _RUNTIME_CACHE_BY_TX[normalized] = payload


def _generate_reconstruction(
    context_json: str,
    context_payload: dict[str, Any],
) -> dict[str, Any]:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise PlainCfgLlmServiceError(500, "DEEPSEEK_API_KEY is not configured")

    api_mode = os.environ.get("DEEPSEEK_CFG_API_MODE", "responses").strip().lower()
    if api_mode not in {"responses", "chat_completions"}:
        raise PlainCfgLlmServiceError(
            500,
            (
                f"Unsupported DEEPSEEK_CFG_API_MODE: {api_mode}. "
                "Expected responses or chat_completions."
            ),
        )

    base_url = os.environ.get("DEEPSEEK_BASE_URL", "").strip() or DEFAULT_BASE_URL
    model_name = os.environ.get("DEEPSEEK_CFG_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    timeout_seconds = _get_timeout_seconds()
    max_retries = _get_max_retries()

    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout_seconds,
        max_retries=max_retries,
    )
    system_prompt = """You are a specialized EVM-to-Solidity local reconstruction engine.
Reconstruct only the selected dynamic plain-CFG execution slice. The result is not the
original source and is not a complete function or contract.

Accuracy rules:
- Treat target.execution_trace and target.effect_and_dependency_facts as evidence.
- Use predecessor/successor only to understand values entering or leaving the slice.
  Never emit their behavior as target code.
- A signature-database match is only a candidate. Do not choose a function name or ABI
  unless the executed calldata handling and call evidence support it.
- Preserve every observed storage write, external call/create, log, return/revert, and
  self-destruct effect. Preserve the observed order and the executed branch only.
- Preserve material conditions that were evaluated on the executed path. If the opposite
  failure/revert body was not executed, express the known condition as a concise Solidity
  comment instead of inventing that body.
- Do not invent unexecuted branches, modifiers, events, state-variable names, mappings,
  structs, interfaces, constants, or business intent.
- Describe branch comparisons operationally from their observed operands. Do not label a
  check as self-transfer, allowance, authorization, or another business rule unless the
  selector, calldata flow, and executed operations jointly support that label.
- A runtime stack value is not necessarily a Solidity literal. Emit a literal only when
  its provenance is an observed PUSH or another clearly constant expression. Otherwise
  use a declared, descriptive unknown_* local and explain it in unresolved.
- Prefer readable Solidity. Use a small inline assembly section only when high-level
  Solidity would falsely imply unavailable type or storage-layout information.
- Represent observed raw storage, log, and contract-creation opcodes with their matching
  inline-assembly operation instead of inventing state variables, events, or contract types.
  Represent external calls with their matching low-level call form.
- The solidity field must be exactly one Solidity statement block beginning with { and
  ending with }. Do not emit pragma, import, contract, interface, library, function, or
  markdown fences. Declare placeholder locals used inside the block.
- Ground each meaningful emitted operation with evidence entries. Evidence opcodes and
  inclusive step ranges must exactly refer to the supplied target trace. Each evidence.code
  value must be an exact source fragment copied from the solidity field, not a description.
- Keep the answer compact and use at most 64 evidence entries. One evidence range may cover
  repeated instances of the same emitted operation when the range is accurate.
- Report uncertainty rather than guessing. Confidence describes semantic fidelity, not
  formatting confidence.
"""
    base_user_prompt = (
        "Reconstruct the target as a local Solidity statement block. Return JSON only with "
        "the keys solidity, confidence, unresolved, and evidence. Each evidence item must "
        "contain step_start, step_end, opcodes, and an exact emitted code fragment. Do not return "
        "any other keys."
    )

    validation_errors: list[str] = []
    last_response_diagnostic = ""
    previous_parsed: dict[str, Any] | None = None
    for attempt in range(2):
        strict_note = ""
        if attempt > 0 and validation_errors:
            strict_note = (
                "\nThe previous output failed deterministic validation. Correct every issue:\n- "
                + "\n- ".join(validation_errors[:12])
            )
        if attempt > 0 and previous_parsed is not None:
            user_prompt = _build_reconstruction_repair_prompt(
                previous_parsed,
                validation_errors,
                context_payload,
            )
        else:
            user_prompt = (
                f"{base_user_prompt}{strict_note}\n\n"
                f"Evidence context JSON:\n{context_json}"
            )

        try:
            response = _invoke_reconstruction_model(
                client=client,
                api_mode=api_mode,
                model_name=model_name,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_output_tokens=min(
                    _get_max_output_tokens() * (attempt + 1),
                    32768,
                ),
            )
        except APITimeoutError as exc:
            raise PlainCfgLlmServiceError(
                504,
                (
                    f"LLM request timed out after {timeout_seconds:g}s. "
                    "Check the upstream LLM gateway or increase DEEPSEEK_CFG_TIMEOUT_SECONDS."
                ),
            ) from exc
        except Exception as exc:
            raise PlainCfgLlmServiceError(502, f"LLM request failed: {exc}") from exc

        raw_text = _extract_response_text(response)
        last_response_diagnostic = _response_diagnostic(response, raw_text)
        parsed = _parse_response_json(raw_text)
        if parsed is None:
            previous_parsed = None
            validation_errors = [
                "Return one valid JSON object with the required keys; "
                + last_response_diagnostic
            ]
            continue

        parsed = _normalize_generated_reconstruction(parsed, context_payload)
        validation_errors = _validate_generated_reconstruction(parsed, context_payload)
        if not validation_errors:
            return parsed
        previous_parsed = parsed

    error_detail = (
        "LLM reconstruction failed deterministic validation: "
        + "; ".join(validation_errors[:6])
    )
    if last_response_diagnostic and not any(
        last_response_diagnostic in error for error in validation_errors
    ):
        error_detail += f"; {last_response_diagnostic}"
    raise PlainCfgLlmServiceError(
        502,
        error_detail,
    )


def _build_reconstruction_repair_prompt(
    previous_output: dict[str, Any],
    validation_errors: list[str],
    context_payload: dict[str, Any],
) -> str:
    target = context_payload.get("target", {})
    trace = target.get("execution_trace", []) if isinstance(target, dict) else []
    claimed_opcodes = {
        str(opcode).upper()
        for item in previous_output.get("evidence", [])
        if isinstance(item, dict) and isinstance(item.get("opcodes"), list)
        for opcode in item["opcodes"]
        if isinstance(opcode, str)
    }
    observed_side_effects = {
        str(item.get("op", "")).upper()
        for item in trace
        if isinstance(item, dict) and str(item.get("op", "")).upper() in SIDE_EFFECT_OPCODES
    }
    relevant_opcodes = claimed_opcodes | observed_side_effects
    opcode_steps: dict[str, list[int]] = {opcode: [] for opcode in sorted(relevant_opcodes)}
    relevant_trace: list[dict[str, Any]] = []
    for item in trace:
        if not isinstance(item, dict):
            continue
        opcode = str(item.get("op", "")).upper()
        step = _safe_int(item.get("step"))
        if opcode not in relevant_opcodes or step is None:
            continue
        opcode_steps[opcode].append(step)
        relevant_trace.append(copy.deepcopy(item))

    repair_payload = {
        "task": (
            "Repair the previous JSON output. Do not reconstruct from scratch. Preserve the "
            "solidity code unless a listed validation error specifically requires changing it."
        ),
        "validation_errors": validation_errors[:12],
        "target_range": {
            "start_step": target.get("start_step") if isinstance(target, dict) else None,
            "end_step": target.get("end_step") if isinstance(target, dict) else None,
        },
        "observed_opcode_steps": opcode_steps,
        "relevant_trace_steps": relevant_trace,
        "effect_and_dependency_facts": (
            copy.deepcopy(target.get("effect_and_dependency_facts", {}))
            if isinstance(target, dict)
            else {}
        ),
        "previous_output": previous_output,
        "repair_rules": [
            "Every evidence opcode must occur inside that evidence item's inclusive step range.",
            "Every evidence.code must remain an exact source fragment from solidity.",
            "Use observed_opcode_steps as authoritative; never guess a step number.",
            "Return the complete repaired JSON object with exactly solidity, confidence, unresolved, and evidence.",
        ],
    }
    return "Targeted deterministic-validation repair JSON:\n" + json.dumps(
        repair_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _invoke_reconstruction_model(
    *,
    client: OpenAI,
    api_mode: str,
    model_name: str,
    system_prompt: str,
    user_prompt: str,
    max_output_tokens: int,
) -> Any:
    if api_mode == "responses":
        return client.responses.create(
            model=model_name,
            instructions=system_prompt,
            input=user_prompt,
            reasoning={"effort": "high"},
            max_output_tokens=max_output_tokens,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "plain_cfg_solidity_reconstruction",
                    "schema": RECONSTRUCTION_OUTPUT_SCHEMA,
                    "strict": True,
                }
            },
        )
    return client.chat.completions.create(
        model=model_name,
        temperature=0.1,
        max_tokens=max_output_tokens,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )


def _extract_response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        if message is not None:
            content = getattr(message, "content", "")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, dict):
                return json.dumps(content, ensure_ascii=False)
            if isinstance(content, list):
                parts: list[str] = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                        parts.append(str(item.get("text", "")))
                    elif hasattr(item, "text"):
                        parts.append(str(getattr(item, "text")))
                return "\n".join(parts).strip()

    output = getattr(response, "output", None)
    if isinstance(output, list):
        parts = []
        for item in output:
            content = item.get("content", []) if isinstance(item, dict) else getattr(item, "content", [])
            if not isinstance(content, list):
                continue
            for part in content:
                part_type = part.get("type") if isinstance(part, dict) else getattr(part, "type", "")
                if part_type != "output_text":
                    continue
                text = part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "")
                parts.append(str(text))
        return "\n".join(parts).strip()
    return ""


def _response_diagnostic(response: Any, raw_text: str) -> str:
    finish_reasons: list[str] = []
    choices = getattr(response, "choices", None)
    if isinstance(choices, list):
        for choice in choices:
            reason = (
                choice.get("finish_reason")
                if isinstance(choice, dict)
                else getattr(choice, "finish_reason", None)
            )
            if reason is not None:
                finish_reasons.append(str(reason))
    reasoning_chars = 0
    if isinstance(choices, list) and choices:
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, dict) else getattr(choice, "message", None)
        reasoning = (
            message.get("reasoning_content")
            if isinstance(message, dict)
            else getattr(message, "reasoning_content", None)
        )
        if isinstance(reasoning, str):
            reasoning_chars = len(reasoning)
    reason_label = ",".join(finish_reasons) if finish_reasons else "unknown"
    return (
        f"upstream finish_reason={reason_label}, content_chars={len(raw_text)}, "
        f"reasoning_chars={reasoning_chars}"
    )


def _parse_response_json(raw_text: str) -> dict[str, Any] | None:
    if not raw_text:
        return None

    candidate = raw_text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
        candidate = candidate.strip()

    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(candidate):
        if char != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(candidate[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _normalize_generated_reconstruction(
    parsed: dict[str, Any],
    context_payload: dict[str, Any],
) -> dict[str, Any]:
    """Repair only mechanically provable Chat-compatibility schema deviations."""
    normalized = copy.deepcopy(parsed)

    confidence = normalized.get("confidence")
    if confidence not in CONFIDENCE_RANK:
        normalized_confidence = "low"
        if isinstance(confidence, str):
            match = re.search(r"\b(high|medium|low)\b", confidence.strip().lower())
            if match:
                normalized_confidence = match.group(1)
        elif isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            if confidence >= 0.75:
                normalized_confidence = "high"
            elif confidence >= 0.4:
                normalized_confidence = "medium"
        normalized["confidence"] = normalized_confidence

    unresolved = normalized.get("unresolved")
    if isinstance(unresolved, str):
        normalized["unresolved"] = [unresolved] if unresolved.strip() else []
    elif unresolved is None:
        normalized["unresolved"] = []

    target = context_payload.get("target", {})
    trace = target.get("execution_trace", []) if isinstance(target, dict) else []
    observed_by_step = {
        int(item["step"]): str(item.get("op", "")).upper()
        for item in trace
        if isinstance(item, dict) and _safe_int(item.get("step")) is not None
    }
    evidence = normalized.get("evidence")
    if evidence is None or evidence == []:
        normalized["evidence"] = _synthesize_side_effect_evidence(
            normalized.get("solidity"),
            observed_by_step,
        )
        return normalized
    if not isinstance(evidence, list):
        return normalized

    normalized_evidence: list[Any] = []
    for item in evidence:
        if not isinstance(item, dict):
            normalized_evidence.append(item)
            continue
        normalized_item = copy.deepcopy(item)
        raw_opcodes = normalized_item.get("opcodes")
        if raw_opcodes is None:
            raw_opcodes = []
            normalized_item["opcodes"] = raw_opcodes
        if isinstance(raw_opcodes, str):
            raw_opcodes = [part for part in re.split(r"[\s,|/]+", raw_opcodes) if part]
            normalized_item["opcodes"] = raw_opcodes
        if isinstance(raw_opcodes, list):
            cleaned_opcodes = [
                str(opcode).strip().upper()
                for opcode in raw_opcodes
                if isinstance(opcode, str) and opcode.strip()
            ]
            normalized_item["opcodes"] = list(dict.fromkeys(cleaned_opcodes))
            if not cleaned_opcodes:
                inferred = _infer_evidence_opcodes(normalized_item, observed_by_step)
                if inferred:
                    normalized_item["opcodes"] = inferred
                else:
                    # A source declaration or prose fragment with no trace operation is
                    # not evidence. Dropping it is safer than inventing an opcode claim.
                    continue
        normalized_evidence.append(normalized_item)
    normalized["evidence"] = (
        normalized_evidence
        or _synthesize_side_effect_evidence(normalized.get("solidity"), observed_by_step)
    )
    return normalized


def _synthesize_side_effect_evidence(
    solidity: Any,
    observed_by_step: dict[int, str],
) -> list[dict[str, Any]]:
    """Recover only trace/code-provable coverage when the model omits evidence."""
    if not isinstance(solidity, str) or not solidity.strip() or not observed_by_step:
        return []

    code_without_comments = _strip_solidity_comments(solidity).lower()
    represented_opcodes: list[str] = []
    for opcode in dict.fromkeys(observed_by_step.values()):
        if opcode not in SIDE_EFFECT_OPCODES:
            continue
        if not _validate_observed_effect_representation(
            code_without_comments,
            {opcode},
        ):
            represented_opcodes.append(opcode)

    if not represented_opcodes:
        return []

    return [
        {
            "step_start": min(observed_by_step),
            "step_end": max(observed_by_step),
            "opcodes": represented_opcodes,
            # The complete statement block is an exact source fragment. Keeping it intact
            # avoids pretending that a fragile regex can map a nested Solidity/Yul statement
            # to a more precise dynamic step range.
            "code": solidity.strip(),
        }
    ]


def _infer_evidence_opcodes(
    evidence_item: dict[str, Any],
    observed_by_step: dict[int, str],
) -> list[str]:
    start = _safe_int(evidence_item.get("step_start"))
    end = _safe_int(evidence_item.get("step_end"))
    code = evidence_item.get("code")
    if start is None or end is None or start > end or not isinstance(code, str):
        return []

    actual_opcodes = {
        opcode
        for step, opcode in observed_by_step.items()
        if start <= step <= end
    }
    called_names = {
        match.group(1).lower()
        for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", code)
    }
    inferred: list[str] = []
    for opcode in sorted(actual_opcodes):
        aliases = {opcode.lower()}
        if opcode in {"KECCAK256", "SHA3"}:
            aliases.update({"keccak256", "sha3"})
        elif opcode == "REVERT":
            aliases.update({"require", "assert"})
        if aliases.intersection(called_names):
            inferred.append(opcode)
    return inferred


def _validate_generated_reconstruction(
    parsed: dict[str, Any],
    context_payload: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    expected_keys = {"solidity", "confidence", "unresolved", "evidence"}
    if set(parsed) != expected_keys:
        errors.append("JSON must contain exactly solidity, confidence, unresolved, and evidence")

    solidity = parsed.get("solidity")
    if not isinstance(solidity, str):
        errors.append("solidity must be a string")
    else:
        errors.extend(_validate_solidity_statement_block(solidity))

    confidence = parsed.get("confidence")
    if confidence not in CONFIDENCE_RANK:
        errors.append("confidence must be high, medium, or low")

    unresolved = parsed.get("unresolved")
    if not isinstance(unresolved, list) or any(not isinstance(item, str) for item in unresolved):
        errors.append("unresolved must be an array of strings")
    elif len(unresolved) > MAX_UNRESOLVED_ITEMS:
        errors.append(f"unresolved may contain at most {MAX_UNRESOLVED_ITEMS} items")

    target = context_payload.get("target", {})
    trace = target.get("execution_trace", []) if isinstance(target, dict) else []
    observed_by_step = {
        int(item["step"]): str(item.get("op", "")).upper()
        for item in trace
        if isinstance(item, dict) and _safe_int(item.get("step")) is not None
    }
    if not observed_by_step:
        errors.append("target trace contains no verifiable steps")
        return errors
    target_start = min(observed_by_step)
    target_end = max(observed_by_step)

    evidence = parsed.get("evidence")
    normalized_evidence: list[tuple[int, int, set[str]]] = []
    if not isinstance(evidence, list) or not evidence:
        errors.append("evidence must be a non-empty array")
    elif len(evidence) > MAX_EVIDENCE_ITEMS:
        errors.append(f"evidence may contain at most {MAX_EVIDENCE_ITEMS} items")
    else:
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                errors.append(f"evidence[{index}] must be an object")
                continue
            if set(item) != {"step_start", "step_end", "opcodes", "code"}:
                errors.append(f"evidence[{index}] has invalid keys")
                continue
            start = _safe_int(item.get("step_start"))
            end = _safe_int(item.get("step_end"))
            opcodes = item.get("opcodes")
            code = item.get("code")
            if start is None or end is None or start > end:
                errors.append(f"evidence[{index}] has an invalid step range")
                continue
            if start < target_start or end > target_end:
                errors.append(f"evidence[{index}] references steps outside the target")
                continue
            if not isinstance(opcodes, list) or not opcodes or any(not isinstance(op, str) for op in opcodes):
                errors.append(f"evidence[{index}].opcodes must be a non-empty string array")
                continue
            if not isinstance(code, str) or not code.strip():
                errors.append(f"evidence[{index}].code must be a non-empty string")
                continue
            if isinstance(solidity, str) and not _source_contains_fragment(solidity, code):
                errors.append(f"evidence[{index}].code is not a source fragment from solidity")
                continue
            normalized_opcodes = {str(op).upper() for op in opcodes}
            actual_opcodes = {
                opcode
                for step, opcode in observed_by_step.items()
                if start <= step <= end
            }
            missing = normalized_opcodes - actual_opcodes
            if missing:
                observed_locations = {
                    opcode: [
                        step
                        for step, observed_opcode in observed_by_step.items()
                        if observed_opcode == opcode
                    ]
                    for opcode in sorted(missing)
                }
                location_text = "; ".join(
                    f"{opcode} target steps={_format_step_locations(locations)}"
                    for opcode, locations in observed_locations.items()
                )
                errors.append(
                    f"evidence[{index}] claims opcodes absent from range {start}..{end}: "
                    f"{', '.join(sorted(missing))}; {location_text}"
                )
                continue
            fragment_errors = _validate_observed_effect_representation(
                _strip_solidity_comments(code).lower(),
                normalized_opcodes & SIDE_EFFECT_OPCODES,
            )
            if fragment_errors:
                errors.extend(
                    f"evidence[{index}] {error}" for error in fragment_errors
                )
                continue
            normalized_evidence.append((start, end, normalized_opcodes))

    for step, opcode in observed_by_step.items():
        if opcode not in SIDE_EFFECT_OPCODES:
            continue
        if not any(start <= step <= end and opcode in opcodes for start, end, opcodes in normalized_evidence):
            errors.append(f"side-effect opcode {opcode} at step {step} lacks evidence coverage")

    if isinstance(solidity, str):
        code_without_comments = _strip_solidity_comments(solidity).lower()
        observed_opcodes = set(observed_by_step.values())
        errors.extend(
            _validate_observed_effect_representation(code_without_comments, observed_opcodes)
        )
        errors.extend(
            _validate_unobserved_effect_representation(code_without_comments, observed_opcodes)
        )
        if "emit " in code_without_comments and not any(op.startswith("LOG") for op in observed_opcodes):
            errors.append("code emits an event but the target contains no LOG opcode")
        if "selfdestruct" in code_without_comments and "SELFDESTRUCT" not in observed_opcodes:
            errors.append("code uses selfdestruct but the target contains no SELFDESTRUCT opcode")
        if re.search(r"\.\s*(?:delegatecall|staticcall|call)\s*(?:\{|\()", code_without_comments):
            if not observed_opcodes.intersection({"CALL", "CALLCODE", "DELEGATECALL", "STATICCALL"}):
                errors.append("code performs an external call but the target contains no call opcode")
    return errors


def _format_step_locations(locations: list[int], limit: int = 24) -> str:
    if not locations:
        return "none in target"
    shown = ",".join(str(step) for step in locations[:limit])
    if len(locations) > limit:
        shown += f",...(+{len(locations) - limit})"
    return shown


def _source_contains_fragment(solidity: str, fragment: str) -> bool:
    normalized_source = re.sub(r"\s+", " ", solidity).strip()
    normalized_fragment = re.sub(r"\s+", " ", fragment).strip()
    return len(normalized_fragment) >= 3 and normalized_fragment in normalized_source


def _validate_observed_effect_representation(
    code: str,
    observed_opcodes: set[str],
) -> list[str]:
    patterns: dict[str, str] = {
        "SSTORE": r"\bsstore\s*\(",
        "CALL": r"(?:\.\s*call\s*(?:\{|\()|\bcall\s*\()",
        "CALLCODE": r"\bcallcode\s*\(",
        "DELEGATECALL": r"(?:\.\s*delegatecall\s*\(|\bdelegatecall\s*\()",
        "STATICCALL": r"(?:\.\s*staticcall\s*\(|\bstaticcall\s*\()",
        "CREATE": r"\bcreate\s*\(",
        "CREATE2": r"\bcreate2\s*\(",
        "RETURN": r"\breturn\b",
        "REVERT": r"\b(?:revert|require|assert)\b",
        "SELFDESTRUCT": r"\bselfdestruct\s*\(",
    }
    errors: list[str] = []
    for opcode, pattern in patterns.items():
        if opcode in observed_opcodes and not re.search(pattern, code):
            errors.append(f"observed {opcode} is not represented in the solidity code")

    observed_logs = sorted(op for op in observed_opcodes if re.fullmatch(r"LOG[0-4]", op))
    for opcode in observed_logs:
        if not re.search(rf"\b{opcode.lower()}\s*\(", code):
            errors.append(f"observed {opcode} is not represented in the solidity code")
    return errors


def _validate_unobserved_effect_representation(
    code: str,
    observed_opcodes: set[str],
) -> list[str]:
    patterns: dict[str, str] = {
        "SSTORE": r"\bsstore\s*\(",
        "CALL": r"(?:\.\s*call\s*(?:\{|\()|\bcall\s*\()",
        "CALLCODE": r"\bcallcode\s*\(",
        "DELEGATECALL": r"(?:\.\s*delegatecall\s*\(|\bdelegatecall\s*\()",
        "STATICCALL": r"(?:\.\s*staticcall\s*\(|\bstaticcall\s*\()",
        "CREATE": r"\bcreate\s*\(",
        "CREATE2": r"\bcreate2\s*\(",
        "RETURN": r"\breturn\b",
        "REVERT": r"\b(?:revert|require|assert)\b",
        "SELFDESTRUCT": r"\bselfdestruct\s*\(",
    }
    errors = [
        f"code represents {opcode} but the target contains no {opcode} opcode"
        for opcode, pattern in patterns.items()
        if opcode not in observed_opcodes and re.search(pattern, code)
    ]
    for match in re.finditer(r"\blog([0-4])\s*\(", code):
        opcode = f"LOG{match.group(1)}"
        if opcode not in observed_opcodes:
            errors.append(f"code represents {opcode} but the target contains no {opcode} opcode")
    return errors


def _validate_solidity_statement_block(solidity: str) -> list[str]:
    errors: list[str] = []
    code = solidity.strip()
    if not code:
        return ["solidity cannot be empty"]
    if len(code) > MAX_SOLIDITY_CHARS:
        errors.append(f"solidity exceeds {MAX_SOLIDITY_CHARS} characters")
    if "```" in code:
        errors.append("solidity must not contain markdown fences")
    if not code.startswith("{") or not code.endswith("}"):
        errors.append("solidity must be exactly one statement block enclosed by braces")

    uncommented = _strip_solidity_comments(code)
    forbidden = re.search(r"\b(?:pragma|import|contract|interface|library|function)\b", uncommented)
    if forbidden:
        errors.append(f"solidity statement block cannot contain {forbidden.group(0)}")
    delimiter_error = _balanced_delimiter_error(code)
    if delimiter_error:
        errors.append(delimiter_error)
    return errors


def _strip_solidity_comments(code: str) -> str:
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", code)


def _balanced_delimiter_error(code: str) -> str | None:
    opening = {"(": ")", "[": "]", "{": "}"}
    closing = {value: key for key, value in opening.items()}
    stack: list[str] = []
    index = 0
    state = "normal"
    quote = ""
    while index < len(code):
        char = code[index]
        nxt = code[index + 1] if index + 1 < len(code) else ""
        if state == "line_comment":
            if char == "\n":
                state = "normal"
        elif state == "block_comment":
            if char == "*" and nxt == "/":
                state = "normal"
                index += 1
        elif state == "string":
            if char == "\\":
                index += 1
            elif char == quote:
                state = "normal"
        elif char == "/" and nxt == "/":
            state = "line_comment"
            index += 1
        elif char == "/" and nxt == "*":
            state = "block_comment"
            index += 1
        elif char in {"'", '"'}:
            state = "string"
            quote = char
        elif char in opening:
            stack.append(char)
        elif char in closing:
            if not stack or stack[-1] != closing[char]:
                return f"unbalanced delimiter {char}"
            stack.pop()
        index += 1
    if state in {"block_comment", "string"}:
        return "unterminated comment or string literal"
    if stack:
        return f"unclosed delimiter {stack[-1]}"
    return None


def _public_reconstruction(
    generated: dict[str, Any],
    context_payload: dict[str, Any],
) -> dict[str, Any]:
    evidence = generated.get("evidence", [])
    unresolved = [
        item.strip()
        for item in generated.get("unresolved", [])
        if isinstance(item, str) and item.strip()
    ][:MAX_UNRESOLVED_ITEMS]
    return {
        "kind": "solidity_statement_block",
        "solidity": str(generated["solidity"]).strip(),
        "confidence": _calibrate_confidence(
            str(generated.get("confidence", "low")),
            unresolved,
            context_payload,
        ),
        "unresolved": unresolved,
        "validation": {
            "format": "validated",
            "evidence_items": len(evidence) if isinstance(evidence, list) else 0,
        },
    }


def _calibrate_confidence(
    model_confidence: str,
    unresolved: list[str],
    context_payload: dict[str, Any],
) -> str:
    rank = CONFIDENCE_RANK.get(model_confidence, 0)
    target = context_payload.get("target", {})
    step_count = _safe_int(target.get("step_count")) if isinstance(target, dict) else 0
    histogram = target.get("opcode_histogram", {}) if isinstance(target, dict) else {}
    active_frame = context_payload.get("call_context", {}).get("active_frame", {})
    signature_candidates = active_frame.get("signature_candidates", []) if isinstance(active_frame, dict) else []

    if unresolved:
        rank = min(rank, CONFIDENCE_RANK["medium"])
    if step_count is not None and step_count > 1500:
        rank = CONFIDENCE_RANK["low"]
    elif step_count is not None and step_count > 400:
        rank = min(rank, CONFIDENCE_RANK["medium"])
    if isinstance(histogram, dict) and ("INVALID" in histogram or "SELFDESTRUCT" in histogram):
        rank = min(rank, CONFIDENCE_RANK["medium"])
    if isinstance(histogram, dict) and any(op.startswith("CALLDATA") for op in histogram) and not signature_candidates:
        rank = min(rank, CONFIDENCE_RANK["medium"])
    return next(name for name, value in CONFIDENCE_RANK.items() if value == rank)


def _get_timeout_seconds() -> float:
    raw_timeout = os.environ.get("DEEPSEEK_CFG_TIMEOUT_SECONDS", "").strip()
    try:
        timeout = float(raw_timeout) if raw_timeout else DEFAULT_LLM_TIMEOUT_SECONDS
    except ValueError:
        timeout = DEFAULT_LLM_TIMEOUT_SECONDS
    return timeout if timeout > 0 else DEFAULT_LLM_TIMEOUT_SECONDS


def _get_max_retries() -> int:
    raw_retries = os.environ.get("DEEPSEEK_CFG_MAX_RETRIES", "").strip()
    try:
        retries = int(raw_retries) if raw_retries else DEFAULT_LLM_MAX_RETRIES
    except ValueError:
        retries = DEFAULT_LLM_MAX_RETRIES
    return max(retries, 0)


def _get_max_output_tokens() -> int:
    raw_tokens = os.environ.get("DEEPSEEK_CFG_MAX_OUTPUT_TOKENS", "").strip()
    try:
        tokens = int(raw_tokens) if raw_tokens else DEFAULT_MAX_OUTPUT_TOKENS
    except ValueError:
        tokens = DEFAULT_MAX_OUTPUT_TOKENS
    return max(1024, min(tokens, 32768))


def _build_opcode_step_context(steps: list[Any], idx: int) -> dict[str, Any]:
    current = steps[idx] if idx < len(steps) and isinstance(steps[idx], dict) else {}
    nxt = steps[idx + 1] if idx + 1 < len(steps) and isinstance(steps[idx + 1], dict) else {}

    opcode = str(current.get("opcode", "")).upper()
    stack_before = _as_str_list(current.get("stack"))
    after_observed = bool(
        nxt
        and current.get("depth") == nxt.get("depth")
        and str(current.get("address", "")).lower() == str(nxt.get("address", "")).lower()
    )
    stack_after = _as_str_list(nxt.get("stack")) if after_observed else []
    memory_before = _as_str_list(current.get("memory"))
    memory_after = _as_str_list(nxt.get("memory")) if after_observed else memory_before

    context: dict[str, Any] = {
        "step_index": idx,
        "address": current.get("address"),
        "rw_address": current.get("RW_address"),
        "depth": current.get("depth"),
        "pc": current.get("pc"),
        "opcode": opcode,
        "gascost": current.get("gascost"),
        "stack": _build_stack_context(
            opcode,
            stack_before,
            stack_after,
            after_observed=after_observed,
        ),
    }

    operands = _extract_stack_operands(opcode, stack_before)
    if operands:
        context["opcode_operands"] = operands

    memory_context = _build_memory_context(opcode, stack_before, memory_before, memory_after)
    if memory_context:
        context["memory"] = memory_context

    return context


def build_plain_semantics_payload(
    steps: list[Any],
    plain_blocks: dict[str, Any],
) -> dict[str, Any]:
    """Project full trace steps into the compact contexts consumed by the LLM."""
    if not isinstance(steps, list):
        raise TypeError("steps must be a list")
    if not isinstance(plain_blocks, dict):
        raise TypeError("plain_blocks must be an object")

    block_semantics: dict[str, list[dict[str, Any]]] = {}
    for key, raw_block in plain_blocks.items():
        if not isinstance(raw_block, dict):
            continue
        try:
            block_id = str(raw_block.get("block_id", key))
            start_step = int(raw_block["start_step"])
            end_step = int(raw_block["end_step"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid plain block range for {key}") from exc
        if start_step >= 0 and end_step == -1:
            end_step = len(steps) - 1
        if start_step < 0 or end_step < start_step or end_step >= len(steps):
            raise ValueError(
                f"Plain block {block_id} range {start_step}..{end_step} exceeds trace bounds"
            )
        block_semantics[block_id] = [
            _build_opcode_step_context(steps, index)
            for index in range(start_step, end_step + 1)
        ]

    return {
        "schema_version": PLAIN_SEMANTICS_SCHEMA_VERSION,
        "trace_step_count": len(steps),
        "blocks": block_semantics,
    }


def write_plain_semantics_artifact(
    output_path: str,
    steps: list[Any],
    plain_blocks: dict[str, Any],
) -> dict[str, Any]:
    payload = build_plain_semantics_payload(steps, plain_blocks)
    temp_path = f"{output_path}.tmp"
    with gzip.open(temp_path, "wt", encoding="utf-8", compresslevel=6) as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(temp_path, output_path)
    return payload


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value]


def _build_stack_context(
    opcode: str,
    stack_before: list[str],
    stack_after: list[str],
    *,
    after_observed: bool = True,
) -> dict[str, Any]:
    stack_delta = len(stack_after) - len(stack_before) if after_observed else None
    pushed_values: list[str] = []
    popped_count = 0
    if stack_delta is not None and stack_delta > 0:
        pushed_values = stack_after[-stack_delta:]
    elif stack_delta is not None and stack_delta < 0:
        popped_count = -stack_delta

    return {
        "before_size": len(stack_before),
        "after_size": len(stack_after),
        "top_before": stack_before[-STACK_TOP_LIMIT:],
        "top_after": stack_after[-STACK_TOP_LIMIT:] if after_observed else [],
        "after_observed": after_observed,
        "delta": stack_delta,
        "popped_count": popped_count,
        "pushed_values": pushed_values,
        "focus_operands_from_top": _extract_stack_operands(opcode, stack_before),
    }


def _extract_stack_operands(opcode: str, stack_before: list[str]) -> dict[str, str]:
    # top_1 means stack top (first popped operand)
    op = opcode.upper()

    if op.startswith("LOG") and len(op) == 4 and op[-1].isdigit():
        topic_count = int(op[-1])
        names = ["mem_offset", "mem_size"] + [f"topic_{i + 1}" for i in range(topic_count)]
        return _pick_top_operands(stack_before, names)

    operand_hints: dict[str, list[str]] = {
        "JUMP": ["destination"],
        "JUMPI": ["destination", "condition"],
        "CALLDATALOAD": ["calldata_offset"],
        "BALANCE": ["account"],
        "EXTCODESIZE": ["account"],
        "EXTCODEHASH": ["account"],
        "BLOCKHASH": ["block_number"],
        "SLOAD": ["slot"],
        "SSTORE": ["slot", "value"],
        "MLOAD": ["mem_offset"],
        "MSTORE": ["mem_offset", "value"],
        "MSTORE8": ["mem_offset", "value"],
        "KECCAK256": ["mem_offset", "mem_size"],
        "SHA3": ["mem_offset", "mem_size"],
        "CALLDATACOPY": ["mem_offset", "calldata_offset", "size"],
        "CODECOPY": ["mem_offset", "code_offset", "size"],
        "RETURNDATACOPY": ["mem_offset", "returndata_offset", "size"],
        "MCOPY": ["dst_mem_offset", "src_mem_offset", "size"],
        "RETURN": ["mem_offset", "mem_size"],
        "REVERT": ["mem_offset", "mem_size"],
        "CREATE": ["value", "init_mem_offset", "init_mem_size"],
        "CREATE2": ["value", "init_mem_offset", "init_mem_size", "salt"],
        "CALL": ["gas", "to", "value", "in_offset", "in_size", "out_offset", "out_size"],
        "CALLCODE": ["gas", "to", "value", "in_offset", "in_size", "out_offset", "out_size"],
        "DELEGATECALL": ["gas", "to", "in_offset", "in_size", "out_offset", "out_size"],
        "STATICCALL": ["gas", "to", "in_offset", "in_size", "out_offset", "out_size"],
    }
    names = operand_hints.get(op)
    if not names:
        return {}
    return _pick_top_operands(stack_before, names)


def _pick_top_operands(stack_before: list[str], names: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for i, name in enumerate(names):
        if i >= len(stack_before):
            break
        result[name] = stack_before[-(i + 1)]
    return result


def _build_memory_context(
    opcode: str, stack_before: list[str], memory_before: list[str], memory_after: list[str]
) -> dict[str, Any]:
    before_bytes = len(memory_before) * 32
    after_bytes = len(memory_after) * 32
    memory_ctx: dict[str, Any] = {
        "before_size_bytes": before_bytes,
        "after_size_bytes": after_bytes,
    }

    changed = _memory_changed_summary(memory_before, memory_after)
    if changed:
        memory_ctx["changes"] = changed

    reads, writes = _opcode_memory_access(opcode, stack_before, memory_before, memory_after)
    if reads:
        memory_ctx["reads"] = reads
    if writes:
        memory_ctx["writes"] = writes

    if len(memory_ctx.keys()) <= 2 and "changes" not in memory_ctx:
        return {}
    return memory_ctx


def _opcode_memory_access(
    opcode: str, stack_before: list[str], memory_before: list[str], memory_after: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    op = opcode.upper()
    reads: list[dict[str, Any]] = []
    writes: list[dict[str, Any]] = []

    def top_int(n: int) -> int | None:
        if len(stack_before) < n:
            return None
        return _hex_to_int(stack_before[-n])

    if op == "MLOAD":
        offset = top_int(1)
        if offset is not None:
            reads.append(_memory_window_entry("mload", memory_before, offset, 32))
    elif op == "MSTORE":
        offset = top_int(1)
        value = stack_before[-2] if len(stack_before) >= 2 else None
        if offset is not None:
            writes.append(_memory_write_entry("mstore", memory_after, offset, 32, value))
    elif op == "MSTORE8":
        offset = top_int(1)
        value = stack_before[-2] if len(stack_before) >= 2 else None
        if offset is not None:
            writes.append(_memory_write_entry("mstore8", memory_after, offset, 1, value))
    elif op in {"KECCAK256", "SHA3"}:
        offset = top_int(1)
        size = top_int(2)
        if offset is not None and size is not None:
            reads.append(_memory_window_entry("hash_input", memory_before, offset, size))
    elif op in {"RETURN", "REVERT"}:
        offset = top_int(1)
        size = top_int(2)
        if offset is not None and size is not None:
            reads.append(_memory_window_entry("return_data", memory_before, offset, size))
    elif op in {"CALLDATACOPY", "CODECOPY", "RETURNDATACOPY"}:
        mem_offset = top_int(1)
        size = top_int(3)
        if mem_offset is not None and size is not None:
            writes.append(_memory_write_entry("copy_dst", memory_after, mem_offset, size, None))
    elif op == "MCOPY":
        dst = top_int(1)
        src = top_int(2)
        size = top_int(3)
        if src is not None and size is not None:
            reads.append(_memory_window_entry("mcopy_src", memory_before, src, size))
        if dst is not None and size is not None:
            writes.append(_memory_write_entry("mcopy_dst", memory_after, dst, size, None))
    elif op in {"CALL", "CALLCODE"}:
        in_offset = top_int(4)
        in_size = top_int(5)
        out_offset = top_int(6)
        out_size = top_int(7)
        if in_offset is not None and in_size is not None:
            reads.append(_memory_window_entry("call_input", memory_before, in_offset, in_size))
        if out_offset is not None and out_size is not None:
            writes.append(_memory_write_entry("call_output", memory_after, out_offset, out_size, None))
    elif op in {"DELEGATECALL", "STATICCALL"}:
        in_offset = top_int(3)
        in_size = top_int(4)
        out_offset = top_int(5)
        out_size = top_int(6)
        if in_offset is not None and in_size is not None:
            reads.append(_memory_window_entry("call_input", memory_before, in_offset, in_size))
        if out_offset is not None and out_size is not None:
            writes.append(_memory_write_entry("call_output", memory_after, out_offset, out_size, None))
    elif op in {"CREATE", "CREATE2"}:
        offset = top_int(2)
        size = top_int(3)
        if offset is not None and size is not None:
            reads.append(_memory_window_entry("init_code", memory_before, offset, size))
    elif op.startswith("LOG") and len(op) == 4 and op[-1].isdigit():
        offset = top_int(1)
        size = top_int(2)
        if offset is not None and size is not None:
            reads.append(_memory_window_entry("log_data", memory_before, offset, size))

    return reads, writes


def _memory_window_entry(
    tag: str, memory_words: list[str], offset: int, size: int
) -> dict[str, Any]:
    window = _memory_window(memory_words, offset, size, MEMORY_WINDOW_MAX_BYTES)
    return {"type": tag, **window}


def _memory_write_entry(
    tag: str, memory_words_after: list[str], offset: int, size: int, source_value: str | None
) -> dict[str, Any]:
    entry = _memory_window_entry(tag, memory_words_after, offset, size)
    if source_value is not None:
        entry["source_value"] = source_value
    return entry


def _memory_changed_summary(memory_before: list[str], memory_after: list[str]) -> dict[str, Any] | None:
    before_words = [_normalize_memory_word(w) for w in memory_before]
    after_words = [_normalize_memory_word(w) for w in memory_after]
    max_len = max(len(before_words), len(after_words))
    changed_indexes: list[int] = []
    for i in range(max_len):
        before = before_words[i] if i < len(before_words) else "0" * 64
        after = after_words[i] if i < len(after_words) else "0" * 64
        if before != after:
            changed_indexes.append(i)

    if not changed_indexes:
        return None

    ranges: list[dict[str, int]] = []
    range_start = changed_indexes[0]
    prev_idx = changed_indexes[0]
    for idx in changed_indexes[1:]:
        if idx == prev_idx + 1:
            prev_idx = idx
            continue
        ranges.append({"start_word": range_start, "end_word": prev_idx})
        range_start = idx
        prev_idx = idx
    ranges.append({"start_word": range_start, "end_word": prev_idx})

    changed_values: list[dict[str, Any]] = []
    for idx in changed_indexes[:CHANGED_WORD_VALUE_LIMIT]:
        before = before_words[idx] if idx < len(before_words) else "0" * 64
        after = after_words[idx] if idx < len(after_words) else "0" * 64
        changed_values.append(
            {
                "word_index": idx,
                "before": f"0x{before}",
                "after": f"0x{after}",
            }
        )

    return {
        "changed_word_count": len(changed_indexes),
        "changed_word_ranges": ranges,
        "changed_word_values": changed_values,
    }


def _memory_window(
    memory_words: list[str], offset: int, size: int, max_bytes: int
) -> dict[str, Any]:
    normalized_offset = max(int(offset), 0)
    normalized_size = max(int(size), 0)
    if normalized_size == 0:
        return {
            "offset": normalized_offset,
            "size_bytes": 0,
            "slice_hex": "0x",
            "truncated": False,
        }

    take_bytes = min(normalized_size, max_bytes)
    mem_hex = "".join(_normalize_memory_word(w) for w in memory_words)
    start = normalized_offset * 2
    end = start + take_bytes * 2
    chunk = mem_hex[start:end]
    if len(chunk) < take_bytes * 2:
        chunk = chunk + ("0" * (take_bytes * 2 - len(chunk)))

    return {
        "offset": normalized_offset,
        "size_bytes": normalized_size,
        "slice_hex": f"0x{chunk}",
        "truncated": normalized_size > take_bytes,
    }


def _normalize_memory_word(value: str) -> str:
    text = str(value).strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    text = re.sub(r"[^0-9a-f]", "", text)
    if len(text) > 64:
        text = text[-64:]
    if len(text) < 64:
        text = text.zfill(64)
    return text


def _hex_to_int(value: str) -> int | None:
    text = str(value).strip().lower()
    if not text:
        return None
    try:
        if text.startswith("0x"):
            return int(text, 16)
        if re.fullmatch(r"[0-9]+", text):
            return int(text, 10)
        return int(text, 16)
    except Exception:
        return None
