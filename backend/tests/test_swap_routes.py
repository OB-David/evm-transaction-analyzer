from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from utils.extract_token_changes import (
    compute_address_balances,
    pair_transactions,
    render_asset_flow,
)
from utils.swap_routes import build_arbitrage_artifact, detect_arbitrage


ROUTE = "0x" + "11" * 20
HELPER = "0x" + "22" * 20
FIRST_VENUE = "0x" + "33" * 20
BNT = "0x" + "44" * 20
OUTER_VENUE = "0x" + "55" * 20
SECOND_VENUE = "0x" + "66" * 20
BAT = "0x" + "77" * 20
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"


def _native_edge(order: int, sender: str, receiver: str, amount: int, step: int) -> dict:
    return {
        "order": order,
        "from": sender,
        "to": receiver,
        "amount_raw": amount,
        "amount": amount / 10**18,
        "token": "ETH",
        "token_addr": "ETH",
        "decimals": 18,
        "source_steps": [step],
    }


def _token_edge(
    order: int,
    sender: str,
    receiver: str,
    amount: int,
    token: str,
    step: int,
) -> dict:
    return {
        "order": order,
        "from": sender,
        "to": receiver,
        "amount_raw": amount,
        "amount": amount / 10**18,
        "token": token,
        "token_addr": token,
        "decimals": 18,
        "source_steps": {
            "sender_sload_step": step - 1,
            "sender_sstore_step": step,
            "receiver_sload_step": step,
            "receiver_sstore_step": step + 1,
        },
    }


def _frame(
    call_id: int,
    parent_call_id: int | None,
    depth: int,
    entry_step: int,
    exit_step: int,
    sender: str,
    receiver: str,
) -> dict:
    return {
        "call_id": call_id,
        "parent_call_id": parent_call_id,
        "depth": depth,
        "entry_step": entry_step,
        "exit_step": exit_step,
        "from_address": sender,
        "to_address": receiver,
    }


class TransferAccountingTests(unittest.TestCase):
    def test_pairing_and_pending_changes_preserve_evidence(self) -> None:
        changes = [
            {
                "type": "ERC20_BALANCE_CHANGE",
                "erc20_token_address": BNT,
                "token_name": "BNT",
                "user_address": ROUTE,
                "changed_balance": "-100",
                "codecontract_address": BNT,
                "SLOAD_pc": "0x1",
                "SSTORE_pc": "0x2",
                "SLOAD_step": 10,
                "SSTORE_step": 11,
            },
            {
                "type": "ERC20_BALANCE_CHANGE",
                "erc20_token_address": BNT,
                "token_name": "BNT",
                "user_address": SECOND_VENUE,
                "changed_balance": "100",
                "codecontract_address": BNT,
                "SLOAD_pc": "0x3",
                "SSTORE_pc": "0x4",
                "SLOAD_step": 12,
                "SSTORE_step": 13,
            },
            {
                "type": "ERC20_BALANCE_CHANGE",
                "erc20_token_address": BAT,
                "token_name": "BAT",
                "user_address": ROUTE,
                "changed_balance": "5",
                "codecontract_address": BAT,
                "SLOAD_pc": "0x5",
                "SSTORE_pc": "0x6",
                "SLOAD_step": 14,
                "SSTORE_step": 15,
            },
        ]

        paired, _, pending = pair_transactions(
            (ROUTE, FIRST_VENUE, "0"), changes, {BNT: 18, BAT: 18}
        )

        self.assertEqual([(edge["from"], edge["to"], edge["amount_raw"]) for edge in paired], [
            (ROUTE, SECOND_VENUE, 100),
        ])
        self.assertEqual([(change["user"], change["value"]) for change in pending], [
            (ROUTE, 5),
        ])

    def test_balance_accounting_includes_pending_supply_boundaries(self) -> None:
        balances = compute_address_balances(
            [_token_edge(1, ROUTE, SECOND_VENUE, 100, BNT, 10)],
            [{"order": 2, "user": ROUTE, "value": 5, "token": "BAT", "decimals": 0}],
        )

        self.assertEqual(balances[ROUTE][BNT], -100 / 10**18)
        self.assertEqual(balances[SECOND_VENUE][BNT], 100 / 10**18)
        self.assertEqual(balances[ROUTE]["BAT"], 5)


class CallbackForwardingArbitrageTests(unittest.TestCase):
    def test_recovers_full_wrapped_native_route_without_callback_pseudo_leg(self) -> None:
        paired = [
            _native_edge(12, WETH, ROUTE, 1_000_000, 3),
            _native_edge(13, ROUTE, HELPER, 999_000, 10),
            _native_edge(14, HELPER, FIRST_VENUE, 999_000, 20),
            _token_edge(15, FIRST_VENUE, ROUTE, 2_000_100, BNT, 29),
            _token_edge(16, OUTER_VENUE, ROUTE, 1_010_000, WETH, 56),
            _token_edge(17, ROUTE, SECOND_VENUE, 2_000_000, BNT, 71),
            _token_edge(18, SECOND_VENUE, OUTER_VENUE, 3_000_000, BAT, 86),
        ]
        pending = [{
            "order": 11,
            "user": ROUTE,
            "value": -1_000_000,
            "token": "WETH",
            "token_addr": WETH,
            "decimals": 18,
            "source_steps": [1, 2],
        }]
        trace_tree = {
            "root": {"address": ROUTE},
            "calls": [
                _frame(10, None, 1, 1, 5, ROUTE, WETH),
                _frame(11, 10, 2, 3, 4, WETH, ROUTE),
                _frame(1, None, 1, 10, 45, ROUTE, HELPER),
                _frame(2, 1, 2, 20, 40, HELPER, FIRST_VENUE),
                _frame(3, 2, 3, 28, 30, FIRST_VENUE, BNT),
                _frame(4, None, 1, 50, 120, ROUTE, OUTER_VENUE),
                _frame(5, 4, 2, 55, 57, OUTER_VENUE, WETH),
                # Direct inverse callback: OUTER_VENUE -> ROUTE.
                _frame(6, 4, 2, 60, 110, OUTER_VENUE, ROUTE),
                _frame(7, 6, 3, 70, 72, ROUTE, BNT),
                _frame(8, 6, 3, 75, 100, ROUTE, SECOND_VENUE),
                _frame(9, 8, 4, 85, 87, SECOND_VENUE, BAT),
            ],
        }

        result = detect_arbitrage(paired, pending, trace_tree)
        artifact = build_arbitrage_artifact(result)

        self.assertNotIn(ROUTE, {leg["venue_address"] for leg in result["swap_legs"]})
        self.assertEqual(len(artifact["selected_cycles"]), 1)
        cycle = artifact["selected_cycles"][0]
        self.assertEqual(cycle["token_address_path"], ["eth", BNT, BAT, WETH])
        self.assertEqual(cycle["connector_edge_orders"], [11, 12, 13])
        self.assertEqual(cycle["swap_transfer_edge_orders"], [14, 15, 16, 17, 18])
        self.assertEqual(cycle["transfer_edge_orders"], list(range(11, 19)))
        self.assertEqual(cycle["route_account"], ROUTE)
        self.assertEqual(cycle["closure_kind"], "wrapped_native")
        self.assertEqual(cycle["arbitrage_token_address"], WETH)
        self.assertEqual(cycle["arbitrage_amount_delta_raw"], "11000")
        self.assertEqual(cycle["arbitrage_direction"], "increase")
        self.assertEqual(artifact["arb_edge_orders"], list(range(11, 19)))

    def test_detects_output_first_sibling_settlement_and_excludes_profit_payout(self) -> None:
        executor = "0x" + "88" * 20
        root = "0x" + "99" * 20
        first_pool = "0x" + "aa" * 20
        second_pool = "0x" + "bb" * 20
        settlement_venue = "0x" + "cc" * 20
        paired = [
            _token_edge(1, first_pool, executor, 1_000_100, WETH, 30),
            _token_edge(2, settlement_venue, first_pool, 3_000, USDT, 47),
            _token_edge(3, second_pool, executor, 2_000, USDC, 54),
            _token_edge(4, executor, second_pool, 1_000_000, WETH, 60),
            _token_edge(5, executor, settlement_venue, 2_000, USDC, 70),
            # Profit payout, not a USDC -> WETH swap at the executor.
            _token_edge(6, executor, root, 100, WETH, 115),
        ]
        trace_tree = {
            "root": {"address": root},
            "calls": [
                _frame(1, None, 1, 10, 100, root, executor),
                _frame(2, 1, 2, 20, 90, executor, first_pool),
                _frame(3, 2, 3, 29, 32, first_pool, WETH),
                _frame(4, 2, 3, 35, 85, first_pool, executor),
                _frame(5, 4, 4, 40, 80, executor, settlement_venue),
                # Proven inverse callback envelope.
                _frame(6, 5, 5, 42, 78, settlement_venue, executor),
                _frame(7, 6, 6, 44, 50, executor, settlement_venue),
                _frame(8, 7, 7, 46, 49, settlement_venue, USDT),
                _frame(9, 6, 6, 52, 65, executor, second_pool),
                _frame(10, 9, 7, 53, 56, second_pool, USDC),
                _frame(11, 9, 7, 59, 62, second_pool, WETH),
                _frame(12, 6, 6, 68, 75, executor, settlement_venue),
                _frame(13, 12, 7, 69, 72, settlement_venue, USDC),
                _frame(14, None, 1, 110, 120, root, executor),
                _frame(15, 14, 2, 114, 117, executor, WETH),
            ],
        }

        result = detect_arbitrage(paired, [], trace_tree)
        artifact = build_arbitrage_artifact(result)

        self.assertEqual(
            {leg["venue_address"] for leg in result["swap_legs"]},
            {first_pool, second_pool, settlement_venue},
        )
        settlement_leg = next(
            leg for leg in result["swap_legs"]
            if leg["venue_address"] == settlement_venue
        )
        self.assertEqual(settlement_leg["scope_kind"], "callback_post_settled")
        self.assertEqual(settlement_leg["input_edge_ids"], [5])
        self.assertEqual(settlement_leg["output_edge_ids"], [2])
        self.assertEqual(len(artifact["selected_cycles"]), 1)
        cycle = artifact["selected_cycles"][0]
        self.assertEqual(cycle["token_address_path"], [WETH, USDC, USDT, WETH])
        self.assertEqual(cycle["transfer_edge_orders"], [1, 2, 3, 4, 5])
        self.assertNotIn(6, cycle["transfer_edge_orders"])
        self.assertEqual(cycle["route_account"], executor)
        self.assertEqual(cycle["arbitrage_token_address"], WETH)
        self.assertEqual(cycle["arbitrage_amount_delta_raw"], "100")
        self.assertEqual(cycle["arbitrage_direction"], "increase")
        self.assertEqual(artifact["schema_version"], 10)

    def test_candidate_burn_remains_dashed_and_is_visibly_emphasized(self) -> None:
        pending = [{
            "order": 11,
            "user": ROUTE,
            "value": -1_000_000,
            "token": "WETH",
            "token_addr": WETH,
            "decimals": 18,
        }]
        with TemporaryDirectory() as temp_dir:
            output_path = str(Path(temp_dir) / "asset_flow.dot")
            dot = render_asset_flow(
                [],
                {},
                [ROUTE],
                {ROUTE: "route", WETH: "WETH"},
                pending,
                {WETH: "#F3DAB5"},
                output_file=output_path,
                arb_edge_orders={11},
            )

        edge_line = next(line for line in dot.source.splitlines() if "WETH(burn)" in line)
        self.assertIn('style="dashed, bold"', edge_line)
        self.assertIn("penwidth=3.1", edge_line)
        self.assertIn("arrowsize=1.02", edge_line)


if __name__ == "__main__":
    unittest.main()
