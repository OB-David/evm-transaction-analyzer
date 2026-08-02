# Visualization contract

## Evidence layers

Use four linked layers: corpus/block overview with source freshness; transaction status/profit/limitations; ordered AFG with individually selectable cycles; and AFG edge → folded CFG → plain CFG → PC/step/sequence evidence.

## Stable join

The current UI joins by integer transfer order: DOT labels contain `(order)`; `arbitrage.json` lists the order; both `TFG_link_*` files use it as `edge_id`. Label parsing is fragile, so prefer adding machine-readable attributes while retaining visible order. Until then preserve `(number)`. Version schema migrations or add backward-compatible readers.

## Encoding

Store full addresses in data/title and aliases in labels. Users are diamonds, token contracts ellipses, other contracts records/rectangles. Transfers are solid and mint/burn dashed. Labels include order, symbol, normalized amount; details expose token address/raw value. Candidate emphasis needs width plus legend, not color/animation alone. Profit must textually say positive, negative, or unknown. Share palette mappings with `frontend/src/visualTheme.ts`.

## Interaction and failure states

Selecting an AFG edge emits every mapped block. Folded/plain switching uses the matching link artifact and retains logical selection when possible. Candidate-only filtering is reversible. Isolate overlapping cycles one at a time. Balance details expose canonical assets. SVG export preserves current labels/highlights.

Distinguish no candidate, unsupported transaction, missing legacy artifact, incomplete analysis, stale Dune data, and RPC/render/API failure. The current missing `arbitrage.json` fallback is false; prefer unknown/not analyzed.

## Acceptance

Orders agree across DOT, detector JSON, and both mappings; decimals and raw values reconcile; directed cycle order closes; profit reconciles to deltas/costs; overlapping cycles remain distinct; missing and negative states differ; keyboard/mouse, zoom, filter, mode switch, and export preserve context.
