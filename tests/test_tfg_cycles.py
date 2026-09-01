import unittest

from utils.tfg_cycles import (
    ETHEREUM_MAINNET_WETH_ADDRESS,
    detect_tfg_cycles,
)


def transfer(order, source, target, token_address, amount_raw):
    return {
        "order": order,
        "from": source,
        "to": target,
        "token_addr": token_address,
        "amount_raw": amount_raw,
    }


class EthWethTokenCycleTests(unittest.TestCase):
    def test_eth_and_mainnet_weth_close_the_same_token_cycle(self):
        result = detect_tfg_cycles([
            transfer(1, "0xarb", "0xpool", "ETH", 100),
            transfer(
                2,
                "0xpool",
                "0xarb",
                ETHEREUM_MAINNET_WETH_ADDRESS.upper(),
                110,
            ),
        ])

        self.assertTrue(result["has_structural_paths"])
        self.assertEqual(len(result["selected_paths"]), 1)
        path = next(
            candidate
            for candidate in result["minimal_paths"]
            if candidate["anchor_address"] == "0xarb"
        )
        self.assertEqual(
            path["token_address_path"],
            ["eth", ETHEREUM_MAINNET_WETH_ADDRESS],
        )
        self.assertEqual(path["token_identity_path"], ["eth", "eth"])
        self.assertEqual(path["arbitrage_token_address"], "eth")
        self.assertEqual(path["arbitrage_token_identity"], "eth")
        self.assertEqual(path["arbitrage_amount_delta_raw"], "10")

    def test_unrelated_token_address_is_not_equated_by_weth_symbol(self):
        result = detect_tfg_cycles([
            transfer(1, "0xarb", "0xpool", "ETH", 100),
            {
                **transfer(2, "0xpool", "0xarb", "0xnot-weth", 110),
                "token": "WETH",
            },
        ])

        self.assertTrue(result["has_address_cycles"])
        self.assertFalse(result["has_structural_paths"])

    def test_zero_delta_eth_weth_closure_is_not_an_arbitrage_path(self):
        result = detect_tfg_cycles([
            transfer(1, "0xarb", "0xpool", "ETH", 100),
            transfer(2, "0xpool", "0xarb", ETHEREUM_MAINNET_WETH_ADDRESS, 100),
        ])

        self.assertTrue(result["has_address_cycles"])
        self.assertFalse(result["has_structural_paths"])


if __name__ == "__main__":
    unittest.main()
