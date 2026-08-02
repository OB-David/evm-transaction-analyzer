# Repository map

## Pipeline

`backend/main_api.py` is the server-facing transaction entry. It validates a contract call, fetches a standardized Geth trace, retrieves bytecode, builds basic blocks and CFGs, extracts asset changes, pairs transfers, detects candidates, writes artifacts, and renders graphs.

1. `utils/evm_information.py`: trace, addresses, slots, token metadata, bytecode.
2. `utils/basic_block.py`: bytecode to basic blocks.
3. `utils/cfg_transaction.py`: plain, folded, and original CFGs plus balance/ETH changes.
4. `utils/extract_token_changes.py`: pairing, mint/burn changes, cycle detection, balances, AFG DOT, AFG-to-CFG mapping.
5. `utils/indentify_swap.py`: ordered opcode-pattern hints for CFG highlighting.
6. `main.save_graphs`: CFG DOT/SVG, legend, AFG, and sequence diagram.

`backend/main.py` is the standalone equivalent with a hard-coded hash; keep shared semantics aligned.

## Result artifacts

Results live under `backend/Result/<hash-without-0x>/` when run from the documented backend directory.

- `trace.json`: standardized execution evidence.
- `balance_and_eth_changes.json`: asset-change evidence.
- `arbitrage.json`: `{is_arbitrage, cycles, arb_edge_orders}`.
- `address_balances.json`: address → token symbol → net delta.
- `asset_flow.dot`: AFG rendered by D3-Graphviz.
- `TFG_link_FCFG.json`, `TFG_link_PCFG.json`: AFG order → folded/plain CFG blocks.
- `folded_cfg.svg`, `plain_cfg.svg`: overview and detailed CFG.
- `folded_blocks_information.json`, `plain_blocks_information.json`: block actions, instructions, steps, and PCs.
- `swap_in_fcfg.json`, `swap_in_pcfg.json`: heuristic swap seed blocks.
- `edge_id-step.json`: folded CFG edge-to-step map.
- `legend.json`: user/token/contract aliases, addresses, colors.
- `trace_sequence.svg` and mappings: sequence evidence and navigation.

## API and frontend

`backend/server.py` runs `main_api.py` via `POST /api/analyze`, serves whitelisted artifacts, provides block gas views, cached Dune hashes, and plain-block LLM analysis. Arbitrage endpoints include `GET/POST /api/arbitrage-hashes` and `GET /api/arbitrage/{tx_hash}`.

`frontend/src/api/analyze.ts` owns artifact types/loaders. `AfgPanel.vue` concurrently loads DOT, edge links, detector output, balances, and legend; it extracts orders from labels like `(7)`, highlights/filter candidates, shows balance tooltips, and emits CFG IDs. `CfgPanel.vue` supplies folded/plain and swap highlighting. `BlockPanel.vue` marks Dune hashes/blocks. `App.vue` coordinates cross-panel selection and SVG export.

## Runtime

Backend needs Python 3.12+, UV, FastAPI/Web3, and `GETH_API`. Dune markers use `DUNE_API` and the fixed query in `arbitrage_crawler.py`. Rendering uses Graphviz and PlantUML/Java. Frontend uses Vue 3, TypeScript, Vite, D3-Graphviz, and Plotly. Pure detector tests can use synthetic fixtures without network access.
