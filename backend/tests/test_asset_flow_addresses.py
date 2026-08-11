import unittest

from utils.extract_token_changes import (
    collect_asset_flow_addresses,
    filter_asset_flow_user_addresses,
    pair_transactions,
)


class AssetFlowAddressTests(unittest.TestCase):
    def setUp(self):
        self.paired = [
            {"from": "0xUserA", "to": "0xContractA"},
            {"from": "0xContractA", "to": "0xUserB"},
        ]
        self.pending = [
            {"user": "0xUserC", "token_addr": "0xTokenA"},
        ]

    def test_collects_only_addresses_rendered_as_tfg_nodes(self):
        self.assertEqual(
            collect_asset_flow_addresses(self.paired, self.pending),
            {"0xUserA", "0xUserB", "0xUserC", "0xContractA", "0xTokenA"},
        )

    def test_filters_standardized_users_by_tfg_membership(self):
        users = ["0xUserA", "0xUnused", "0xuserb", "0xUserC"]

        self.assertEqual(
            filter_asset_flow_user_addresses(users, self.paired, self.pending),
            ["0xUserA", "0xuserb", "0xUserC"],
        )

    def test_zero_value_top_level_call_does_not_create_eth_edge(self):
        paired, _, pending = pair_transactions(
            ["0xUserA", "0xContractA", 0],
            [],
        )

        self.assertEqual(paired, [])
        self.assertEqual(pending, [])

    def test_real_transfer_after_zero_value_call_starts_at_order_one(self):
        paired, _, _ = pair_transactions(
            ["0xUserA", "0xContractA", 0],
            [{
                "type": "ETH_TRANSFER",
                "eth_value": 10**18,
                "codecontract_address": "0xContractA",
                "from_address": "0xContractA",
                "to_address": "0xUserB",
                "pc": 12,
                "step": 34,
            }],
        )

        self.assertEqual(len(paired), 1)
        self.assertEqual(paired[0]["order"], 1)
        self.assertEqual(paired[0]["amount"], 1)


if __name__ == "__main__":
    unittest.main()
