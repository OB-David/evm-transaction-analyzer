import unittest

from utils.tfg_layout import filter_dot_through_order


DOT = '''digraph {
  graph [rankdir=TB]
  "a" [label=<A>]
  "b" [label=<B>]
  "c" [label=<C>]
  "a" -> "b" [label=<(0) ETH: 1> arrowsize=0.8]
  "b" -> "c" [label=<(3) TOKEN: 2> arrowsize=0.8]
  "c" -> "a" [label=<(5) TOKEN: 3> arrowsize=0.8]
}
'''


class TfgPlaybackSliceTests(unittest.TestCase):
    def test_keeps_the_complete_transfer_prefix(self):
        result = filter_dot_through_order(DOT, 3)

        self.assertIn('(0) ETH', result)
        self.assertIn('(3) TOKEN', result)
        self.assertNotIn('(5) TOKEN', result)
        self.assertIn('"a" [', result)
        self.assertIn('"b" [', result)
        self.assertIn('"c" [', result)

    def test_removes_nodes_not_reached_by_the_prefix(self):
        result = filter_dot_through_order(DOT, 0)

        self.assertIn('"a" [', result)
        self.assertIn('"b" [', result)
        self.assertNotIn('"c" [', result)

    def test_rejects_a_negative_prefix(self):
        with self.assertRaisesRegex(ValueError, "non-negative"):
            filter_dot_through_order(DOT, -1)


if __name__ == "__main__":
    unittest.main()
