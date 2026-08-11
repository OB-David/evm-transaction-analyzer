import unittest

from utils.evm_information import TraceFormatter


class CallValueClassificationTests(unittest.TestCase):
    def test_call_with_zero_value_is_not_an_eth_transfer(self):
        stack = ["0x0", "0x0", "0x0"]

        self.assertFalse(TraceFormatter._call_has_nonzero_value("CALL", stack))

    def test_call_with_nonzero_value_is_an_eth_transfer(self):
        stack = ["0x1", "0x1234", "0x5208"]

        self.assertTrue(TraceFormatter._call_has_nonzero_value("CALL", stack))

    def test_non_value_call_opcodes_do_not_create_eth_users(self):
        stack = ["0x1", "0x1234", "0x5208"]

        for opcode in ("STATICCALL", "DELEGATECALL", "CALLCODE"):
            with self.subTest(opcode=opcode):
                self.assertFalse(TraceFormatter._call_has_nonzero_value(opcode, stack))


if __name__ == "__main__":
    unittest.main()
