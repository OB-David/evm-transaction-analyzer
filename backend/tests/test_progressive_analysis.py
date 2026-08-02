import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

import main  # noqa: E402
import main_api  # noqa: E402
import server  # noqa: E402


class ProgressiveAnalysisTests(unittest.TestCase):
    def test_status_writer_publishes_stage_atomically(self):
        with tempfile.TemporaryDirectory() as result_dir:
            main_api._write_analysis_status(result_dir, "afg")

            status_path = os.path.join(result_dir, "analysis_status.json")
            with open(status_path, "r", encoding="utf-8") as f:
                payload = json.load(f)

            self.assertEqual(payload["status"], "processing")
            self.assertEqual(payload["stage"], "afg")
            self.assertFalse(os.path.exists(f"{status_path}.tmp"))

    def test_graph_stages_are_reported_in_progressive_order(self):
        stages: list[str] = []

        def fake_render_transaction(*_args, output_path: str, **_kwargs):
            Path(f"{output_path}.dot").write_text("digraph G {}", encoding="utf-8")
            return {"0xabc": "#ffffff"}

        with tempfile.TemporaryDirectory() as result_dir, patch.object(
            main, "render_transaction", side_effect=fake_render_transaction
        ), patch.object(
            main, "get_valid_nodes_and_colors", return_value=([], [], {"0xabc": "#ffffff"})
        ), patch.object(main, "render_asset_flow"), patch.object(
            main, "tree_to_puml"
        ), patch.object(main, "render_puml_to_svg"), patch("subprocess.run"):
            main.save_graphs(
                result_dir=result_dir,
                plain_cfg=object(),
                folded_cfg=object(),
                full_address_name_map={"0xabc": "contract_to"},
                erc20_token_map={},
                users_addresses=[],
                pairs=[],
                annotations=[],
                pending_erc20=[],
                tree_data={},
                arb_result={"arb_edge_orders": set()},
                progress_callback=stages.append,
            )

        self.assertEqual(stages, ["afg", "sequence", "folded_cfg", "plain_cfg"])

    def test_server_status_reader_matches_progress_contract(self):
        tx_hash = f"0x{'1' * 64}"
        with tempfile.TemporaryDirectory() as workdir:
            previous = os.getcwd()
            os.chdir(workdir)
            try:
                server._write_server_analysis_status(tx_hash, "queued")
                response = server._read_analysis_status(tx_hash)
            finally:
                os.chdir(previous)

        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(response.status, "processing")
        self.assertEqual(response.stage, "queued")


if __name__ == "__main__":
    unittest.main()
