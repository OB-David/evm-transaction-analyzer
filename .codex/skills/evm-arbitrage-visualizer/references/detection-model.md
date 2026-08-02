# Detection model and limits

## Transfer reconstruction

`pair_transactions` begins with top-level ETH at order 0. ETH changes become directed edges. ERC-20 changes queue by token address and pair only when the last two values cancel exactly; negative is sender and positive receiver. Same-user pairs are not emitted. Remaining changes become `pending_erc20`.

An exact-value, opposite-direction ETH mirror is detected before ordinary ERC-20 pairing for wrap/unwrap-like behavior. The ERC-20 side stays pending and renders as mint/burn; nearest execution step wins among equal candidates. Orders are compacted across paired/pending records, with source PCs/steps retained.

## Current candidate rule

`detect_arbitrage` builds paired `from → to` edges and pending mint `token → user` or burn `user → token` edges. DFS starts at every node, avoids repeating `(from,to,order)`, and limits the open path to fewer than 10 edges. It accepts a closure with at least three total edges, more than one token label, and a closing token equal to the first token.

It returns cycle order lists and their union. Deduplication uses unordered order sets, and orders found from one start node are accumulated before output, so overlapping cycles can merge. Preserve ordered per-cycle paths when improving it.

## Limits and profit

The rule does not check profit, prices, gas, priority fees, builder payments, flash-loan fees, slippage, protocol intent, or beneficiary identity. Call positives “candidate arbitrage cycles” unless economic validation is added.

`compute_address_balances` produces per-symbol deltas; symbols can collide. Robust profit work must key by `(chain_id, token_address)`, attach display metadata, identify beneficiaries, value at a declared block/source in one numeraire, subtract explicit costs, and retain unpriced assets separately. Never add unlike token units.

## Swap hints and tests

`utils/indentify_swap.py` searches for `MUL, LT, JUMPI, GT, JUMPI, DIV` and `ISZERO, MUL, DIV, EQ`. These are static navigation hints, not proof.

Test profitable and unprofitable closures, normal router swaps, flash-loan repayment, wrap/unwrap ambiguity, mint/burn, order 0, disjoint/overlapping cycles, parallel edges, duplicate symbols, non-18 decimals, self/zero transfers, malformed fields, and path-length boundaries.
