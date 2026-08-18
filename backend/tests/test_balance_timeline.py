import unittest

from utils.extract_token_changes import build_balance_timeline


class BalanceTimelineTests(unittest.TestCase):
    def test_emits_raw_transfer_deltas_in_playback_order(self):
        artifact = build_balance_timeline(
            [
                {
                    "order": 3,
                    "from": "0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
                    "to": "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                    "amount_raw": 1250000,
                    "token": "USDC",
                    "token_addr": "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
                    "decimals": 6,
                },
                {
                    "order": 0,
                    "from": "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                    "to": "0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
                    "amount_raw": 10**18,
                    "token": "ETH",
                    "token_addr": "ETH",
                    "decimals": 18,
                },
            ],
        )

        self.assertEqual(artifact["schema_version"], 1)
        self.assertEqual([event["order"] for event in artifact["events"]], [0, 3])
        self.assertEqual(artifact["events"][0]["token_address"], "eth")
        self.assertEqual(
            artifact["events"][1]["deltas"],
            [
                {
                    "address": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "amount_raw": "-1250000",
                },
                {
                    "address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "amount_raw": "1250000",
                },
            ],
        )

    def test_mint_and_burn_change_only_the_holder(self):
        artifact = build_balance_timeline(
            [],
            [
                {
                    "order": 2,
                    "user": "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                    "value": 500,
                    "token": "WETH",
                    "token_addr": "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
                    "decimals": 18,
                },
                {
                    "order": 4,
                    "user": "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                    "value": -200,
                    "token": "WETH",
                    "token_addr": "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
                    "decimals": 18,
                },
            ],
        )

        mint, burn = artifact["events"]
        self.assertEqual(mint["kind"], "mint")
        self.assertEqual(mint["deltas"], [{
            "address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "amount_raw": "500",
        }])
        self.assertEqual(burn["kind"], "burn")
        self.assertEqual(burn["deltas"][0]["amount_raw"], "-200")


if __name__ == "__main__":
    unittest.main()
