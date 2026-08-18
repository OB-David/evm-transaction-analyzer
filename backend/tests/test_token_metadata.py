from __future__ import annotations

import unittest
from unittest.mock import call, patch

from utils.evm_information import TraceFormatter


class TokenMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.formatter = object.__new__(TraceFormatter)
        self.address = "0x0000000000000000000000000000000000000001"

    def test_symbol_is_preferred_over_name(self) -> None:
        with patch.object(
            self.formatter,
            "_read_token_text",
            return_value="fUSDC",
        ) as read_text:
            label = self.formatter._read_token_label(self.address)

        self.assertEqual(label, "fUSDC")
        read_text.assert_called_once_with(self.address, "symbol")

    def test_name_is_used_when_symbol_is_empty(self) -> None:
        with patch.object(
            self.formatter,
            "_read_token_text",
            side_effect=["", "USD Coin"],
        ) as read_text:
            label = self.formatter._read_token_label(self.address)

        self.assertEqual(label, "USD Coin")
        self.assertEqual(
            read_text.call_args_list,
            [call(self.address, "symbol"), call(self.address, "name")],
        )


if __name__ == "__main__":
    unittest.main()
