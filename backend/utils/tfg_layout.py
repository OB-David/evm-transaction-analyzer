"""Topology-petal TFG layout and SVG renderer.

The renderer uses persisted production artifacts. Arbitrage detection is never
used to place nodes.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from utils.analysis_paths import analysis_directory


NODE_RE = re.compile(r'^\s*"([^"]+)"\s+\[(.*)\]\s*$')
EDGE_RE = re.compile(r'^\s*"([^"]+)"\s*->\s*"([^"]+)"\s+\[(.*)\]\s*$')
ORDER_RE = re.compile(r"\((\d+)\)")
EDGE_LABEL_RE = re.compile(r"label=<(.*?)>\s+arrowsize=", re.IGNORECASE)
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
XLINK_NAMESPACE = "http://www.w3.org/1999/xlink"
NUMBER_RE = re.compile(r"-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
ET.register_namespace("", SVG_NAMESPACE)
ET.register_namespace("xlink", XLINK_NAMESPACE)

NODE_CLEARANCE_POINTS = 12.0


# Artifact loading and DOT parsing


def _artifact_paths(tx_hash: str) -> tuple[Path, Path]:
    result_dir = analysis_directory(tx_hash)
    return (
        result_dir / "asset_flow.dot",
        result_dir / "call_tree.json",
    )


def _parse_dot(dot_source: str) -> tuple[list[str], list[dict[str, Any]]]:
    nodes: list[str] = []
    edges: list[dict[str, Any]] = []
    for line in dot_source.splitlines():
        edge_match = EDGE_RE.match(line)
        if edge_match:
            order_match = ORDER_RE.search(edge_match.group(3))
            edges.append({
                "source": edge_match.group(1),
                "target": edge_match.group(2),
                "order": int(order_match.group(1)) if order_match else None,
                "line": line,
            })
            continue
        node_match = NODE_RE.match(line)
        if node_match and node_match.group(1) not in nodes:
            nodes.append(node_match.group(1))
    return nodes, edges


def filter_dot_through_order(dot_source: str, max_order: int) -> str:
    """Keep the complete TFG execution prefix through ``max_order``."""
    if max_order < 0:
        raise ValueError("max_order must be non-negative")

    retained_nodes: set[str] = set()
    retained_edge_lines: set[str] = set()
    for line in dot_source.splitlines():
        edge_match = EDGE_RE.match(line)
        if not edge_match:
            continue
        order_match = ORDER_RE.search(edge_match.group(3))
        if order_match and int(order_match.group(1)) <= max_order:
            retained_nodes.update((edge_match.group(1), edge_match.group(2)))
            retained_edge_lines.add(line)

    if not retained_edge_lines:
        raise ValueError(f"No TFG transfers found through order {max_order}")

    output: list[str] = []
    for line in dot_source.splitlines():
        edge_match = EDGE_RE.match(line)
        if edge_match:
            if line in retained_edge_lines:
                output.append(line)
            continue
        node_match = NODE_RE.match(line)
        if node_match and node_match.group(1) not in retained_nodes:
            continue
        output.append(line)
    return "\n".join(output) + "\n"


def topology_center(dot_source: str, call_tree_path: Path | None = None) -> str:
    """Use the transaction root address, falling back to TFG centrality."""
    nodes, edges = _parse_dot(dot_source)
    if not nodes:
        return ""
    if call_tree_path is not None and call_tree_path.is_file():
        call_tree = json.loads(call_tree_path.read_text(encoding="utf-8"))
        root_address = str(call_tree.get("root", {}).get("address", "")).lower()
        if root_address in nodes:
            return root_address
    degree: Counter[str] = Counter()
    for edge in edges:
        degree[edge["source"]] += 1
        degree[edge["target"]] += 1
    return max(nodes, key=lambda node: (degree[node], node))


# Topology detection


def _cycle_from_edge(
    start_index: int,
    edges: list[dict[str, Any]],
    center: str,
    unavailable: set[int],
) -> list[int] | None:
    """Find the shortest simple directed return path for one center edge."""
    start = edges[start_index]
    if start["source"] != center:
        return None
    if start["target"] == center:
        return [start_index]

    outgoing: defaultdict[str, list[int]] = defaultdict(list)
    for index, edge in enumerate(edges):
        if index not in unavailable and index != start_index:
            outgoing[edge["source"]].append(index)
    for candidates in outgoing.values():
        candidates.sort(key=lambda index: edges[index]["order"] or 10**12)

    best: list[int] | None = None

    def path_score(indexes: list[int]) -> tuple[Any, ...]:
        orders = [edges[index]["order"] or 10**12 for index in indexes]
        return (
            len(indexes),
            max(orders) - min(orders),
            sum(abs(second - first) for first, second in zip(orders, orders[1:])),
            orders,
        )

    def visit(node: str, path: list[int], visited_nodes: set[str]) -> None:
        nonlocal best
        if best is not None and len(path) >= len(best):
            return
        for edge_index in outgoing.get(node, []):
            edge = edges[edge_index]
            target = edge["target"]
            candidate = [*path, edge_index]
            if target == center:
                if best is None or path_score(candidate) < path_score(best):
                    best = candidate
                continue
            if target in visited_nodes:
                continue
            visit(target, candidate, visited_nodes | {target})

    visit(start["target"], [start_index], {center, start["target"]})
    return best


def _undirected_adjacency(edges: list[dict[str, Any]]) -> defaultdict[str, set[str]]:
    """Build the address adjacency used for root distance and parent lookup."""
    adjacency_all: defaultdict[str, set[str]] = defaultdict(set)
    for edge in edges:
        adjacency_all[edge["source"]].add(edge["target"])
        adjacency_all[edge["target"]].add(edge["source"])
    return adjacency_all


def _breadth_first_tree(
    center: str,
    adjacency: defaultdict[str, set[str]],
) -> tuple[dict[str, int], dict[str, str | None]]:
    """Return deterministic root distances and parents for every reachable node."""
    distances = {center: 0}
    parents: dict[str, str | None] = {center: None}
    frontier = [center]
    while frontier:
        current = frontier.pop(0)
        for neighbor in sorted(adjacency[current]):
            if neighbor not in distances:
                distances[neighbor] = distances[current] + 1
                parents[neighbor] = current
                frontier.append(neighbor)
    return distances, parents


def _cycle_structures(
    nodes: list[str],
    edges: list[dict[str, Any]],
    distances: dict[str, int],
    parents: dict[str, str | None],
) -> tuple[list[dict[str, Any]], set[int]]:
    """Find local directed cycles from the root outward."""
    used: set[int] = set()
    structures: list[dict[str, Any]] = []

    # Root first, then every node reached along a chain. This is the recursive
    # step that discovers a scheduler-local ring hanging below contract_to.
    for local_center in sorted(nodes, key=lambda node: (distances.get(node, 10**12), node)):
        outgoing = sorted(
            (index for index, edge in enumerate(edges) if edge["source"] == local_center),
            key=lambda index: edges[index]["order"] or 10**12,
        )
        for edge_index in outgoing:
            if edge_index in used:
                continue
            cycle_indexes = _cycle_from_edge(edge_index, edges, local_center, used)
            if not cycle_indexes:
                continue
            used.update(cycle_indexes)
            cycle_edges = [edges[index] for index in cycle_indexes]
            structures.append({
                "kind": "cycle",
                "center": local_center,
                "parent": parents.get(local_center),
                "depth": distances.get(local_center, 0),
                "orders": [edge["order"] for edge in cycle_edges],
                "nodes": [local_center, *[edge["target"] for edge in cycle_edges]],
                "first_order": min(edge["order"] for edge in cycle_edges if edge["order"] is not None),
            })
    return structures, used


def _residual_components(
    edges: list[dict[str, Any]],
    remaining: list[int],
    center: str,
) -> list[set[str]]:
    """Group non-cycle edges by weak connectivity after removing the root."""
    adjacency: defaultdict[str, set[str]] = defaultdict(set)
    for index in remaining:
        edge = edges[index]
        non_center = [node for node in (edge["source"], edge["target"]) if node != center]
        for node in non_center:
            adjacency[node]
        if len(non_center) == 2:
            adjacency[non_center[0]].add(non_center[1])
            adjacency[non_center[1]].add(non_center[0])

    components: list[set[str]] = []
    unseen = set(adjacency)
    while unseen:
        seed = min(unseen)
        component = {seed}
        frontier = [seed]
        unseen.remove(seed)
        while frontier:
            node = frontier.pop()
            for neighbor in adjacency[node] & unseen:
                unseen.remove(neighbor)
                component.add(neighbor)
                frontier.append(neighbor)
        components.append(component)
    return components


def _component_node_distances(
    center: str,
    component_edges: list[dict[str, Any]],
) -> dict[str, int]:
    """Measure a residual component outward from the topology center."""
    distances: dict[str, int] = {}
    frontier = [center]
    distance = 0
    while frontier:
        next_frontier: list[str] = []
        for current in frontier:
            for edge in component_edges:
                if current == edge["source"]:
                    neighbor = edge["target"]
                elif current == edge["target"]:
                    neighbor = edge["source"]
                else:
                    continue
                if neighbor != center and neighbor not in distances:
                    distances[neighbor] = distance + 1
                    next_frontier.append(neighbor)
        frontier = next_frontier
        distance += 1
    return distances


def _chain_structure(
    center: str,
    component: set[str],
    component_edges: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build one ordered chain-petal descriptor from a residual component."""
    distances = _component_node_distances(center, component_edges)
    component_nodes = sorted(
        component,
        key=lambda node: (
            distances.get(node, 10**12),
            min(
                edge["order"] or 10**12
                for edge in component_edges
                if node in (edge["source"], edge["target"])
            ),
        ),
    )
    return {
        "kind": "chain",
        "center": center,
        "depth": 0,
        "orders": [edge["order"] for edge in component_edges],
        "nodes": [center, *component_nodes],
        "first_order": min(edge["order"] for edge in component_edges if edge["order"] is not None),
    }
def _residual_structures(
    edges: list[dict[str, Any]],
    used: set[int],
    center: str,
) -> list[dict[str, Any]]:
    """Convert all non-cycle edges into chain or self-transfer petals."""
    remaining = [index for index in range(len(edges)) if index not in used]
    components = _residual_components(edges, remaining, center)
    structures: list[dict[str, Any]] = []
    assigned_remaining: set[int] = set()

    for component in components:
        indexes = [
            index for index in remaining
            if edges[index]["source"] in component or edges[index]["target"] in component
        ]
        assigned_remaining.update(indexes)
        component_edges = sorted(
            (edges[index] for index in indexes),
            key=lambda edge: edge["order"] or 10**12,
        )
        structures.append(_chain_structure(center, component, component_edges))

    # Center self-transfers are valid one-edge topological petals.
    for index in remaining:
        if index in assigned_remaining:
            continue
        edge = edges[index]
        structures.append({
            "kind": "cycle" if edge["source"] == edge["target"] == center else "chain",
            "center": center,
            "depth": 0,
            "orders": [edge["order"]],
            "nodes": [center, center] if edge["source"] == edge["target"] else [center],
            "first_order": edge["order"] or 10**12,
        })
    return structures


def detect_topology(dot_source: str, center: str) -> dict[str, Any]:
    """Recursively find local cycles, then group the residual edges as chains."""
    nodes, edges = _parse_dot(dot_source)
    distances, parents = _breadth_first_tree(center, _undirected_adjacency(edges))
    structures, used = _cycle_structures(nodes, edges, distances, parents)
    structures.extend(_residual_structures(edges, used, center))

    structures.sort(key=lambda structure: (structure["first_order"], structure["kind"]))
    cycle_count = sum(structure["kind"] == "cycle" for structure in structures)
    return {
        "center": center,
        "classification": (
            "figure_eight" if cycle_count > 1
            else "cycle" if cycle_count == 1
            else "chains"
        ),
        "cycle_petals": cycle_count,
        "chain_petals": len(structures) - cycle_count,
        "structures": structures,
    }


# Topology positioning


def _layout_structure_groups(
    structures: list[dict[str, Any]],
    center: str,
) -> tuple[list[dict[str, Any]], defaultdict[str, list[dict[str, Any]]]]:
    """Split root petals from cycle petals nested below another node."""
    root_structures = [
        structure for structure in structures
        if structure["kind"] == "chain" or structure.get("center", center) == center
    ]
    nested_by_center: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for structure in structures:
        if structure["kind"] == "cycle" and structure.get("center", center) != center:
            nested_by_center[structure["center"]].append(structure)
    return root_structures, nested_by_center


def _direction_slots(
    petals: list[dict[str, Any]],
    local_center: str,
) -> tuple[list[int], int]:
    """Give petals sharing a real node one stable radial direction."""
    parents = list(range(len(petals)))
    petal_nodes = [set(structure["nodes"]) - {local_center} for structure in petals]

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first: int, second: int) -> None:
        first_root, second_root = find(first), find(second)
        if first_root != second_root:
            parents[second_root] = first_root

    for first in range(len(petals)):
        for second in range(first):
            if petal_nodes[first] & petal_nodes[second]:
                union(second, first)

    roots = [find(index) for index in range(len(petals))]
    ordered_roots = list(dict.fromkeys(roots))
    root_slots = {root: slot for slot, root in enumerate(ordered_roots)}
    return [root_slots[root] for root in roots], max(len(ordered_roots), 1)


def _petal_count_growth(count: int, scale: float) -> float:
    """Stay compact below ten petals, then widen dense flowers faster."""
    count = max(count, 1)
    smooth_count = min(count, 7)
    medium_count = min(max(count - 7, 0), 2)
    dense_count = max(count - 9, 0)
    return scale * (
        math.sqrt(smooth_count)
        + 0.55 * medium_count
        + 2.25 * dense_count
    )


def _local_base_radius(local_cycles: list[dict[str, Any]]) -> float:
    """Size a nested flower from its petal count and longest path."""
    longest = max(len(set(item["nodes"])) - 1 for item in local_cycles)
    return (
        220.0
        + _petal_count_growth(len(local_cycles), 38.0)
        + 30.0 * max(0, longest - 1)
    )


def _flower_extent(
    local_center: str,
    nested_by_center: defaultdict[str, list[dict[str, Any]]],
    extent_cache: dict[str, float],
    visiting: set[str] | None = None,
) -> float:
    """Recursively reserve radial space for flowers nested below a node."""
    if local_center in extent_cache:
        return extent_cache[local_center]
    local_cycles = nested_by_center.get(local_center, [])
    if not local_cycles:
        return 0.0
    visiting = set() if visiting is None else visiting
    if local_center in visiting:
        return 0.0
    visiting = visiting | {local_center}
    child_extent = max(
        (
            _flower_extent(node, nested_by_center, extent_cache, visiting)
            for item in local_cycles
            for node in item["nodes"]
            if node != local_center
        ),
        default=0.0,
    )
    extent_cache[local_center] = _local_base_radius(local_cycles) + child_extent
    return extent_cache[local_center]


def _root_position_proposals(
    center: str,
    root_structures: list[dict[str, Any]],
    nested_by_center: defaultdict[str, list[dict[str, Any]]],
    extent_cache: dict[str, float],
) -> defaultdict[str, list[tuple[float, float]]]:
    """Propose coordinates for every root-level petal and chain."""
    proposed: defaultdict[str, list[tuple[float, float]]] = defaultdict(list)
    proposed[center].append((0.0, 0.0))
    root_slots, direction_count = _direction_slots(root_structures, center)
    petal_count = max(len(root_structures), 1)
    longest_root_petal = max(
        (len(set(structure["nodes"])) - 1 for structure in root_structures),
        default=1,
    )
    root_cycle_radius = (
        285.0
        + _petal_count_growth(petal_count, 42.0)
        + 25.0 * max(0, longest_root_petal - 1)
    )
    root_chain_radius = 215.0 + _petal_count_growth(petal_count, 24.0)

    for petal_index, structure in enumerate(root_structures):
        angle = math.pi / 2 - 2 * math.pi * root_slots[petal_index] / direction_count
        petal_nodes = [node for node in structure["nodes"] if node != center]
        # Preserve path order but do not duplicate a node within one petal.
        petal_nodes = list(dict.fromkeys(petal_nodes))
        if structure["kind"] == "cycle":
            # Use a wider angular chord for multi-node rings: the center
            # distance stays compact while the ring itself becomes fatter and
            # leaves more room between its ordinary nodes.
            spread = min(1.00, 0.34 + 0.20 * max(0, len(petal_nodes) - 1))
            for index, node in enumerate(petal_nodes):
                offset = 0.0 if len(petal_nodes) == 1 else (
                    -spread / 2 + spread * index / (len(petal_nodes) - 1)
                )
                radius = (
                    root_cycle_radius
                    + _flower_extent(node, nested_by_center, extent_cache)
                    + 65.0 * (index % 2)
                )
                proposed[node].append((
                    radius * math.cos(angle + offset),
                    radius * math.sin(angle + offset),
                ))
        else:
            for index, node in enumerate(petal_nodes):
                radius = (
                    root_chain_radius
                    + 155.0 * index
                    + _flower_extent(node, nested_by_center, extent_cache)
                )
                offset = (index % 2 * 2 - 1) * min(0.08 * index, 0.22)
                proposed[node].append((
                    radius * math.cos(angle + offset),
                    radius * math.sin(angle + offset),
                ))
    return proposed


def _average_position_proposals(
    proposed: defaultdict[str, list[tuple[float, float]]],
) -> dict[str, tuple[float, float]]:
    """Merge positions for real nodes shared by multiple root petals."""
    return {
        node: (
            sum(value[0] for value in values) / len(values),
            sum(value[1] for value in values) / len(values),
        )
        for node, values in proposed.items()
    }


def _place_nested_flowers(
    positions: dict[str, tuple[float, float]],
    nested_by_center: defaultdict[str, list[dict[str, Any]]],
    extent_cache: dict[str, float],
) -> None:
    """Place cycle petals nested beneath already-positioned nodes in place."""

    # Nested flowers may use almost the full circle. Only reserve a computed
    # cone toward the parent instead of discarding an entire 180-degree side.
    nested_groups = sorted(
        nested_by_center.items(),
        key=lambda item: (
            min(structure.get("depth", 0) for structure in item[1]),
            min(structure["first_order"] for structure in item[1]),
        ),
    )
    for local_center, local_cycles in nested_groups:
        if local_center not in positions:
            continue
        local_x, local_y = positions[local_center]
        local_slots, local_count = _direction_slots(local_cycles, local_center)
        local_radius = _local_base_radius(local_cycles)
        parent = next(
            (
                structure.get("parent")
                for structure in local_cycles
                if structure.get("parent") in positions
            ),
            None,
        )
        parent_x, parent_y = positions.get(parent, (0.0, 0.0))
        outward = math.atan2(local_y - parent_y, local_x - parent_x)
        parent_distance = max(math.hypot(local_x - parent_x, local_y - parent_y), 1.0)
        max_node_spread = max(
            min(0.72, 0.24 * max(0, len(set(item["nodes"])) - 2))
            for item in local_cycles
        )
        # Always exclude a 60-degree cone centered on the parent connection.
        # Add symmetric padding for the petal's node spread and Bezier bow, so
        # curved edges do not re-enter the nominally clear parent corridor.
        curve_margin = min(
            math.radians(15.0),
            math.atan2(local_radius * 0.18, parent_distance),
        )
        blocked_half_angle = min(
            math.radians(60.0),
            math.radians(30.0) + max_node_spread / 2 + curve_margin,
        )
        available_span = 2 * math.pi - 2 * blocked_half_angle
        for cycle_index, structure in enumerate(local_cycles):
            direction_index = local_slots[cycle_index]
            petal_angle = (
                outward
                if local_count == 1
                else outward - available_span / 2
                + available_span * direction_index / (local_count - 1)
            )
            cycle_nodes = list(dict.fromkeys(
                node for node in structure["nodes"] if node != local_center
            ))
            node_spread = min(1.05, 0.36 * max(0, len(cycle_nodes) - 1))
            for index, node in enumerate(cycle_nodes):
                # Repeated A-B-A-B-A cycles intentionally reuse node positions;
                # their parallel edges are separated later by curve lanes.
                if node in positions:
                    continue
                offset = 0.0 if len(cycle_nodes) == 1 else (
                    -node_spread / 2
                    + node_spread * index / (len(cycle_nodes) - 1)
                )
                radius = (
                    local_radius
                    + _flower_extent(node, nested_by_center, extent_cache)
                    + 45.0 * (index % 2)
                )
                positions[node] = (
                    local_x + radius * math.cos(petal_angle + offset),
                    local_y + radius * math.sin(petal_angle + offset),
                )


def _place_unpositioned_nodes(
    nodes: list[str],
    positions: dict[str, tuple[float, float]],
) -> None:
    """Put disconnected or otherwise unclassified nodes on an outer ring."""
    unplaced = [node for node in nodes if node not in positions]
    outer_radius = 1100.0 + 30.0 * len(unplaced)
    for index, node in enumerate(unplaced):
        angle = math.pi / 2 - (2 * math.pi * index / max(len(unplaced), 1))
        positions[node] = (
            outer_radius * math.cos(angle),
            outer_radius * math.sin(angle),
        )


def topology_positions(
    dot_source: str,
    topology: dict[str, Any],
) -> dict[str, tuple[float, float]]:
    """Place execution-adjacent topology structures as adjacent petals."""
    nodes, _ = _parse_dot(dot_source)
    center = topology["center"]
    root_structures, nested_by_center = _layout_structure_groups(
        topology["structures"], center
    )
    extent_cache: dict[str, float] = {}
    proposed = _root_position_proposals(
        center, root_structures, nested_by_center, extent_cache
    )
    positions = _average_position_proposals(proposed)
    _place_nested_flowers(positions, nested_by_center, extent_cache)
    _place_unpositioned_nodes(nodes, positions)
    return positions


def _separate_overlapping_nodes(
    positions: dict[str, tuple[float, float]],
    node_sizes: dict[str, tuple[float, float]],
    center: str,
    clearance: float = NODE_CLEARANCE_POINTS,
) -> dict[str, tuple[float, float]]:
    """Push only colliding nodes outward while preserving their petal angles."""
    adjusted = dict(positions)
    positioned_nodes = [node for node in adjusted if node in node_sizes]

    def overlaps(first: str, second: str) -> bool:
        first_x, first_y = adjusted[first]
        second_x, second_y = adjusted[second]
        first_width, first_height = node_sizes[first]
        second_width, second_height = node_sizes[second]
        return (
            abs(first_x - second_x) < (first_width + second_width) / 2 + clearance
            and abs(first_y - second_y) < (first_height + second_height) / 2 + clearance
        )

    def axis_exit_distance(delta: float, direction: float, boundary: float) -> float:
        if abs(delta) >= boundary:
            return 0.0
        if abs(direction) < 1e-9:
            return math.inf
        candidates = [
            distance for distance in (
                (boundary - delta) / direction,
                (-boundary - delta) / direction,
            )
            if distance >= 0.0
        ]
        return min(candidates, default=math.inf)

    def outward_distance(moving: str, fixed: str) -> float:
        moving_x, moving_y = adjusted[moving]
        radius = math.hypot(moving_x, moving_y)
        if radius < 1e-9:
            return math.inf
        fixed_x, fixed_y = adjusted[fixed]
        moving_width, moving_height = node_sizes[moving]
        fixed_width, fixed_height = node_sizes[fixed]
        return min(
            axis_exit_distance(
                moving_x - fixed_x,
                moving_x / radius,
                (moving_width + fixed_width) / 2 + clearance,
            ),
            axis_exit_distance(
                moving_y - fixed_y,
                moving_y / radius,
                (moving_height + fixed_height) / 2 + clearance,
            ),
        )

    for _ in range(max(len(positioned_nodes) ** 2, 1)):
        collisions = [
            (first, second)
            for first_index, first in enumerate(positioned_nodes)
            for second in positioned_nodes[first_index + 1:]
            if overlaps(first, second)
        ]
        if not collisions:
            break
        moved = False
        for first, second in collisions:
            if not overlaps(first, second):
                continue
            first_distance = math.inf if first == center else outward_distance(first, second)
            second_distance = math.inf if second == center else outward_distance(second, first)
            moving, distance = (
                (first, first_distance)
                if first_distance <= second_distance
                else (second, second_distance)
            )
            if not math.isfinite(distance):
                continue
            x, y = adjusted[moving]
            radius = math.hypot(x, y)
            padding = distance + 0.5
            adjusted[moving] = (
                x + x / radius * padding,
                y + y / radius * padding,
            )
            moved = True
        if not moved:
            break
    return adjusted


def _measure_node_sizes(dot_source: str) -> dict[str, tuple[float, float]]:
    """Ask Graphviz for final label-aware node sizes, returned in points."""
    completed = subprocess.run(
        ["neato", "-n2", "-Tplain"],
        input=dot_source.encode("utf-8"),
        capture_output=True,
        check=True,
        timeout=30,
    )
    node_sizes: dict[str, tuple[float, float]] = {}
    for line in completed.stdout.decode("utf-8", errors="replace").splitlines():
        fields = line.split()
        if len(fields) >= 6 and fields[0] == "node":
            node_sizes[fields[1].strip('"')] = (
                float(fields[4]) * 72.0,
                float(fields[5]) * 72.0,
            )
    return node_sizes


# DOT rewriting and presentation


def topology_dot(
    dot_source: str,
    topology: dict[str, Any],
    positions: dict[str, tuple[float, float]] | None = None,
) -> str:
    positions = topology_positions(dot_source, topology) if positions is None else positions
    output: list[str] = []
    for line in dot_source.splitlines():
        stripped = line.strip()
        if stripped.startswith("graph ["):
            output.append(
                '\tgraph [layout=neato overlap=true outputorder=edgesfirst '
                'pad=0.35 splines=curved normalize=false]'
            )
            continue
        if not EDGE_RE.match(line):
            node_match = NODE_RE.match(line)
            if node_match and node_match.group(1) in positions:
                x, y = positions[node_match.group(1)]
                output.append(
                    f'\t"{node_match.group(1)}" [{node_match.group(2)} '
                    f'pos="{x:.3f},{y:.3f}!"]'
                )
                continue
        output.append(line)
    return "\n".join(output) + "\n"
def presentation_dot(dot_source: str) -> str:
    """Apply the same curved edges and compact two-line labels to every engine."""
    output: list[str] = []
    for line in dot_source.splitlines():
        stripped = line.strip()
        if stripped.startswith("graph ["):
            if re.search(r"\bsplines=\S+", line):
                line = re.sub(r"\bsplines=\S+", "splines=curved", line)
            else:
                line = line.rsplit("]", 1)[0] + " splines=curved]"
            if "forcelabels=" not in line:
                line = line.rsplit("]", 1)[0] + " forcelabels=true]"
        edge_match = EDGE_RE.match(line)
        label_match = EDGE_LABEL_RE.search(edge_match.group(3)) if edge_match else None
        if edge_match and label_match:
            full_label = label_match.group(1)
            header, separator, amount = full_label.partition(": ")
            if separator and amount:
                compact_label = (
                    'label=<<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" '
                    'CELLPADDING="2" BGCOLOR="white">'
                    f'<TR><TD><B>{header}</B></TD></TR>'
                    f'<TR><TD><FONT POINT-SIZE="10">{amount}</FONT></TD></TR>'
                    '</TABLE>> arrowsize='
                )
                line = (
                    line[:edge_match.start(3)]
                    + EDGE_LABEL_RE.sub(compact_label, edge_match.group(3), count=1)
                    + line[edge_match.end(3):]
                )
        output.append(line)
    return "\n".join(output) + "\n"


# SVG geometry and post-processing


def _svg_tag(name: str) -> str:
    return f"{{{SVG_NAMESPACE}}}{name}"


def _numbers(value: str) -> list[float]:
    return [float(item) for item in NUMBER_RE.findall(value)]


def _cubic_point(
    start: tuple[float, float],
    control1: tuple[float, float],
    control2: tuple[float, float],
    end: tuple[float, float],
    t: float,
) -> tuple[float, float]:
    inverse = 1.0 - t
    return (
        inverse ** 3 * start[0]
        + 3 * inverse ** 2 * t * control1[0]
        + 3 * inverse * t ** 2 * control2[0]
        + t ** 3 * end[0],
        inverse ** 3 * start[1]
        + 3 * inverse ** 2 * t * control1[1]
        + 3 * inverse * t ** 2 * control2[1]
        + t ** 3 * end[1],
    )


def _cubic_tangent(
    start: tuple[float, float],
    control1: tuple[float, float],
    control2: tuple[float, float],
    end: tuple[float, float],
    t: float,
) -> tuple[float, float]:
    inverse = 1.0 - t
    return (
        3 * inverse ** 2 * (control1[0] - start[0])
        + 6 * inverse * t * (control2[0] - control1[0])
        + 3 * t ** 2 * (end[0] - control2[0]),
        3 * inverse ** 2 * (control1[1] - start[1])
        + 6 * inverse * t * (control2[1] - control1[1])
        + 3 * t ** 2 * (end[1] - control2[1]),
    )


def _overlap_area(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    margin: float = 0.0,
) -> float:
    left = max(first[0] - margin, second[0] - margin)
    top = max(first[1] - margin, second[1] - margin)
    right = min(first[2] + margin, second[2] + margin)
    bottom = min(first[3] + margin, second[3] + margin)
    return max(0.0, right - left) * max(0.0, bottom - top)


def _node_obstacles(root: ET.Element) -> list[tuple[float, float, float, float]]:
    obstacles: list[tuple[float, float, float, float]] = []
    for node_group in root.findall(f".//{_svg_tag('g')}[@class='node']"):
        translation = _numbers(node_group.get("transform", ""))
        offset_x, offset_y = (translation + [0.0, 0.0])[:2]
        ellipse = node_group.find(_svg_tag("ellipse"))
        polygon = node_group.find(_svg_tag("polygon"))
        if ellipse is not None:
            center_x = float(ellipse.get("cx", "0"))
            center_y = float(ellipse.get("cy", "0"))
            radius_x = float(ellipse.get("rx", "0"))
            radius_y = float(ellipse.get("ry", "0"))
            obstacles.append((
                center_x - radius_x + offset_x,
                center_y - radius_y + offset_y,
                center_x + radius_x + offset_x,
                center_y + radius_y + offset_y,
            ))
        elif polygon is not None:
            values = _numbers(polygon.get("points", ""))
            points = list(zip(values[0::2], values[1::2]))
            if points:
                obstacles.append((
                    min(point[0] for point in points) + offset_x,
                    min(point[1] for point in points) + offset_y,
                    max(point[0] for point in points) + offset_x,
                    max(point[1] for point in points) + offset_y,
                ))
    return obstacles


def _node_geometries(root: ET.Element) -> dict[str, dict[str, Any]]:
    geometries: dict[str, dict[str, Any]] = {}
    for node_group in root.findall(f".//{_svg_tag('g')}[@class='node']"):
        title = node_group.find(_svg_tag("title"))
        if title is None or not title.text:
            continue
        ellipse = node_group.find(_svg_tag("ellipse"))
        polygon = node_group.find(_svg_tag("polygon"))
        if ellipse is not None:
            geometries[title.text] = {
                "kind": "ellipse",
                "center": (
                    float(ellipse.get("cx", "0")),
                    float(ellipse.get("cy", "0")),
                ),
                "radius": (
                    float(ellipse.get("rx", "0")),
                    float(ellipse.get("ry", "0")),
                ),
            }
        elif polygon is not None:
            values = _numbers(polygon.get("points", ""))
            points = list(zip(values[0::2], values[1::2]))
            if not points:
                continue
            left = min(point[0] for point in points)
            top = min(point[1] for point in points)
            right = max(point[0] for point in points)
            bottom = max(point[1] for point in points)
            geometries[title.text] = {
                "kind": "rectangle",
                "center": ((left + right) / 2, (top + bottom) / 2),
                "radius": ((right - left) / 2, (bottom - top) / 2),
            }
    return geometries


def _boundary_port(
    geometry: dict[str, Any],
    direction: tuple[float, float],
) -> tuple[float, float]:
    center_x, center_y = geometry["center"]
    radius_x, radius_y = geometry["radius"]
    dx, dy = direction
    if math.hypot(dx, dy) < 1e-6:
        dx, dy = 1.0, 0.0
    if geometry["kind"] == "ellipse":
        scale = 1.0 / math.sqrt(
            (dx / max(radius_x, 1.0)) ** 2
            + (dy / max(radius_y, 1.0)) ** 2
        )
    else:
        scales = []
        if abs(dx) > 1e-6:
            scales.append(radius_x / abs(dx))
        if abs(dy) > 1e-6:
            scales.append(radius_y / abs(dy))
        scale = min(scales) if scales else 0.0
    return center_x + dx * scale, center_y + dy * scale


def _shared_petal_ports(
    root: ET.Element,
    topology: dict[str, Any] | None,
    geometries: dict[str, dict[str, Any]] | None = None,
) -> dict[tuple[int, str], tuple[float, float]]:
    """Share both ports on 2-edge lobes; split ordinary nodes on larger rings."""
    if not topology:
        return {}
    geometries = _node_geometries(root) if geometries is None else geometries
    ports: dict[tuple[int, str], tuple[float, float]] = {}
    for petal_index, structure in enumerate(topology["structures"]):
        path = structure["nodes"]
        neighbors: defaultdict[str, list[str]] = defaultdict(list)
        for source, target in zip(path, path[1:]):
            if target not in neighbors[source]:
                neighbors[source].append(target)
            if source not in neighbors[target]:
                neighbors[target].append(source)
        is_two_edge_lobe = structure["kind"] == "cycle" and len(structure["orders"]) == 2
        for node, adjacent_nodes in neighbors.items():
            if (
                node != structure.get("center", topology["center"])
                and not is_two_edge_lobe
            ):
                continue
            geometry = geometries.get(node)
            adjacent_geometries = [
                geometries[neighbor]
                for neighbor in adjacent_nodes
                if neighbor in geometries and neighbor != node
            ]
            if geometry is None or not adjacent_geometries:
                continue
            center_x, center_y = geometry["center"]
            target = (
                sum(item["center"][0] for item in adjacent_geometries) / len(adjacent_geometries),
                sum(item["center"][1] for item in adjacent_geometries) / len(adjacent_geometries),
            )
            ports[(petal_index, node)] = _boundary_port(
                geometry,
                (target[0] - center_x, target[1] - center_y),
            )
    return ports


def _topology_svg_metadata(
    root: ET.Element,
    topology: dict[str, Any] | None,
    node_geometries: dict[str, dict[str, Any]],
) -> tuple[dict[int, tuple[int, str]], dict[int, tuple[float, float]]]:
    """Index edge orders by petal and mark the topology center in the SVG."""
    order_structure: dict[int, tuple[int, str]] = {}
    petal_interiors: dict[int, tuple[float, float]] = {}
    if not topology:
        return order_structure, petal_interiors

    for petal_index, structure in enumerate(topology["structures"]):
        for order in structure["orders"]:
            if order is not None:
                order_structure[int(order)] = (petal_index, structure["kind"])
        if structure["kind"] != "cycle":
            continue
        centers = [
            node_geometries[node]["center"]
            for node in dict.fromkeys(structure["nodes"])
            if node in node_geometries
        ]
        if centers:
            petal_interiors[petal_index] = (
                sum(point[0] for point in centers) / len(centers),
                sum(point[1] for point in centers) / len(centers),
            )

    for node_group in root.findall(f".//{_svg_tag('g')}[@class='node']"):
        title = node_group.find(_svg_tag("title"))
        if title is not None and title.text == topology["center"]:
            node_group.set("data-topology-center", "true")
    return order_structure, petal_interiors


def _edge_topology_identity(
    edge_group: ET.Element,
    order_structure: dict[int, tuple[int, str]],
) -> tuple[str, int | None]:
    """Read an SVG edge identity and attach stable topology attributes."""
    title = edge_group.find(_svg_tag("title"))
    edge_key = title.text if title is not None and title.text else ""
    edge_text = " ".join(
        text.text or "" for text in edge_group.findall(_svg_tag("text"))
    )
    order_match = ORDER_RE.search(edge_text)
    petal_index: int | None = None
    if order_match and int(order_match.group(1)) in order_structure:
        edge_order = int(order_match.group(1))
        petal_index, structure_kind = order_structure[edge_order]
        edge_group.set("data-edge-order", str(edge_order))
        edge_group.set("data-petal-index", str(petal_index))
        edge_group.set("data-topology-kind", structure_kind)
        edge_group.set("data-shared-ports", "true")
    return edge_key, petal_index


def _edge_shape_elements(
    edge_group: ET.Element,
) -> tuple[ET.Element, list[ET.Element], list[float]] | None:
    """Return the Graphviz path, polygons, and path coordinates for one edge."""
    path = edge_group.find(_svg_tag("path"))
    polygons = edge_group.findall(_svg_tag("polygon"))
    if path is None or not polygons:
        return None
    path_values = _numbers(path.get("d", ""))
    if len(path_values) < 4:
        return None
    return path, polygons, path_values


def _edge_endpoint_geometry(
    edge_key: str,
    petal_index: int | None,
    path_values: list[float],
    polygons: list[ET.Element],
    shared_ports: dict[tuple[int, str], tuple[float, float]],
) -> dict[str, Any] | None:
    """Resolve shared ports and retain Graphviz's original arrow dimensions."""
    source_node, separator, target_node = edge_key.partition("->")
    start = shared_ports.get(
        (petal_index, source_node),
        (path_values[0], path_values[1]),
    ) if petal_index is not None and separator else (path_values[0], path_values[1])
    original_end = (path_values[-2], path_values[-1])
    arrow = next(
        (polygon for polygon in polygons if polygon.get("fill", "").lower() != "white"),
        None,
    )
    if arrow is None:
        return None
    arrow_values = _numbers(arrow.get("points", ""))
    arrow_points = list(zip(arrow_values[0::2], arrow_values[1::2]))
    if len(arrow_points) < 3:
        return None
    original_tip = max(
        arrow_points,
        key=lambda point: math.hypot(point[0] - original_end[0], point[1] - original_end[1]),
    )
    tip = shared_ports.get(
        (petal_index, target_node),
        original_tip,
    ) if petal_index is not None and separator else original_tip
    base_points = [point for point in arrow_points if point != original_tip]
    if len(base_points) < 2:
        return None
    original_base = (
        sum(point[0] for point in base_points) / len(base_points),
        sum(point[1] for point in base_points) / len(base_points),
    )
    return {
        "arrow": arrow,
        "start": start,
        "tip": tip,
        "arrow_length": max(
            7.0,
            math.hypot(
                original_tip[0] - original_base[0],
                original_tip[1] - original_base[1],
            ),
        ),
        "arrow_half_width": max(
            3.0,
            math.hypot(
                base_points[0][0] - base_points[1][0],
                base_points[0][1] - base_points[1][1],
            ) / 2,
        ),
    }


def _curved_edge_geometry(
    endpoint: dict[str, Any],
    lane_index: int,
) -> dict[str, Any] | None:
    """Build one cubic Bezier and an aligned arrow from resolved endpoints."""
    start = endpoint["start"]
    tip = endpoint["tip"]
    dx = tip[0] - start[0]
    dy = tip[1] - start[1]
    distance = math.hypot(dx, dy)
    if distance < 1.0:
        return None
    perpendicular = (-dy / distance, dx / distance)
    lane_scale = 1.0 + 0.28 * lane_index
    curvature = min(88.0, max(26.0, distance * 0.10)) * lane_scale
    control1 = (
        start[0] + dx * 0.28 + perpendicular[0] * curvature,
        start[1] + dy * 0.28 + perpendicular[1] * curvature,
    )
    control2 = (
        start[0] + dx * 0.72 + perpendicular[0] * curvature,
        start[1] + dy * 0.72 + perpendicular[1] * curvature,
    )
    tangent_dx = tip[0] - control2[0]
    tangent_dy = tip[1] - control2[1]
    tangent_length = max(math.hypot(tangent_dx, tangent_dy), 1.0)
    tangent = (tangent_dx / tangent_length, tangent_dy / tangent_length)
    tangent_perpendicular = (-tangent[1], tangent[0])
    curve_end = (
        tip[0] - tangent[0] * endpoint["arrow_length"],
        tip[1] - tangent[1] * endpoint["arrow_length"],
    )
    arrow_base_left = (
        curve_end[0] + tangent_perpendicular[0] * endpoint["arrow_half_width"],
        curve_end[1] + tangent_perpendicular[1] * endpoint["arrow_half_width"],
    )
    arrow_base_right = (
        curve_end[0] - tangent_perpendicular[0] * endpoint["arrow_half_width"],
        curve_end[1] - tangent_perpendicular[1] * endpoint["arrow_half_width"],
    )
    return {
        "start": start,
        "control1": control1,
        "control2": control2,
        "curve_end": curve_end,
        "tip": tip,
        "arrow_base_left": arrow_base_left,
        "arrow_base_right": arrow_base_right,
    }


def _apply_edge_curve(
    edge_group: ET.Element,
    path: ET.Element,
    arrow: ET.Element,
    curve: dict[str, Any],
) -> None:
    """Write the computed Bezier path and arrow points into the SVG tree."""
    start = curve["start"]
    control1 = curve["control1"]
    control2 = curve["control2"]
    curve_end = curve["curve_end"]
    path.set(
        "d",
        (
            f"M{start[0]:.2f},{start[1]:.2f} "
            f"C{control1[0]:.2f},{control1[1]:.2f} "
            f"{control2[0]:.2f},{control2[1]:.2f} "
            f"{curve_end[0]:.2f},{curve_end[1]:.2f}"
        ),
    )
    arrow.set(
        "points",
        " ".join(
            f"{point[0]:.2f},{point[1]:.2f}"
            for point in (
                curve["tip"], curve["arrow_base_left"], curve["arrow_base_right"]
            )
        ),
    )
    edge_group.set("data-curved", "true")


def _edge_label_item(
    edge_group: ET.Element,
    polygons: list[ET.Element],
    curve: dict[str, Any],
    petal_interior: tuple[float, float] | None,
) -> dict[str, Any] | None:
    """Capture the original label box and the final curve used to place it."""
    label_background = next(
        (polygon for polygon in polygons if polygon.get("fill", "").lower() == "white"),
        None,
    )
    if label_background is None:
        return None
    label_values = _numbers(label_background.get("points", ""))
    label_points = list(zip(label_values[0::2], label_values[1::2]))
    if not label_points:
        return None
    left = min(point[0] for point in label_points)
    top = min(point[1] for point in label_points)
    right = max(point[0] for point in label_points)
    bottom = max(point[1] for point in label_points)
    return {
        "background": label_background,
        "texts": edge_group.findall(_svg_tag("text")),
        "current_center": ((left + right) / 2, (top + bottom) / 2),
        "width": right - left,
        "height": bottom - top,
        "curve": (
            curve["start"], curve["control1"], curve["control2"], curve["curve_end"]
        ),
        "petal_interior": petal_interior,
    }


def _curve_edge_group(
    edge_group: ET.Element,
    directed_indexes: Counter[str],
    order_structure: dict[int, tuple[int, str]],
    shared_ports: dict[tuple[int, str], tuple[float, float]],
    petal_interiors: dict[int, tuple[float, float]],
) -> tuple[dict[str, Any] | None, list[tuple[float, float]]]:
    """Curve one edge and return its deferred label plus collision samples."""
    edge_key, petal_index = _edge_topology_identity(edge_group, order_structure)
    shape = _edge_shape_elements(edge_group)
    if shape is None:
        return None, []
    path, polygons, path_values = shape
    endpoint = _edge_endpoint_geometry(
        edge_key, petal_index, path_values, polygons, shared_ports
    )
    if endpoint is None:
        return None, []
    curve = _curved_edge_geometry(endpoint, directed_indexes[edge_key])
    if curve is None:
        return None, []
    directed_indexes[edge_key] += 1
    _apply_edge_curve(edge_group, path, endpoint["arrow"], curve)
    samples = [
        _cubic_point(
            curve["start"], curve["control1"], curve["control2"],
            curve["curve_end"], index / 24,
        )
        for index in range(25)
    ]
    label_item = _edge_label_item(
        edge_group, polygons, curve, petal_interiors.get(petal_index)
    )
    return label_item, samples


def _label_position_candidates(
    item: dict[str, Any],
    occupied: list[tuple[float, float, float, float]],
    curve_samples: list[tuple[float, float]],
) -> list[tuple[float, tuple[float, float], tuple[float, float, float, float]]]:
    """Score bounded positions along both normal directions of one edge."""
    start, control1, control2, curve_end = item["curve"]
    clearance = item["height"] / 2 + 5.0
    offset_distances = (
        clearance,
        clearance + 10.0,
        clearance + 24.0,
        clearance + 42.0,
    )
    curve_parameters = (0.50, 0.38, 0.62, 0.28, 0.72, 0.20, 0.80)
    candidates: list[
        tuple[float, tuple[float, float], tuple[float, float, float, float]]
    ] = []
    for parameter in curve_parameters:
        point = _cubic_point(start, control1, control2, curve_end, parameter)
        tangent_x, tangent_y = _cubic_tangent(
            start, control1, control2, curve_end, parameter,
        )
        tangent_length = max(math.hypot(tangent_x, tangent_y), 1.0)
        normal = (-tangent_y / tangent_length, tangent_x / tangent_length)
        petal_interior = item["petal_interior"]
        interior_dot = (
            normal[0] * (petal_interior[0] - point[0])
            + normal[1] * (petal_interior[1] - point[1])
            if petal_interior is not None
            else 0.0
        )
        preferred_sign = 1.0 if interior_dot >= 0 else -1.0
        for distance in offset_distances:
            for sign_index, sign in enumerate((preferred_sign, -preferred_sign)):
                offset = distance * sign
                center = (
                    point[0] + normal[0] * offset,
                    point[1] + normal[1] * offset,
                )
                box = (
                    center[0] - item["width"] / 2,
                    center[1] - item["height"] / 2,
                    center[0] + item["width"] / 2,
                    center[1] + item["height"] / 2,
                )
                label_overlap = sum(
                    _overlap_area(box, other, margin=5.0) for other in occupied
                )
                edge_hits = sum(
                    box[0] - 3.0 <= sample[0] <= box[2] + 3.0
                    and box[1] - 3.0 <= sample[1] <= box[3] + 3.0
                    for sample in curve_samples
                )
                outside_penalty = (
                    600_000.0 * sign_index if petal_interior is not None else 0.0
                )
                interior_distance = (
                    math.hypot(
                        center[0] - petal_interior[0], center[1] - petal_interior[1]
                    )
                    if petal_interior is not None
                    else 0.0
                )
                displacement_penalty = (
                    abs(parameter - 0.5) * 180.0
                    + distance * 0.20
                    + interior_distance * 0.04
                )
                candidates.append((
                    edge_hits * 1_000_000.0
                    + label_overlap * 1000.0
                    + outside_penalty
                    + displacement_penalty,
                    center,
                    box,
                ))
    return candidates


def _move_svg_label(item: dict[str, Any], desired_center: tuple[float, float]) -> None:
    """Translate all visual pieces of one Graphviz edge label together."""
    translation = (
        desired_center[0] - item["current_center"][0],
        desired_center[1] - item["current_center"][1],
    )
    transform = f"translate({translation[0]:.2f} {translation[1]:.2f})"
    item["background"].set("transform", transform)
    item["background"].set("fill", "none")
    item["background"].set("stroke", "none")
    item["background"].set("pointer-events", "none")
    for text in item["texts"]:
        text.set("transform", transform)


def _relocate_edge_labels(
    root: ET.Element,
    label_items: list[dict[str, Any]],
    curve_samples: list[tuple[float, float]],
) -> None:
    """Greedily place larger labels first while avoiding nodes and curves."""
    occupied = _node_obstacles(root)
    label_items.sort(key=lambda item: item["width"] * item["height"], reverse=True)
    for item in label_items:
        candidates = _label_position_candidates(item, occupied, curve_samples)
        _, desired_center, selected_box = min(
            candidates, key=lambda candidate: candidate[0]
        )
        _move_svg_label(item, desired_center)
        occupied.append(selected_box)


def _pad_svg_view_box(root: ET.Element) -> None:
    """Leave stable breathing room around the post-processed graph."""
    view_box = _numbers(root.get("viewBox", ""))
    if len(view_box) != 4:
        return
    padding_x = 90.0
    padding_y = 90.0
    root.set(
        "viewBox",
        (
            f"{view_box[0] - padding_x:.2f} {view_box[1] - padding_y:.2f} "
            f"{view_box[2] + padding_x * 2:.2f} {view_box[3] + padding_y * 2:.2f}"
        ),
    )


def enhance_svg_curves(svg: bytes, topology: dict[str, Any] | None = None) -> bytes:
    """Give every transfer a visible curve and align its arrow/label to it."""
    root = ET.fromstring(svg)
    directed_indexes: Counter[str] = Counter()
    label_items: list[dict[str, Any]] = []
    curve_samples: list[tuple[float, float]] = []
    node_geometries = _node_geometries(root)
    shared_ports = _shared_petal_ports(root, topology, node_geometries)
    order_structure, petal_interiors = _topology_svg_metadata(
        root, topology, node_geometries
    )

    for edge_group in root.findall(f".//{_svg_tag('g')}[@class='edge']"):
        label_item, samples = _curve_edge_group(
            edge_group,
            directed_indexes,
            order_structure,
            shared_ports,
            petal_interiors,
        )
        curve_samples.extend(samples)
        if label_item is not None:
            label_items.append(label_item)

    _relocate_edge_labels(root, label_items, curve_samples)
    _pad_svg_view_box(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


# Public rendering entry point


def render_tfg_svg(tx_hash: str, max_order: int | None = None) -> bytes:
    dot_path, call_tree_path = _artifact_paths(tx_hash)
    if not dot_path.is_file():
        raise HTTPException(status_code=404, detail="Run transaction analysis first")
    dot_source = dot_path.read_text(encoding="utf-8")
    if max_order is not None:
        try:
            dot_source = filter_dot_through_order(dot_source, max_order)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    center = topology_center(dot_source, call_tree_path)
    topology = detect_topology(dot_source, center)
    positions = topology_positions(dot_source, topology)
    initial_dot = presentation_dot(topology_dot(dot_source, topology, positions))
    node_sizes = _measure_node_sizes(initial_dot)
    positions = _separate_overlapping_nodes(
        positions,
        node_sizes,
        center,
    )
    dot_source = presentation_dot(
        topology_dot(dot_source, topology, positions)
    )
    command = ["neato", "-n2", "-Gsplines=curved", "-Tsvg"]
    try:
        completed = subprocess.run(
            command,
            input=dot_source.encode("utf-8"),
            capture_output=True,
            check=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Topology layout timed out") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", b"") or b""
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise HTTPException(status_code=500, detail=detail or "Topology layout failed") from exc
    return enhance_svg_curves(completed.stdout, topology)
