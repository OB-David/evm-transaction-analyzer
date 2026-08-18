import unittest

from utils.cfg_slice import build_cfg_subset_dot


DOT = """digraph CFG {
  rankdir=LR;
  node [shape=rect];
  subgraph cluster_0 {
    label="";
    color="#111111";
    node_1 [fillcolor="#aaaaaa"];
    node_2 [fillcolor="#aaaaaa"];
  }
  subgraph cluster_1 {
    label="";
    color="#222222";
    node_3 [fillcolor="#bbbbbb"];
  }
  node_1 -> node_2 [color="#111111"]
  node_2 -> node_3 [color="#222222"]
  node_3 -> node_1 [color="#333333"]
}
"""


class CfgSliceTests(unittest.TestCase):
    def test_keeps_selected_nodes_edges_and_clusters(self):
        result = build_cfg_subset_dot(DOT, ["node_2", "node_3"])

        self.assertNotIn("node_1 [", result)
        self.assertIn("node_2 [", result)
        self.assertIn("node_3 [", result)
        self.assertIn("node_2 -> node_3", result)
        self.assertNotIn("node_1 -> node_2", result)
        self.assertIn("subgraph cluster_0", result)
        self.assertIn("subgraph cluster_1", result)

    def test_can_limit_edges_to_the_visible_selection(self):
        result = build_cfg_subset_dot(
            DOT,
            ["node_1", "node_2", "node_3"],
            ["node_2->node_3"],
        )

        self.assertIn("node_2 -> node_3", result)
        self.assertNotIn("node_1 -> node_2", result)
        self.assertNotIn("node_3 -> node_1", result)

    def test_rejects_unknown_or_invalid_nodes(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            build_cfg_subset_dot(DOT, ["node_404"])
        with self.assertRaisesRegex(ValueError, "Invalid"):
            build_cfg_subset_dot(DOT, ["node_1;evil"])


if __name__ == "__main__":
    unittest.main()
