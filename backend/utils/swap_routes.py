"""Strict-step swap-leg extraction and ordered arbitrage-cycle detection.

The detector deliberately ignores names, selectors, signatures, logs, and
protocol metadata.  Its only semantic inputs are TFG transfers and CALLTREE
address/parent/step structure.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable


MAX_ROUTE_HOPS = 8
MAX_AMOUNT_RELATIVE_GAP = 0.01
DETECTOR_SCHEMA_VERSION = 10
NATIVE_TOKEN_ADDRESS = "eth"
# Canonical Ethereum mainnet WETH.  This is an address-level asset identity,
# not a contract name, selector, ABI, event, or public-interface dependency.
WRAPPED_NATIVE_TOKEN_ADDRESSES = frozenset({
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
})


def _address(value: Any) -> str:
    return str(value or "").strip().lower()


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _wrapped_native_pair(first: str, second: str) -> bool:
    return (
        first == NATIVE_TOKEN_ADDRESS and second in WRAPPED_NATIVE_TOKEN_ADDRESSES
    ) or (
        second == NATIVE_TOKEN_ADDRESS and first in WRAPPED_NATIVE_TOKEN_ADDRESSES
    )


def _route_tokens_compatible(first: str, second: str) -> bool:
    return first == second or _wrapped_native_pair(first, second)


def _economic_asset(token: str) -> str:
    return "native" if token == NATIVE_TOKEN_ADDRESS or token in WRAPPED_NATIVE_TOKEN_ADDRESSES else token


@dataclass(frozen=True)
class _Frame:
    call_id: int
    parent_call_id: int | None
    depth: int
    entry_step: int
    exit_step: int
    from_address: str
    to_address: str


def _flatten_call_tree(trace_tree: dict[str, Any] | None) -> tuple[str, list[_Frame]]:
    """Accept the nested runtime tree or the persisted flat call-tree payload."""
    if not isinstance(trace_tree, dict) or not trace_tree:
        return "", []

    if isinstance(trace_tree.get("calls"), list) and isinstance(trace_tree.get("root"), dict):
        root_address = _address(trace_tree["root"].get("address"))
        frames: list[_Frame] = []
        for raw in trace_tree["calls"]:
            if not isinstance(raw, dict):
                continue
            call_id = _integer(raw.get("call_id"))
            entry_step = _integer(raw.get("entry_step"))
            exit_step = _integer(raw.get("exit_step"))
            if call_id is None or entry_step is None or exit_step is None:
                continue
            frames.append(_Frame(
                call_id=call_id,
                parent_call_id=_integer(raw.get("parent_call_id")),
                depth=_integer(raw.get("depth")) or 0,
                entry_step=entry_step,
                exit_step=exit_step,
                from_address=_address(raw.get("from_address")),
                to_address=_address(raw.get("to_address")),
            ))
        return root_address, frames

    root_address = _address(trace_tree.get("contract"))
    frames = []

    def visit(node: dict[str, Any], parent_id: int | None, caller: str, depth: int) -> None:
        call_id = len(frames) + 1
        entry_step = _integer(node.get("entry_step"))
        exit_step = _integer(node.get("exit_step"))
        if entry_step is None or exit_step is None:
            return
        target = _address(node.get("contract"))
        frames.append(_Frame(
            call_id=call_id,
            parent_call_id=parent_id,
            depth=depth,
            entry_step=entry_step,
            exit_step=exit_step,
            from_address=caller,
            to_address=target,
        ))
        for child in node.get("calls", []):
            if isinstance(child, dict):
                visit(child, call_id, target, depth + 1)

    for child in trace_tree.get("calls", []):
        if isinstance(child, dict):
            visit(child, None, root_address, 1)
    return root_address, frames


def _parent_map(frames: Iterable[_Frame]) -> dict[int, int | None]:
    return {frame.call_id: frame.parent_call_id for frame in frames}


def _ancestor_ids(call_id: int | None, parents: dict[int, int | None]) -> list[int]:
    result: list[int] = []
    current = call_id
    while current is not None:
        result.append(current)
        current = parents.get(current)
    return result


def _is_descendant(call_id: int | None, ancestor_id: int | None, parents: dict[int, int | None]) -> bool:
    if ancestor_id is None:
        return True
    return ancestor_id in _ancestor_ids(call_id, parents)


def _lca(call_ids: Iterable[int | None], parents: dict[int, int | None]) -> int | None:
    concrete = [call_id for call_id in call_ids if call_id is not None]
    if not concrete:
        return None
    ancestor_lists = [_ancestor_ids(call_id, parents) for call_id in concrete]
    common = set(ancestor_lists[0])
    for ancestors in ancestor_lists[1:]:
        common.intersection_update(ancestors)
    if not common:
        return None
    return next(call_id for call_id in ancestor_lists[0] if call_id in common)


def _deepest_frame_for_step(step: int, frames: list[_Frame]) -> _Frame | None:
    matches = [
        frame for frame in frames
        if frame.entry_step <= step <= frame.exit_step
    ]
    if not matches:
        return None
    return max(matches, key=lambda frame: (frame.depth, -frame.entry_step))


def _transfer_steps(transfer: dict[str, Any]) -> list[int]:
    raw = transfer.get("source_steps")
    values: list[Any]
    if isinstance(raw, dict):
        # State-changing steps are the strict transfer anchors.  SLOAD steps
        # remain available on the original TFG edge for drill-down.
        values = [raw.get("sender_sstore_step"), raw.get("receiver_sstore_step")]
    elif isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        values = []
    result = [_integer(value) for value in values]
    return [value for value in result if value is not None]


def _transfer_amount_raw(transfer: dict[str, Any]) -> int | None:
    raw = _integer(transfer.get("amount_raw"))
    if raw is not None:
        return abs(raw)
    # Compatibility for synthetic/legacy tests.  Production pairing now
    # persists amount_raw, so float conversion is not used in new artifacts.
    amount = transfer.get("amount")
    if isinstance(amount, (int, float)):
        return abs(int(amount))
    return None


def map_transfers_to_token_calls(
    paired: list[dict[str, Any]],
    trace_tree: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[int, _Frame], dict[int, int | None]]:
    """Bind every usable TFG transfer to exact CALLTREE frames via source steps."""
    root_address, frames = _flatten_call_tree(trace_tree)
    del root_address  # Root-only steps are deliberately not guessed into token calls.
    by_id = {frame.call_id: frame for frame in frames}
    parents = _parent_map(frames)
    mapped: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []

    for transfer in paired:
        edge_id = _integer(transfer.get("order"))
        if edge_id is None or edge_id == 0:
            continue
        token_address = _address(transfer.get("token_addr") or transfer.get("token"))
        steps = _transfer_steps(transfer)
        if not steps:
            diagnostics.append({"edge_id": edge_id, "status": "missing_step"})
            continue

        mutation_frames = [_deepest_frame_for_step(step, frames) for step in steps]
        if any(frame is None for frame in mutation_frames):
            diagnostics.append({"edge_id": edge_id, "status": "unmapped_step", "source_steps": steps})
            continue

        deepest_ids = [frame.call_id for frame in mutation_frames if frame is not None]
        evidence_call_id: int | None
        token_call_id: int | None
        is_native = token_address == "eth"
        if is_native:
            evidence_call_id = deepest_ids[-1]
            token_call_id = None
        else:
            common_id = _lca(deepest_ids, parents)
            token_call_id = None
            for ancestor_id in _ancestor_ids(common_id, parents):
                frame = by_id.get(ancestor_id)
                if frame is not None and frame.to_address == token_address:
                    token_call_id = ancestor_id
                    break
            if token_call_id is None:
                diagnostics.append({
                    "edge_id": edge_id,
                    "status": "unmapped_token_call",
                    "token_address": token_address,
                    "source_steps": steps,
                })
                continue
            evidence_call_id = token_call_id

        amount_raw = _transfer_amount_raw(transfer)
        if amount_raw is None:
            diagnostics.append({"edge_id": edge_id, "status": "missing_raw_amount"})
            continue
        mapped.append({
            "edge_id": edge_id,
            "token_address": token_address,
            "from_address": _address(transfer.get("from")),
            "to_address": _address(transfer.get("to")),
            "amount_raw": amount_raw,
            "source_steps": sorted(steps),
            "step_min": min(steps),
            "step_max": max(steps),
            "token_call_id": token_call_id,
            "evidence_call_id": evidence_call_id,
            "mutation_call_ids": deepest_ids,
            "is_native": is_native,
        })

    mapped.sort(key=lambda item: (item["step_min"], item["edge_id"]))
    return mapped, diagnostics, by_id, parents


def _deepest_venue_ancestor(
    call_id: int | None,
    venue: str,
    frames: dict[int, _Frame],
    parents: dict[int, int | None],
) -> _Frame | None:
    for ancestor_id in _ancestor_ids(call_id, parents):
        frame = frames.get(ancestor_id)
        if frame is not None and frame.to_address == venue:
            return frame
    return None


def _same_parent_domain(
    call_id: int | None,
    parent_id: int | None,
    parents: dict[int, int | None],
) -> bool:
    return _is_descendant(call_id, parent_id, parents)


def _callback_prefund_anchor(
    input_call_id: int | None,
    output_scope: _Frame,
    frames: dict[int, _Frame],
    parents: dict[int, int | None],
) -> _Frame | None:
    """Recognize a venue prefunded before an outer venue calls back its caller.

    Some callback swaps transfer the intermediate asset to the next venue
    before entering the callback.  The next venue is then called from inside
    that callback, so the input token mutation and output token mutation live
    in sibling call-tree branches rather than one parent domain::

        route -> outer venue
          outer venue -> token (prefund inner venue)
          outer venue -> route (callback)
            route -> inner venue
              inner venue -> token (output)

    The inverse outer-venue callback is the structural proof that ties those
    branches together.  Requiring it avoids pairing arbitrary sibling token
    transfers merely because one happened before a venue call.
    """
    callback = frames.get(output_scope.parent_call_id)
    common_id = _lca([input_call_id, output_scope.call_id], parents)
    envelope = frames.get(common_id) if common_id is not None else None
    if callback is None or envelope is None:
        return None
    if callback.call_id == envelope.call_id:
        return None
    if not _is_descendant(callback.call_id, envelope.call_id, parents):
        return None
    if (
        callback.from_address != envelope.to_address
        or callback.to_address != envelope.from_address
    ):
        return None
    return callback


def _is_inverse_callback(frame: _Frame, frames: dict[int, _Frame]) -> bool:
    """Return whether ``frame`` calls back into its direct caller.

    A route executor entered as ``route -> venue -> route`` is a settlement
    callback, not another venue merely because it receives one asset before
    forwarding another.  Treating that inverse frame as a venue creates a
    synthetic SwapLeg from the outer venue's output to the callback payment.
    """
    parent = frames.get(frame.parent_call_id) if frame.parent_call_id is not None else None
    return bool(
        parent is not None
        and frame.from_address == parent.to_address
        and frame.to_address == parent.from_address
    )


def _scope_candidate(
    *,
    kind: str,
    venue: str,
    input_edge: dict[str, Any],
    output_edge: dict[str, Any],
    anchor: _Frame,
    depth: int,
) -> dict[str, Any]:
    start_step = min(input_edge["step_min"], output_edge["step_min"], anchor.entry_step)
    end_step = max(input_edge["step_max"], output_edge["step_max"], anchor.exit_step)
    return {
        "kind": kind,
        "venue_address": venue,
        "anchor_call_ids": [anchor.call_id],
        "logical_step": min(input_edge["step_min"], output_edge["step_min"]),
        "start_step": start_step,
        "end_step": end_step,
        "depth": depth,
        "input_edges": [input_edge],
        "output_edges": [output_edge],
        "strength": 2 if kind == "common_scope" else 1,
    }


def _post_settlement_scope_candidate(
    *,
    venue: str,
    input_edge: dict[str, Any],
    output_edge: dict[str, Any],
    input_scope: _Frame,
    output_scope: _Frame,
    envelope: _Frame,
) -> dict[str, Any]:
    """Build an output-first leg settled by a later sibling venue call."""
    return {
        "kind": "callback_post_settled",
        "venue_address": venue,
        "anchor_call_ids": [output_scope.call_id, input_scope.call_id, envelope.call_id],
        # Economic route order follows settlement for an output-first leg.
        # Its output edge can then connect backwards through a shared TFG edge
        # while non-shared predecessor legs remain chronologically increasing.
        "logical_step": input_edge["step_min"],
        "start_step": min(
            output_edge["step_min"], output_scope.entry_step, envelope.entry_step
        ),
        "end_step": max(
            input_edge["step_max"], input_scope.exit_step, envelope.exit_step
        ),
        "depth": max(input_scope.depth, output_scope.depth),
        "input_edges": [input_edge],
        "output_edges": [output_edge],
        "strength": 2,
    }


def extract_swap_scopes(
    mapped: list[dict[str, Any]],
    frames: dict[int, _Frame],
    parents: dict[int, int | None],
) -> list[dict[str, Any]]:
    """Extract minimal one-in/one-out venue scopes using strict call/step evidence."""
    by_venue_in: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_venue_out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in mapped:
        if edge["to_address"]:
            by_venue_in[edge["to_address"]].append(edge)
        if edge["from_address"]:
            by_venue_out[edge["from_address"]].append(edge)

    candidates: list[dict[str, Any]] = []
    for venue in sorted(set(by_venue_in) & set(by_venue_out)):
        incoming = by_venue_in[venue]
        outgoing = by_venue_out[venue]
        for input_edge in incoming:
            for output_edge in outgoing:
                if input_edge["token_address"] == output_edge["token_address"]:
                    continue
                evidence_ids = [input_edge["evidence_call_id"], output_edge["evidence_call_id"]]
                common_id = _lca(evidence_ids, parents)
                common_frame = frames.get(common_id) if common_id is not None else None

                # Strong case: both token/native evidence frames meet in a
                # minimal frame whose execution address is the venue.
                if common_frame is not None and common_frame.to_address == venue:
                    scope_edges = [
                        edge for edge in mapped
                        if (
                            edge["from_address"] == venue or edge["to_address"] == venue
                        )
                        and _is_descendant(
                            edge["evidence_call_id"], common_frame.call_id, parents
                        )
                    ]
                    scope_input_tokens = {
                        edge["token_address"] for edge in scope_edges
                        if edge["to_address"] == venue
                    }
                    scope_output_tokens = {
                        edge["token_address"] for edge in scope_edges
                        if edge["from_address"] == venue
                    }
                    # A broad router/callback scope touching many assets must
                    # be split into deeper scopes; never pair its edges
                    # combinatorially.
                    if (
                        scope_input_tokens != {input_edge["token_address"]}
                        or scope_output_tokens != {output_edge["token_address"]}
                    ):
                        continue
                    candidates.append(_scope_candidate(
                        kind="common_scope",
                        venue=venue,
                        input_edge=input_edge,
                        output_edge=output_edge,
                        anchor=common_frame,
                        depth=common_frame.depth,
                    ))
                    continue

                input_scope = _deepest_venue_ancestor(
                    input_edge["evidence_call_id"], venue, frames, parents
                )
                output_scope = _deepest_venue_ancestor(
                    output_edge["evidence_call_id"], venue, frames, parents
                )

                # Output-first callback settlement:
                #
                #   route -> venue
                #     venue -> route (inverse callback envelope)
                #       route -> venue (venue sends token_out)
                #       ... execute route ...
                #       route -> venue (venue receives token_in)
                #
                # Both venue calls must be ordered siblings inside one proven
                # inverse callback.  This is deliberately stricter than a
                # time-window heuristic and does not rely on ABI/protocol names.
                if (
                    input_scope is not None
                    and output_scope is not None
                    and input_scope.call_id != output_scope.call_id
                    and input_scope.parent_call_id is not None
                    and input_scope.parent_call_id == output_scope.parent_call_id
                    and output_scope.exit_step < input_scope.entry_step
                    and output_edge["step_max"] < input_edge["step_min"]
                ):
                    envelope = frames.get(input_scope.parent_call_id)
                    if envelope is not None and _is_inverse_callback(envelope, frames):
                        candidates.append(_post_settlement_scope_candidate(
                            venue=venue,
                            input_edge=input_edge,
                            output_edge=output_edge,
                            input_scope=input_scope,
                            output_scope=output_scope,
                            envelope=envelope,
                        ))
                        continue

                # Strict pre-transfer case: input precedes the deepest venue
                # frame containing the output.  Prefer one parent domain, or
                # require a proven inverse callback for a prefunded sibling.
                if output_scope is None or input_edge["step_max"] >= output_scope.entry_step:
                    continue
                # A transfer received while an earlier invocation of this
                # venue is already active is an output/credit of that call,
                # not a prefund for a later invocation.  Without this guard a
                # subsequent profit withdrawal can become a pseudo SwapLeg.
                if input_scope is not None:
                    continue
                if _is_inverse_callback(output_scope, frames):
                    continue
                same_parent_domain = _same_parent_domain(
                    input_edge["evidence_call_id"], output_scope.parent_call_id, parents
                )
                callback_anchor = None
                if not same_parent_domain:
                    callback_anchor = _callback_prefund_anchor(
                        input_edge["evidence_call_id"], output_scope, frames, parents
                    )
                    if (
                        callback_anchor is None
                        or input_edge["step_max"] >= callback_anchor.entry_step
                    ):
                        continue
                earlier_inputs = [
                    edge for edge in incoming
                    if edge["step_max"] < output_scope.entry_step
                ]
                if not earlier_inputs:
                    continue
                nearest = max(earlier_inputs, key=lambda edge: (edge["step_max"], edge["edge_id"]))
                if nearest["edge_id"] != input_edge["edge_id"]:
                    continue
                candidates.append(_scope_candidate(
                    kind=(
                        "pre_transfer" if same_parent_domain
                        else "callback_prefunded"
                    ),
                    venue=venue,
                    input_edge=input_edge,
                    output_edge=output_edge,
                    anchor=output_scope,
                    depth=output_scope.depth,
                ))

    # The same two transfers can make a callback recipient look like an
    # inverse venue.  Prefer common-scope evidence, then deeper/narrower scope,
    # independent of address names or selectors.
    best_by_edges: dict[tuple[int, ...], dict[str, Any]] = {}
    for candidate in candidates:
        edge_key = tuple(sorted(
            edge["edge_id"]
            for edge in candidate["input_edges"] + candidate["output_edges"]
        ))
        rank = (
            candidate["strength"],
            candidate["depth"],
            -(candidate["end_step"] - candidate["start_step"]),
            -candidate["logical_step"],
        )
        existing = best_by_edges.get(edge_key)
        if existing is None or rank > existing["_rank"]:
            selected = dict(candidate)
            selected["_rank"] = rank
            best_by_edges[edge_key] = selected

    scopes = []
    for candidate in sorted(
        best_by_edges.values(),
        key=lambda item: (
            item["logical_step"], item["venue_address"],
            item["input_edges"][0]["edge_id"], item["output_edges"][0]["edge_id"],
        ),
    ):
        candidate.pop("_rank", None)
        candidate["scope_id"] = f"scope-{len(scopes) + 1}"
        scopes.append(candidate)
    return scopes


def extract_swap_legs(scopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    legs: list[dict[str, Any]] = []
    for scope in scopes:
        inputs = scope["input_edges"]
        outputs = scope["output_edges"]
        input_tokens = {edge["token_address"] for edge in inputs}
        output_tokens = {edge["token_address"] for edge in outputs}
        if len(input_tokens) != 1 or len(output_tokens) != 1 or input_tokens == output_tokens:
            continue
        input_edge_ids = sorted({edge["edge_id"] for edge in inputs})
        output_edge_ids = sorted({edge["edge_id"] for edge in outputs})
        input_steps = [step for edge in inputs for step in edge["source_steps"]]
        output_steps = [step for edge in outputs for step in edge["source_steps"]]
        leg = {
            "swap_leg_id": "",  # Assigned after deterministic sorting.
            "scope_id": scope["scope_id"],
            "scope_kind": scope["kind"],
            "venue_address": scope["venue_address"],
            "logical_step": scope["logical_step"],
            "scope_step_range": [scope["start_step"], scope["end_step"]],
            "anchor_call_ids": scope["anchor_call_ids"],
            "token_in_address": next(iter(input_tokens)),
            "amount_in_raw": str(sum(edge["amount_raw"] for edge in inputs)),
            "input_edge_amounts_raw": {
                str(edge["edge_id"]): str(edge["amount_raw"]) for edge in inputs
            },
            "payer": inputs[0]["from_address"],
            "input_edge_ids": input_edge_ids,
            "input_step_range": [min(input_steps), max(input_steps)],
            "input_token_call_ids": sorted({
                edge["token_call_id"] for edge in inputs if edge["token_call_id"] is not None
            }),
            "token_out_address": next(iter(output_tokens)),
            "amount_out_raw": str(sum(edge["amount_raw"] for edge in outputs)),
            "output_edge_amounts_raw": {
                str(edge["edge_id"]): str(edge["amount_raw"]) for edge in outputs
            },
            "recipient": outputs[0]["to_address"],
            "output_edge_ids": output_edge_ids,
            "output_step_range": [min(output_steps), max(output_steps)],
            "output_token_call_ids": sorted({
                edge["token_call_id"] for edge in outputs if edge["token_call_id"] is not None
            }),
            "ambiguous": False,
            "evidence_strength": scope["strength"],
        }
        legs.append(leg)

    legs.sort(key=lambda leg: (
        leg["logical_step"], leg["venue_address"],
        leg["input_edge_ids"], leg["output_edge_ids"],
    ))
    for index, leg in enumerate(legs, start=1):
        leg["swap_leg_id"] = f"leg-{index}"
    return legs


def _amounts_compatible(first: dict[str, Any], second: dict[str, Any]) -> tuple[bool, float]:
    available = int(first["amount_out_raw"])
    required = int(second["amount_in_raw"])
    denominator = max(available, required)
    if denominator == 0:
        return False, 1.0
    gap = abs(available - required) / denominator
    return gap <= MAX_AMOUNT_RELATIVE_GAP, gap


def _custody_compatible(first: dict[str, Any], second: dict[str, Any]) -> bool:
    if set(first["output_edge_ids"]) & set(second["input_edge_ids"]):
        return True
    recipient = first["recipient"]
    return recipient in {second["payer"], second["venue_address"]}


def _flow_events(
    paired: list[dict[str, Any]],
    pending_erc20: list[dict[str, Any]] | None,
) -> dict[int, dict[str, Any]]:
    """Build canonical directed asset events for route-boundary accounting."""
    events: dict[int, dict[str, Any]] = {}

    for transfer in paired:
        edge_id = _integer(transfer.get("order"))
        amount = _transfer_amount_raw(transfer)
        if edge_id is None or amount is None:
            continue
        steps = _transfer_steps(transfer)
        events[edge_id] = {
            "edge_id": edge_id,
            "token_address": _address(transfer.get("token_addr") or transfer.get("token")),
            "from_address": _address(transfer.get("from")),
            "to_address": _address(transfer.get("to")),
            "amount_raw": amount,
            "step_min": min(steps) if steps else edge_id,
            "step_max": max(steps) if steps else edge_id,
            "kind": "transfer",
        }

    for change in pending_erc20 or []:
        edge_id = _integer(change.get("order"))
        raw_value = _integer(change.get("value"))
        if edge_id is None or raw_value is None or raw_value == 0:
            continue
        token_address = _address(change.get("token_addr") or change.get("token"))
        user = _address(change.get("user"))
        steps = _transfer_steps(change)
        events[edge_id] = {
            "edge_id": edge_id,
            "token_address": token_address,
            "from_address": user if raw_value < 0 else token_address,
            "to_address": token_address if raw_value < 0 else user,
            "amount_raw": abs(raw_value),
            "step_min": min(steps) if steps else edge_id,
            "step_max": max(steps) if steps else edge_id,
            "kind": "burn" if raw_value < 0 else "mint",
        }

    return events


def _raw_amounts_compatible(available: int, required: int) -> tuple[bool, float]:
    denominator = max(available, required)
    if denominator == 0:
        return False, 1.0
    gap = abs(available - required) / denominator
    return gap <= MAX_AMOUNT_RELATIVE_GAP, gap


def _trace_route_origin(
    first_leg: dict[str, Any],
    flow_events: dict[int, dict[str, Any]],
) -> tuple[str, list[int]]:
    """Trace same-economic-asset custody backwards from a first SwapLeg.

    This collapses exact/near-exact helper forwarding and explicit ETH/WETH
    mirror events without guessing across a token conversion.  The returned
    connector orders are real TFG edges and remain visible in the full path.
    """
    current_account = _address(first_leg.get("payer"))
    current_token = _address(first_leg.get("token_in_address"))
    current_amount = int(first_leg.get("amount_in_raw", 0))
    input_orders = [int(value) for value in first_leg.get("input_edge_ids", [])]
    if not current_account or not input_orders or current_amount <= 0:
        return current_account, []

    cutoff = min(input_orders)
    connectors: list[int] = []
    visited: set[int] = set(input_orders)
    for _ in range(MAX_ROUTE_HOPS):
        candidates: list[tuple[int, int, float, dict[str, Any]]] = []
        for edge_id, event in flow_events.items():
            if edge_id in visited or edge_id >= cutoff:
                continue
            if event["to_address"] != current_account:
                continue
            if not _route_tokens_compatible(event["token_address"], current_token):
                continue
            # Only ETH/WETH supply boundaries represent an explicit asset
            # conversion here.  Arbitrary ERC-20 mint/burn events are not
            # custody forwarding and must never be absorbed into a route.
            if (
                event["kind"] != "transfer"
                and _economic_asset(event["token_address"]) != "native"
            ):
                continue
            compatible, gap = _raw_amounts_compatible(
                int(event["amount_raw"]), current_amount
            )
            if not compatible:
                continue
            candidates.append((event["step_max"], edge_id, -gap, event))
        if not candidates:
            break
        _, edge_id, _, predecessor = max(candidates)
        connectors.append(edge_id)
        visited.add(edge_id)
        cutoff = edge_id
        current_account = predecessor["from_address"]
        current_token = predecessor["token_address"]
        current_amount = int(predecessor["amount_raw"])
        if not current_account:
            break

    connectors.reverse()
    return current_account, connectors


def build_ordered_leg_graph(swap_legs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    graph: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for first in swap_legs:
        for second in swap_legs:
            shared_transfer = bool(
                set(first["output_edge_ids"]) & set(second["input_edge_ids"])
            )
            if (
                first is second
                # A shared TFG edge is direct producer/consumer evidence and
                # therefore defines economic route order by itself.  Nested
                # flash-swap callbacks settle their legs while unwinding the
                # call stack, so their logical steps legitimately run in
                # reverse order.  Only inferred (non-shared) continuity must
                # remain chronologically increasing.
                or (
                    not shared_transfer
                    and first["logical_step"] > second["logical_step"]
                )
                or (
                    first["logical_step"] == second["logical_step"]
                    and not shared_transfer
                )
            ):
                continue
            if not _route_tokens_compatible(
                first["token_out_address"], second["token_in_address"]
            ):
                continue
            if not _custody_compatible(first, second):
                continue
            compatible, gap = _amounts_compatible(first, second)
            if not compatible:
                continue
            graph[first["swap_leg_id"]].append({
                "next_leg_id": second["swap_leg_id"],
                "amount_relative_gap": gap,
            })
    for successors in graph.values():
        successors.sort(key=lambda item: item["next_leg_id"])
    return dict(graph)


def find_primitive_cycles(
    swap_legs: list[dict[str, Any]],
    graph: dict[str, list[dict[str, Any]]],
    flow_events: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {leg["swap_leg_id"]: leg for leg in swap_legs}
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()

    def cycle_asset(edge_orders: set[int], route_account: str) -> tuple[str, str, int]:
        """Find the strongest canonical-asset delta at the route boundary."""
        grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge_id in edge_orders:
            edge = flow_events.get(edge_id)
            if edge is not None:
                grouped[_economic_asset(edge["token_address"])].append(edge)

        matches: list[tuple[float, int, int, str, int]] = []
        for asset_edges in grouped.values():
            outgoing = [edge for edge in asset_edges if edge["from_address"] == route_account]
            incoming = [edge for edge in asset_edges if edge["to_address"] == route_account]
            if not outgoing or not incoming:
                continue
            amount_out = sum(int(edge["amount_raw"]) for edge in outgoing)
            amount_in = sum(int(edge["amount_raw"]) for edge in incoming)
            denominator = max(amount_in, amount_out)
            relative_change = (
                abs(amount_in - amount_out) / denominator if denominator else 0.0
            )
            latest_edge = max(asset_edges, key=lambda edge: (edge["step_max"], edge["edge_id"]))
            matches.append((
                relative_change,
                len(asset_edges),
                latest_edge["step_max"],
                latest_edge["token_address"],
                amount_in - amount_out,
            ))

        if matches:
            _, _, _, token, delta = max(matches)
            return token, route_account, delta
        # Defensive fallback for incomplete transfer evidence.
        selected = [flow_events[edge_id] for edge_id in edge_orders if edge_id in flow_events]
        if selected:
            latest = max(selected, key=lambda edge: (edge["step_max"], edge["edge_id"]))
            return latest["token_address"], route_account, 0
        return "", route_account, 0

    def visit(
        path: list[str],
        gaps: list[float],
        route_account: str,
        connector_orders: list[int],
    ) -> None:
        first = by_id[path[0]]
        last = by_id[path[-1]]
        settlement_closure = bool(
            set(last["output_edge_ids"]) & set(first["input_edge_ids"])
        )
        if (
            len(path) >= 2
            and _route_tokens_compatible(
                last["token_out_address"], first["token_in_address"]
            )
            and (last["recipient"] == route_account or settlement_closure)
        ):
            reported_path = path
            reported_route_account = route_account
            if settlement_closure:
                # Callback swaps often emit the first asset before collecting
                # the settlement asset. Rotate the strict-step path at the
                # latest transfer so its displayed token route follows the
                # economic WETH -> ... -> WETH direction.
                latest_edge = max(
                    edge_id
                    for leg_id in path
                    for edge_id in (
                        by_id[leg_id]["input_edge_ids"]
                        + by_id[leg_id]["output_edge_ids"]
                    )
                )
                rotation = next(
                    (
                        index for index, leg_id in enumerate(path)
                        if latest_edge in by_id[leg_id]["input_edge_ids"]
                    ),
                    0,
                )
                reported_path = path[rotation:] + path[:rotation]
                reported_route_account = by_id[reported_path[0]]["payer"]
            swap_edge_orders = {
                edge_id
                for leg_id in path
                for edge_id in by_id[leg_id]["input_edge_ids"] + by_id[leg_id]["output_edge_ids"]
            }
            edge_order_set = swap_edge_orders | set(connector_orders)
            edge_orders = sorted(edge_order_set)
            arbitrage_token_address, reported_route_account, asset_delta = cycle_asset(
                edge_order_set, reported_route_account
            )

            # Present a closed route from the beneficiary's arbitrage asset
            # when that boundary is explicit.  This turns an output-first
            # settlement rotation such as USDC -> USDT -> WETH -> USDC into
            # the economically clearer WETH -> USDC -> USDT -> WETH.
            asset_rotation = next(
                (
                    index for index, leg_id in enumerate(reported_path)
                    if _route_tokens_compatible(
                        by_id[leg_id]["token_in_address"], arbitrage_token_address
                    )
                    and by_id[leg_id]["payer"] == reported_route_account
                ),
                None,
            )
            if asset_rotation is not None:
                reported_path = (
                    reported_path[asset_rotation:] + reported_path[:asset_rotation]
                )

            key = tuple(reported_path)
            if key in seen:
                return
            seen.add(key)
            reported_first = by_id[reported_path[0]]
            reported_last = by_id[reported_path[-1]]
            path_final_token_address = reported_last["token_out_address"]
            closure_kind = (
                "exact"
                if path_final_token_address == reported_first["token_in_address"]
                else "wrapped_native"
            )
            candidates.append({
                "ordered_swap_leg_ids": list(reported_path),
                "token_address_path": [
                    by_id[leg_id]["token_in_address"] for leg_id in reported_path
                ] + [path_final_token_address],
                # Derived from distinct incoming/outgoing TFG edges at one
                # account, independent of callback-reordered SwapLeg display.
                "arbitrage_token_address": arbitrage_token_address,
                "arbitrage_amount_delta_raw": str(asset_delta),
                "arbitrage_direction": (
                    "increase" if asset_delta > 0 else "decrease" if asset_delta < 0 else "unchanged"
                ),
                "transfer_edge_orders": edge_orders,
                "swap_transfer_edge_orders": sorted(swap_edge_orders),
                "connector_edge_orders": list(connector_orders),
                "route_account": reported_route_account,
                "start_step": min(
                    [by_id[leg_id]["logical_step"] for leg_id in path]
                    + [flow_events[edge_id]["step_min"] for edge_id in connector_orders]
                ),
                "end_step": max(
                    step
                    for leg_id in path
                    for step in (
                        by_id[leg_id]["input_step_range"]
                        + by_id[leg_id]["output_step_range"]
                    )
                ),
                "closure_kind": closure_kind,
                "max_amount_relative_gap": max(gaps, default=0.0),
                "score": sum(by_id[leg_id]["evidence_strength"] for leg_id in path) * 100 - sum(gaps),
            })
            return
        if len(path) >= MAX_ROUTE_HOPS:
            return
        for successor in graph.get(path[-1], []):
            next_id = successor["next_leg_id"]
            if next_id in path:
                continue
            visit(
                path + [next_id],
                gaps + [successor["amount_relative_gap"]],
                route_account,
                connector_orders,
            )

    for leg in swap_legs:
        if leg["payer"]:
            route_account, connector_orders = _trace_route_origin(leg, flow_events)
            visit([leg["swap_leg_id"]], [], route_account, connector_orders)

    candidates.sort(key=lambda cycle: (
        cycle["start_step"], cycle["end_step"], cycle["ordered_swap_leg_ids"]
    ))
    for index, candidate in enumerate(candidates, start=1):
        candidate["cycle_id"] = f"candidate-{index}"
    return candidates


def select_disjoint_cycles(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Maximum-weight swap-leg-disjoint selection with deterministic ties."""
    if not candidates:
        return []
    ordered = sorted(candidates, key=lambda item: (-item["score"], item["cycle_id"]))
    best_score = float("-inf")
    best: list[dict[str, Any]] = []

    def search(index: int, used: set[str], selected: list[dict[str, Any]], score: float) -> None:
        nonlocal best_score, best
        if index == len(ordered):
            selected_key = tuple(item["cycle_id"] for item in selected)
            best_key = tuple(item["cycle_id"] for item in best)
            if score > best_score or (score == best_score and selected_key < best_key):
                best_score = score
                best = list(selected)
            return
        candidate = ordered[index]
        legs = set(candidate["ordered_swap_leg_ids"])
        if not legs & used:
            search(index + 1, used | legs, selected + [candidate], score + candidate["score"])
        search(index + 1, used, selected, score)

    # Real transactions produce few primitive candidates.  Bound pathological
    # ambiguity while keeping exact selection for normal inputs.
    if len(ordered) <= 24:
        search(0, set(), [], 0.0)
    else:
        used: set[str] = set()
        for candidate in ordered:
            legs = set(candidate["ordered_swap_leg_ids"])
            if legs & used:
                continue
            best.append(candidate)
            used.update(legs)

    best.sort(key=lambda item: (item["start_step"], item["end_step"], item["cycle_id"]))
    selected: list[dict[str, Any]] = []
    for index, candidate in enumerate(best, start=1):
        item = dict(candidate)
        item["cycle_id"] = f"cycle-{index}"
        selected.append(item)
    return selected


def detect_arbitrage(
    paired: list[dict[str, Any]],
    pending_erc20: list[dict[str, Any]] | None = None,
    trace_tree: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return strict-step SwapLeg evidence and independently selected cycles."""
    mapped, diagnostics, frames, parents = map_transfers_to_token_calls(paired, trace_tree)
    scopes = extract_swap_scopes(mapped, frames, parents)
    swap_legs = extract_swap_legs(scopes)
    graph = build_ordered_leg_graph(swap_legs)
    flow_events = _flow_events(paired, pending_erc20)
    candidates = find_primitive_cycles(swap_legs, graph, flow_events)
    selected = select_disjoint_cycles(candidates)
    cycles = [cycle["transfer_edge_orders"] for cycle in selected]
    arb_edge_orders = {
        edge_id for cycle in selected for edge_id in cycle["transfer_edge_orders"]
    }
    return {
        "schema_version": DETECTOR_SCHEMA_VERSION,
        "detection_basis": "tfg_calltree_strict_step",
        "swap_legs": swap_legs,
        "mapping_diagnostics": diagnostics,
        "leg_graph": graph,
        "all_candidates": candidates,
        "selected_cycles": selected,
        # Backward-compatible fields used by the current AFG renderer/UI.
        "cycles": cycles,
        "arb_edge_orders": arb_edge_orders,
    }


def build_arbitrage_artifact(result: dict[str, Any]) -> dict[str, Any]:
    """Build the JSON-safe detector contract while retaining legacy fields."""
    cycles = result.get("cycles", [])
    return {
        "schema_version": result.get("schema_version", DETECTOR_SCHEMA_VERSION),
        "detection_basis": result.get("detection_basis", "tfg_calltree_strict_step"),
        "is_arbitrage": bool(cycles),
        "cycles": cycles,
        "selected_cycles": result.get("selected_cycles", []),
        "all_candidates": result.get("all_candidates", []),
        "arb_edge_orders": sorted(result.get("arb_edge_orders", set())),
    }


def build_swap_legs_artifact(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "detection_basis": result.get("detection_basis", "tfg_calltree_strict_step"),
        "swap_legs": result.get("swap_legs", []),
        "leg_graph": result.get("leg_graph", {}),
        "mapping_diagnostics": result.get("mapping_diagnostics", []),
    }
