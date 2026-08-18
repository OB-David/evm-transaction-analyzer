"""Build a Graphviz CFG subset without mutating persisted analysis artifacts."""

from __future__ import annotations

import re
from collections.abc import Collection


NODE_ID_RE = re.compile(r"^node_[A-Za-z0-9_.:-]+$")
NODE_LINE_RE = re.compile(r"^\s*(node_[A-Za-z0-9_.:-]+)\s+\[")
EDGE_LINE_RE = re.compile(
    r"^\s*(node_[A-Za-z0-9_.:-]+)\s*->\s*(node_[A-Za-z0-9_.:-]+)\s+\["
)


def _validate_node_ids(node_ids: Collection[str]) -> set[str]:
    selected = set(node_ids)
    if not selected:
        raise ValueError("At least one CFG node is required")
    if len(selected) > 2_000:
        raise ValueError("A CFG subset may contain at most 2000 nodes")
    if any(not NODE_ID_RE.fullmatch(node_id) for node_id in selected):
        raise ValueError("Invalid CFG node id")
    return selected


def build_cfg_subset_dot(
    source: str,
    node_ids: Collection[str],
    edge_titles: Collection[str] = (),
) -> str:
    """Return the induced selected-node DOT graph, preserving contract clusters.

    ``render_cfg.render_transaction`` emits one non-nested subgraph per contract.
    This deliberately small parser targets that stable writer contract instead of
    accepting arbitrary DOT input from a request.
    """

    selected = _validate_node_ids(node_ids)
    selected_edges = set(edge_titles)
    lines = source.splitlines()
    if not lines or not lines[0].lstrip().startswith("digraph"):
        raise ValueError("Unsupported CFG DOT source")

    output: list[str] = []
    found_nodes: set[str] = set()
    index = 0

    while index < len(lines):
        line = lines[index]
        if line.strip().startswith("subgraph ") and line.rstrip().endswith("{"):
            block = [line]
            index += 1
            while index < len(lines):
                block.append(lines[index])
                if lines[index].strip() == "}":
                    break
                index += 1

            kept_node_lines: list[str] = []
            style_lines: list[str] = []
            for block_line in block[1:-1]:
                node_match = NODE_LINE_RE.match(block_line)
                if node_match:
                    node_id = node_match.group(1)
                    if node_id in selected:
                        found_nodes.add(node_id)
                        kept_node_lines.append(block_line)
                else:
                    style_lines.append(block_line)

            if kept_node_lines:
                output.extend([block[0], *style_lines, *kept_node_lines, block[-1]])
        else:
            node_match = NODE_LINE_RE.match(line)
            edge_match = EDGE_LINE_RE.match(line)
            if node_match:
                node_id = node_match.group(1)
                if node_id in selected:
                    found_nodes.add(node_id)
                    output.append(line)
            elif edge_match:
                source_id, target_id = edge_match.groups()
                title = f"{source_id}->{target_id}"
                if (
                    source_id in selected
                    and target_id in selected
                    and (not selected_edges or title in selected_edges)
                ):
                    output.append(line)
            else:
                output.append(line)
        index += 1

    missing = selected - found_nodes
    if missing:
        raise ValueError(f"CFG nodes not found: {', '.join(sorted(missing)[:5])}")
    return "\n".join(output) + "\n"
