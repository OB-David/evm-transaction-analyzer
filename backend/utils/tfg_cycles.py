"""Address-cycle-first structural detection over the Token Flow Graph.

The detector uses directed, token-labelled transfer edges. Mint and burn stay
visible in the TFG as supply-boundary evidence, but they do not participate in
cycle detection. The detector enumerates elementary address cycles, rotates
them into token transformations at each shared address, and finds minimal
token closures by:

1. number of distinct TFG edges;
2. number of elementary address cycles.

Candidate token closures are retained for auditability.  The displayed token
cycles are an exact edge-disjoint selection that maximizes covered transfer
edges, then minimizes gaps between execution-order edge IDs inside each path.

Each reported token closure must have a non-zero raw delta in one closing
asset. Positive and negative deltas are both retained; intent and cross-asset
economic valuation remain outside this structural model.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import heapq
from itertools import count
from typing import Any


TFG_CYCLE_SCHEMA_VERSION = 5

NATIVE_ETH_TOKEN_IDENTITY = "eth"
ETHEREUM_MAINNET_WETH_ADDRESS = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
TOKEN_EQUIVALENCES = {
    NATIVE_ETH_TOKEN_IDENTITY: frozenset({
        NATIVE_ETH_TOKEN_IDENTITY,
        ETHEREUM_MAINNET_WETH_ADDRESS,
    }),
}


def _normalized(value: Any) -> str:
    return str(value or "").strip().lower()


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _token_identity(token_address: Any) -> str:
    """Return the asset identity used only for token-cycle closure checks."""
    normalized = _normalized(token_address)
    for identity, equivalent_addresses in TOKEN_EQUIVALENCES.items():
        if normalized in equivalent_addresses:
            return identity
    return normalized


@dataclass(frozen=True)
class _Edge:
    edge_id: int
    source: str
    target: str
    token_address: str
    token_identity: str
    amount_raw: int


def _transfer_edges(paired: list[dict[str, Any]]) -> list[_Edge]:
    edges: list[_Edge] = []
    seen_ids: set[int] = set()
    for transfer in paired:
        edge_id = _integer(transfer.get("order"))
        source = _normalized(transfer.get("from"))
        target = _normalized(transfer.get("to"))
        token_address = _normalized(
            transfer.get("token_addr") or transfer.get("token")
        )
        amount_raw = _integer(transfer.get("amount_raw"))
        if (
            edge_id is None
            or edge_id in seen_ids
            or not source
            or not target
            or source == target
            or not token_address
            or amount_raw is None
            or amount_raw <= 0
        ):
            continue
        seen_ids.add(edge_id)
        edges.append(_Edge(
            edge_id=edge_id,
            source=source,
            target=target,
            token_address=token_address,
            token_identity=_token_identity(token_address),
            amount_raw=amount_raw,
        ))
    return sorted(edges, key=lambda edge: edge.edge_id)


def _strongly_connected_components(
    nodes: set[str],
    adjacency: dict[str, list[_Edge]],
) -> list[set[str]]:
    index = 0
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[set[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indexes[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for edge in adjacency.get(node, []):
            target = edge.target
            if target not in indexes:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[target])

        if lowlinks[node] != indexes[node]:
            return
        component: set[str] = set()
        while stack:
            member = stack.pop()
            on_stack.remove(member)
            component.add(member)
            if member == node:
                break
        components.append(component)

    for node in sorted(nodes):
        if node not in indexes:
            visit(node)
    return components


def enumerate_atomic_address_cycles(
    paired: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Enumerate directed elementary address cycles with stable TFG edge IDs."""
    edges = _transfer_edges(paired)
    adjacency: dict[str, list[_Edge]] = defaultdict(list)
    nodes: set[str] = set()
    for edge in edges:
        adjacency[edge.source].append(edge)
        nodes.update((edge.source, edge.target))
    for outgoing in adjacency.values():
        outgoing.sort(key=lambda edge: (edge.target, edge.edge_id))

    raw_cycles: list[tuple[list[str], list[_Edge]]] = []
    for component in _strongly_connected_components(nodes, adjacency):
        if len(component) < 2:
            continue
        for start in sorted(component):
            visited = {start}

            def walk(
                node: str,
                path_nodes: list[str],
                path_edges: list[_Edge],
            ) -> None:
                for edge in adjacency.get(node, []):
                    target = edge.target
                    if target not in component:
                        continue
                    if target == start:
                        if path_edges:
                            raw_cycles.append((
                                [*path_nodes, start],
                                [*path_edges, edge],
                            ))
                        continue
                    # The lexicographically smallest node is the canonical
                    # start, preventing rotational duplicates.
                    if target < start or target in visited:
                        continue
                    visited.add(target)
                    walk(target, [*path_nodes, target], [*path_edges, edge])
                    visited.remove(target)

            walk(start, [start], [])

    atomic_cycles: list[dict[str, Any]] = []
    seen: set[tuple[int, ...]] = set()
    for path_nodes, path_edges in sorted(
        raw_cycles,
        key=lambda item: (
            len(item[1]),
            tuple(edge.edge_id for edge in item[1]),
        ),
    ):
        signature = tuple(edge.edge_id for edge in path_edges)
        if signature in seen:
            continue
        seen.add(signature)
        atomic_cycles.append({
            "cycle_id": f"address-cycle-{len(atomic_cycles) + 1}",
            "nodes": path_nodes,
            "edge_orders": list(signature),
            "token_address_path": [edge.token_address for edge in path_edges],
            "token_identity_path": [edge.token_identity for edge in path_edges],
            "amount_raw_path": [str(edge.amount_raw) for edge in path_edges],
            "edge_count": len(path_edges),
        })
    return atomic_cycles


def _anchor_options(
    atomic_cycles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for cycle in atomic_cycles:
        nodes = cycle["nodes"][:-1]
        edge_orders = cycle["edge_orders"]
        tokens = cycle["token_address_path"]
        token_identities = cycle["token_identity_path"]
        amounts = cycle["amount_raw_path"]
        for index, anchor in enumerate(nodes):
            rotation = list(range(index, len(nodes))) + list(range(0, index))
            options.append({
                "option_id": f"{cycle['cycle_id']}@{anchor}",
                "atomic_cycle_id": cycle["cycle_id"],
                "anchor_address": anchor,
                "token_in_address": tokens[index],
                "token_in_identity": token_identities[index],
                "amount_in_raw": amounts[index],
                "token_out_address": tokens[index - 1],
                "token_out_identity": token_identities[index - 1],
                "amount_out_raw": amounts[index - 1],
                "rotated_nodes": [nodes[position] for position in rotation] + [anchor],
                "rotated_edge_orders": [edge_orders[position] for position in rotation],
                "edge_orders": edge_orders,
                "edge_count": len(edge_orders),
            })
    return options


def find_minimal_structural_paths(
    atomic_cycles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Find minimal token closures composed of edge-disjoint cycles at one address."""
    options = _anchor_options(atomic_cycles)
    by_anchor_token: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    start_tokens: dict[str, set[str]] = defaultdict(set)
    for option in options:
        key = (option["anchor_address"], option["token_in_identity"])
        by_anchor_token[key].append(option)
        start_tokens[option["anchor_address"]].add(option["token_in_identity"])
    for candidates in by_anchor_token.values():
        candidates.sort(key=lambda option: (
            option["edge_count"], option["atomic_cycle_id"], option["option_id"]
        ))

    closures: list[dict[str, Any]] = []
    serial = count()
    for anchor in sorted(start_tokens):
        for start_token in sorted(start_tokens[anchor]):
            heap: list[tuple[Any, ...]] = [(
                0, 0, next(serial), start_token, tuple(), frozenset(),
                frozenset(), (start_token,),
            )]
            best_cost: tuple[int, int] | None = None
            while heap:
                (
                    edge_count,
                    cycle_count,
                    _,
                    current_token,
                    option_path,
                    used_cycles,
                    used_edges,
                    token_path,
                ) = heapq.heappop(heap)
                cost = (edge_count, cycle_count)
                if best_cost is not None and cost >= best_cost:
                    continue

                for option in by_anchor_token.get((anchor, current_token), []):
                    cycle_id = option["atomic_cycle_id"]
                    option_edges = frozenset(option["edge_orders"])
                    if cycle_id in used_cycles or option_edges & used_edges:
                        continue
                    next_token = option["token_out_identity"]
                    next_edge_count = len(used_edges | option_edges)
                    next_cycle_count = cycle_count + 1
                    next_cost = (next_edge_count, next_cycle_count)
                    if best_cost is not None and next_cost > best_cost:
                        continue
                    next_option_path = (*option_path, option)
                    next_token_path = (*token_path, next_token)
                    if next_token == start_token:
                        amount_delta_raw = (
                            int(next_option_path[-1]["amount_out_raw"])
                            - int(next_option_path[0]["amount_in_raw"])
                        )
                        if amount_delta_raw == 0:
                            continue
                        if best_cost is None:
                            best_cost = next_cost
                        if next_cost == best_cost:
                            closures.append({
                                "anchor_address": anchor,
                                "token_address_path": [
                                    next_option_path[0]["token_in_address"],
                                    *[
                                        item["token_out_address"]
                                        for item in next_option_path
                                    ],
                                ],
                                "token_identity_path": list(next_token_path),
                                "atomic_cycle_ids": [
                                    item["atomic_cycle_id"] for item in next_option_path
                                ],
                                "address_cycle_paths": [
                                    item["rotated_nodes"] for item in next_option_path
                                ],
                                "cycle_edge_orders": [
                                    item["rotated_edge_orders"] for item in next_option_path
                                ],
                                "transfer_edge_orders": [
                                    edge_id
                                    for item in next_option_path
                                    for edge_id in item["rotated_edge_orders"]
                                ],
                                "arbitrage_token_address": (
                                    next_option_path[0]["token_in_address"]
                                ),
                                "arbitrage_token_identity": start_token,
                                "arbitrage_amount_delta_raw": str(amount_delta_raw),
                                "_arbitrage_token_order": (
                                    next_option_path[0]["rotated_edge_orders"][0]
                                ),
                                "edge_count": next_edge_count,
                                "atomic_cycle_count": next_cycle_count,
                            })
                        continue
                    if next_token in token_path:
                        continue
                    heapq.heappush(heap, (
                        next_edge_count,
                        next_cycle_count,
                        next(serial),
                        next_token,
                        next_option_path,
                        used_cycles | {cycle_id},
                        used_edges | option_edges,
                        next_token_path,
                    ))

    # Starting the same token closure at a different point can yield the same
    # structural edge set. Keep one deterministic representative.
    unique: dict[tuple[str, frozenset[int]], dict[str, Any]] = {}
    for closure in closures:
        key = (
            closure["anchor_address"],
            frozenset(closure["transfer_edge_orders"]),
        )
        rank = (
            closure["edge_count"],
            closure["atomic_cycle_count"],
            closure["_arbitrage_token_order"],
            closure["token_identity_path"],
            closure["token_address_path"],
            closure["atomic_cycle_ids"],
        )
        existing = unique.get(key)
        if existing is None or rank < (
            existing["edge_count"],
            existing["atomic_cycle_count"],
            existing["_arbitrage_token_order"],
            existing["token_identity_path"],
            existing["token_address_path"],
            existing["atomic_cycle_ids"],
        ):
            unique[key] = closure

    paths = sorted(unique.values(), key=lambda path: (
        path["edge_count"],
        path["atomic_cycle_count"],
        path["anchor_address"],
        path["_arbitrage_token_order"],
        path["token_identity_path"],
        path["token_address_path"],
        path["transfer_edge_orders"],
    ))
    for index, path in enumerate(paths, start=1):
        path["path_id"] = f"structural-path-{index}"
        path.pop("_arbitrage_token_order", None)
    return paths


def _execution_order_gap(path: dict[str, Any]) -> int:
    """Count missing transfer orders inside one candidate path's execution span."""
    orders = sorted(set(path["transfer_edge_orders"]))
    return sum(
        max(0, current - previous - 1)
        for previous, current in zip(orders, orders[1:])
    )


def select_edge_disjoint_paths(
    paths: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Select the best exact set packing of candidate token-cycle paths.

    The lexicographic objective is:

    1. no shared transfer edges (hard constraint);
    2. maximize the number of covered transfer edges;
    3. minimize execution-order gaps inside the selected paths;
    4. minimize the number of paths, then use stable path IDs as a tie-break.

    Conflict-connected components are solved independently so unrelated paths
    do not enlarge the exponential search space.
    """
    if not paths:
        return []

    edge_sets = [frozenset(path["transfer_edge_orders"]) for path in paths]
    gaps = [_execution_order_gap(path) for path in paths]
    conflicts: list[set[int]] = [set() for _ in paths]
    for left in range(len(paths)):
        for right in range(left + 1, len(paths)):
            if edge_sets[left] & edge_sets[right]:
                conflicts[left].add(right)
                conflicts[right].add(left)

    components: list[list[int]] = []
    unseen = set(range(len(paths)))
    while unseen:
        start = min(unseen)
        stack = [start]
        component: list[int] = []
        unseen.remove(start)
        while stack:
            index = stack.pop()
            component.append(index)
            neighbours = conflicts[index] & unseen
            unseen.difference_update(neighbours)
            stack.extend(sorted(neighbours, reverse=True))
        components.append(sorted(component))

    def better(left: tuple[int, ...], right: tuple[int, ...]) -> bool:
        left_score = (
            sum(len(edge_sets[index]) for index in left),
            -sum(gaps[index] for index in left),
            -len(left),
        )
        right_score = (
            sum(len(edge_sets[index]) for index in right),
            -sum(gaps[index] for index in right),
            -len(right),
        )
        if left_score != right_score:
            return left_score > right_score
        left_ids = tuple(str(paths[index].get("path_id", index)) for index in left)
        right_ids = tuple(str(paths[index].get("path_id", index)) for index in right)
        return left_ids < right_ids

    selected_indexes: list[int] = []
    for component in components:
        edge_universe = sorted({edge for index in component for edge in edge_sets[index]})
        edge_bits = {edge: bit for bit, edge in enumerate(edge_universe)}
        masks = {
            index: sum(1 << edge_bits[edge] for edge in edge_sets[index])
            for index in component
        }
        memo: dict[tuple[int, int], tuple[int, ...]] = {}

        def solve(position: int, used_mask: int) -> tuple[int, ...]:
            key = (position, used_mask)
            cached = memo.get(key)
            if cached is not None:
                return cached
            if position == len(component):
                return ()

            index = component[position]
            best = solve(position + 1, used_mask)
            if masks[index] & used_mask == 0:
                included = (index, *solve(position + 1, used_mask | masks[index]))
                if better(included, best):
                    best = included
            memo[key] = best
            return best

        selected_indexes.extend(solve(0, 0))

    return [paths[index] for index in sorted(selected_indexes)]


def detect_tfg_cycles(paired: list[dict[str, Any]]) -> dict[str, Any]:
    atomic_cycles = enumerate_atomic_address_cycles(paired)
    minimal_paths = find_minimal_structural_paths(atomic_cycles)
    selected_paths = select_edge_disjoint_paths(minimal_paths)
    return {
        "schema_version": TFG_CYCLE_SCHEMA_VERSION,
        "detection_basis": "tfg_address_cycle_structure",
        "token_equivalences": [
            {
                "identity": identity,
                "token_addresses": sorted(addresses),
            }
            for identity, addresses in sorted(TOKEN_EQUIVALENCES.items())
        ],
        "objective": ["transfer_edge_count", "atomic_cycle_count"],
        "selection_objective": [
            "edge_disjoint",
            "max_transfer_edge_count",
            "min_execution_order_gap",
            "min_path_count",
        ],
        "has_address_cycles": bool(atomic_cycles),
        "has_structural_paths": bool(minimal_paths),
        "atomic_cycles": atomic_cycles,
        "minimal_paths": minimal_paths,
        "selected_paths": selected_paths,
        "cycle_edge_orders": sorted({
            edge_id
            for cycle in atomic_cycles
            for edge_id in cycle["edge_orders"]
        }),
        "path_edge_orders": sorted({
            edge_id
            for path in selected_paths
            for edge_id in path["transfer_edge_orders"]
        }),
    }
