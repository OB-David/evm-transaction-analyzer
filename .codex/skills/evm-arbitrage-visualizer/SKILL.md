---
name: evm-arbitrage-visualizer
description: Analyze, explain, implement, or review EVM transaction arbitrage detection visualizations in this repository. Use for asset-flow graphs, multi-token cycle detection, arbitrage evidence, AFG-to-CFG linkage, swap highlighting, profit displays, block-level markers, or changes involving arbitrage.json, asset_flow.dot, TFG_link files, address_balances.json, Vue/Graphviz panels, and related FastAPI endpoints.
---

# EVM Arbitrage Visualizer

Build a traceable arbitrage story from execution evidence. Treat a highlighted cycle as a candidate until token-normalized profit, fees, and detector limitations have been checked.

## Load repository knowledge

Read only the references needed:

- Read [references/repository-map.md](references/repository-map.md) before changing pipeline files, endpoints, artifacts, or frontend loading.
- Read [references/detection-model.md](references/detection-model.md) before interpreting or modifying detection, transfer pairing, profits, wrap/unwrap, or swap heuristics.
- Read [references/visualization-contract.md](references/visualization-contract.md) before changing AFG/CFG interaction, styling, labels, filters, tooltips, legends, or exports.

Inspect current source before editing; these references describe the learned baseline, not a substitute for code.

## Follow the evidence workflow

1. Establish the level: one transaction, block/corpus overview, detector correctness, or UI-only presentation.
2. Trace inputs from standardized execution steps and balance changes. Preserve order, token address, decimals, source PC/step, and code-contract address.
3. Build transfers without dropping unmatched ERC-20 changes. Model unmatched positive changes as mint edges and negative changes as burn edges. Keep ETH/WETH mirror handling explicit.
4. Detect candidate cycles on directed, token-labelled edges. Preserve exact membership and order; do not infer profit from closure alone.
5. Compute address/token net deltas. To confirm economic arbitrage, identify beneficiaries, value flows in one numeraire at execution time, then subtract gas, builder payments, flash-loan fees, and other costs.
6. Keep detector result, AFG, balances, legend, and AFG-to-CFG mappings mutually consistent.
7. Render summary, cycle-focused AFG, balance/profit detail, and drill-down from transfer edges to CFG blocks and trace steps.
8. Test repeated-token paths, mint/burn and wrap/unwrap, multiple cycles, no-cycle swaps, self-transfers, zero values, and missing artifacts.

## Apply visualization rules

- Use stable edge IDs/orders across JSON, DOT labels, DOM interaction, and CFG mapping.
- Encode token identity with labels and accessible color; never rely on color alone.
- Distinguish transfers from mint/burn by line style and candidate edges by emphasis.
- Show why a transaction was flagged: ordered edges, participants, assets, amounts, net deltas, source steps, and limitations.
- Keep “candidate cycle” separate from “profitable arbitrage.” Show unknown profit as unknown, not zero.
- Preserve folded CFG overview and plain CFG instruction evidence.
- Make filters reversible and retain context.
- Do not silently convert missing artifacts or failed analysis into a definitive non-arbitrage result.

## Guard detector integrity

- The current detector proves neither profitability nor MEV intent; it finds certain multi-token directed closures.
- Use chain plus token address as canonical identity; symbols are display metadata.
- Never sum heterogeneous token quantities directly.
- Treat opcode swap patterns only as navigation hints.
- Update backend writers, API, TypeScript contracts, UI consumers, and tests together when changing artifact schemas.
- Preserve raw evidence so every visual claim can be audited back to PC/step and balance change.

## Verify proportionally

For detector changes, add focused Python tests for `pair_transactions`, `detect_arbitrage`, and `compute_address_balances`. For contract changes, test generated JSON/DOT against frontend types. For UI changes, run the frontend type/build check and inspect selection, filtering, tooltips, folded/plain switching, missing-file behavior, and SVG export. Report checks blocked by unavailable RPC, Dune, Graphviz, PlantUML, or LLM services.
