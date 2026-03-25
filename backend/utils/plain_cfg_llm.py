import copy
import hashlib
import json
import os
import re
import threading
from typing import Any

from openai import OpenAI

PROMPT_VERSION = "plain_cfg_opcode_objective_v4"
DEFAULT_MODEL = "gpt-5.4-nano"
DEFAULT_MAX_INPUT_CHARS = 400000
STACK_TOP_LIMIT = 10
MEMORY_WINDOW_MAX_BYTES = 192
CHANGED_WORD_VALUE_LIMIT = 24
MIN_DESCRIPTION_SENTENCES = 2
MAX_DESCRIPTION_SENTENCES = 3
_RUNTIME_CACHE_LOCK = threading.Lock()
_RUNTIME_CACHE_BY_TX: dict[str, dict[str, Any]] = {}


class PlainCfgLlmServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def analyze_plain_cfg_block(tx_hash: str, block_id: str | int, force_refresh: bool = False) -> dict[str, Any]:
    context_payload, context_meta = build_plain_cfg_block_context(tx_hash, block_id)
    target_block_id = context_meta["target_block_id"]
    context_json = json.dumps(context_payload, ensure_ascii=False, separators=(",", ":"))
    context_hash = hashlib.sha256(context_json.encode("utf-8")).hexdigest()
    _check_context_size(context_json)

    cache_data = _load_runtime_cache(tx_hash)
    block_cache_key = str(target_block_id)
    cached_item = cache_data["items"].get(block_cache_key)
    if (
        not force_refresh
        and isinstance(cached_item, dict)
        and cached_item.get("prompt_version") == PROMPT_VERSION
        and cached_item.get("context_hash") == context_hash
        and isinstance(cached_item.get("title"), str)
        and isinstance(cached_item.get("description"), str)
        and cached_item.get("title", "").strip()
        and cached_item.get("description", "").strip()
    ):
        return {
            "status": "success",
            "source": "cache",
            "analysis": {
                "title": cached_item["title"].strip(),
                "description": cached_item["description"].strip(),
            },
            "context_meta": context_meta,
        }

    analysis = _generate_analysis(context_json)
    model_name = os.environ.get("OPENAI_SEMANTIC_CFG_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    cache_data["prompt_version"] = PROMPT_VERSION
    cache_data["items"][block_cache_key] = {
        "title": analysis["title"],
        "description": analysis["description"],
        "model": model_name,
        "prompt_version": PROMPT_VERSION,
        "context_hash": context_hash,
    }
    _save_runtime_cache(tx_hash, cache_data)

    return {
        "status": "success",
        "source": "llm",
        "analysis": analysis,
        "context_meta": context_meta,
    }


def build_plain_cfg_block_context(tx_hash: str, block_id: str | int) -> tuple[dict[str, Any], dict[str, Any]]:
    ordered_blocks, steps = _load_plain_cfg_context_inputs(tx_hash)
    target_idx = _find_target_index(ordered_blocks, block_id)
    if target_idx is None:
        raise PlainCfgLlmServiceError(404, f"Block {block_id} not found in plain CFG")
    return _build_context_for_index(tx_hash, steps, ordered_blocks, target_idx)


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
    tx_dir_name = tx_hash.lower().lstrip("0x")
    result_dir = os.path.join("Result", tx_dir_name)
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


def _load_plain_cfg_context_inputs(tx_hash: str) -> tuple[list[dict[str, Any]], list[Any]]:
    result_dir = _resolve_result_dir(tx_hash)
    plain_info_path = os.path.join(result_dir, "plain_blocks_information.json")
    trace_path = os.path.join(result_dir, "trace.json")

    plain_info = _load_json_file(plain_info_path)
    trace_data = _load_json_file(trace_path)
    steps = trace_data.get("steps")
    if not isinstance(steps, list):
        raise PlainCfgLlmServiceError(404, "trace.json is missing valid steps data")

    return _build_ordered_plain_blocks(plain_info), steps


def _build_context_for_index(
    tx_hash: str, steps: list[Any], ordered_blocks: list[dict[str, Any]], target_idx: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    prev_block = ordered_blocks[target_idx - 1] if target_idx > 0 else None
    target_block = ordered_blocks[target_idx]
    next_block = ordered_blocks[target_idx + 1] if target_idx + 1 < len(ordered_blocks) else None
    return _build_context_payload(
        tx_hash=tx_hash,
        steps=steps,
        target_block=target_block,
        prev_block=prev_block,
        next_block=next_block,
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


def _extract_block_steps(steps: list[Any], block: dict[str, Any]) -> list[Any]:
    start = block["start_step"]
    end = block["end_step"]
    if start < 0 or end < start:
        raise PlainCfgLlmServiceError(
            400, f"Invalid step range for block {block['block_id']}: {start}..{end}"
        )
    if end >= len(steps):
        raise PlainCfgLlmServiceError(
            400, f"Step range exceeds trace bounds for block {block['block_id']}: {start}..{end}"
        )
    return [_build_opcode_step_context(steps, idx) for idx in range(start, end + 1)]


def _build_context_payload(
    tx_hash: str,
    steps: list[Any],
    target_block: dict[str, Any],
    prev_block: dict[str, Any] | None,
    next_block: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    context_blocks: list[dict[str, Any]] = []

    def add_block(relation: str, block: dict[str, Any]) -> None:
        context_blocks.append(
            {
                "relation": relation,
                "block_id": block["block_id"],
                "address": block["address"],
                "start_step": block["start_step"],
                "end_step": block["end_step"],
                "actions": copy.deepcopy(block.get("actions", [])),
                "trace_steps": _extract_block_steps(steps, block),
            }
        )

    if prev_block is not None:
        add_block("prev", prev_block)
    add_block("target", target_block)
    if next_block is not None:
        add_block("next", next_block)

    context_payload = {
        "tx_hash": tx_hash,
        "mode": "plain_cfg_step_neighbor_opcode_focused",
        "target_block_id": target_block["block_id"],
        "blocks": context_blocks,
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
    }
    return context_payload, context_meta


def _check_context_size(context_json: str) -> None:
    raw_limit = os.environ.get("OPENAI_SEMANTIC_CFG_MAX_INPUT_CHARS", "").strip()
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


def _generate_analysis(context_json: str) -> dict[str, str]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise PlainCfgLlmServiceError(500, "OPENAI_API_KEY is not configured")

    api_mode = os.environ.get("OPENAI_SEMANTIC_CFG_API_MODE", "chat_completions").strip().lower()
    if api_mode != "chat_completions":
        raise PlainCfgLlmServiceError(
            500, f"Unsupported OPENAI_SEMANTIC_CFG_API_MODE: {api_mode}. Expected chat_completions."
        )

    base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or None
    model_name = os.environ.get("OPENAI_SEMANTIC_CFG_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    client = OpenAI(api_key=api_key, base_url=base_url)
    system_prompt = (
        "You are an EVM opcode domain expert and transaction-semantics analyst. "
        "Interpret opcode behavior using opcode context plus stack and memory state transitions."
    )
    base_user_prompt = (
        "Analyze the selected plain-CFG block with its step-neighbor context. "
        "Stay objective and evidence-based: do not assume the transaction is MEV or arbitrage, and do not assume "
        "the node must belong to predefined roles like core operation, profit calculation, or protection checks. "
        "Infer such roles only when the opcode/stack/memory evidence supports them.\n"
        "Use a semantic level higher than pure control-flow narration by explaining likely transactional intent, "
        "state-transition meaning, and potential economic/logic implications, while explicitly noting uncertainty when needed.\n"
        "The provided trace is opcode-focused. When reasoning about opcodes, explicitly use stack-top values, "
        "opcode operands, and memory reads/writes/changes.\n"
        "Keep the wording compact and high level. Prefer semantic roles instead of copying raw trace literals.\n"
        "Do not mention specific pc values. Do not print full hexadecimal addresses. Do not mention raw numeric offsets "
        "or exact memory positions unless omitting them would hide the core behavior.\n"
        "Return strict JSON only:\n"
        "{\n"
        '  "title": "short English title",\n'
        '  "description": "2-3 short English sentences"\n'
        "}\n"
        "Do not use markdown. Do not return extra keys."
    )

    for attempt in range(2):
        strict_note = ""
        if attempt > 0:
            strict_note = (
                "\nThe previous output failed validation. "
                "Retry with exactly 2 or 3 short complete English sentences in description, while keeping the wording abstract."
            )

        try:
            response = client.chat.completions.create(
                model=model_name,
                temperature=0.2,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"{base_user_prompt}{strict_note}\n\nContext JSON:\n{context_json}"},
                ],
            )
        except Exception as exc:
            raise PlainCfgLlmServiceError(502, f"LLM request failed: {exc}") from exc

        raw_text = _extract_response_text(response)
        parsed = _parse_response_json(raw_text)
        if parsed is None:
            continue

        title = str(parsed.get("title", "")).strip()
        description = str(parsed.get("description", "")).strip()
        if not title or not description:
            continue

        sentence_count = _count_sentences(description)
        if MIN_DESCRIPTION_SENTENCES <= sentence_count <= MAX_DESCRIPTION_SENTENCES:
            return {"title": title, "description": description}

    raise PlainCfgLlmServiceError(
        502,
        (
            "LLM returned invalid format. Expected JSON with title and "
            f"{MIN_DESCRIPTION_SENTENCES}-{MAX_DESCRIPTION_SENTENCES} sentence description."
        ),
    )


def _extract_response_text(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    if message is None:
        return ""

    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif hasattr(item, "text"):
                parts.append(str(getattr(item, "text")))
        return "\n".join(parts).strip()
    return str(content).strip()


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
        match = re.search(r"\{.*\}", candidate, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None


def _count_sentences(text: str) -> int:
    normalized = text.strip()
    if not normalized:
        return 0
    sentences = re.split(r"(?<=[.!?])\s+", normalized)
    return len([s for s in sentences if s.strip()])


def _build_opcode_step_context(steps: list[Any], idx: int) -> dict[str, Any]:
    current = steps[idx] if idx < len(steps) and isinstance(steps[idx], dict) else {}
    nxt = steps[idx + 1] if idx + 1 < len(steps) and isinstance(steps[idx + 1], dict) else {}

    opcode = str(current.get("opcode", "")).upper()
    stack_before = _as_str_list(current.get("stack"))
    stack_after = _as_str_list(nxt.get("stack")) if nxt else []
    memory_before = _as_str_list(current.get("memory"))
    memory_after = _as_str_list(nxt.get("memory")) if nxt else []

    context: dict[str, Any] = {
        "step_index": idx,
        "address": current.get("address"),
        "rw_address": current.get("RW_address"),
        "depth": current.get("depth"),
        "pc": current.get("pc"),
        "opcode": opcode,
        "gascost": current.get("gascost"),
        "stack": _build_stack_context(opcode, stack_before, stack_after),
    }

    operands = _extract_stack_operands(opcode, stack_before)
    if operands:
        context["opcode_operands"] = operands

    memory_context = _build_memory_context(opcode, stack_before, memory_before, memory_after)
    if memory_context:
        context["memory"] = memory_context

    return context


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value]


def _build_stack_context(opcode: str, stack_before: list[str], stack_after: list[str]) -> dict[str, Any]:
    stack_delta = len(stack_after) - len(stack_before)
    pushed_values: list[str] = []
    popped_count = 0
    if stack_delta > 0:
        pushed_values = stack_after[-stack_delta:]
    elif stack_delta < 0:
        popped_count = -stack_delta

    return {
        "before_size": len(stack_before),
        "after_size": len(stack_after),
        "top_before": stack_before[-STACK_TOP_LIMIT:],
        "top_after": stack_after[-STACK_TOP_LIMIT:],
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
