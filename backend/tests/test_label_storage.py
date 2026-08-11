from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from labels.coordinator import LabelCoordinator


HASH_A = "0x" + "11" * 32
HASH_B = "0x" + "ab" * 32


class LabelStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "arbitrage.sqlite3"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_new_schema_uses_one_without_rowid_blob_primary_key(self) -> None:
        coordinator = LabelCoordinator(self.db_path)
        coordinator.initialize()

        connection = sqlite3.connect(self.db_path)
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'arbitrage_transactions'"
        ).fetchone()[0]
        indexes = connection.execute(
            "PRAGMA index_list(arbitrage_transactions)"
        ).fetchall()
        columns = {
            row[1]: (row[2], row[5])
            for row in connection.execute("PRAGMA table_info(arbitrage_transactions)")
        }
        connection.close()

        self.assertIn("WITHOUT ROWID", schema.upper())
        self.assertEqual(columns["block_number"], ("INTEGER", 1))
        self.assertEqual(columns["tx_hash"], ("BLOB", 2))
        self.assertEqual(len(indexes), 1)
        self.assertEqual(indexes[0][3], "pk")

    def test_storage_is_binary_but_query_contract_stays_text(self) -> None:
        coordinator = LabelCoordinator(self.db_path)
        inserted = coordinator.geth.import_transactions(
            [
                {"tx_hash": HASH_A.upper().replace("0X", "0x"), "block_number": 100},
                {"tx_hash": HASH_B, "block_number": 101},
            ],
            coverage_start_block=100,
            last_scanned_block=101,
        )

        connection = sqlite3.connect(self.db_path)
        stored = connection.execute(
            "SELECT typeof(tx_hash), length(tx_hash) FROM arbitrage_transactions"
        ).fetchall()
        connection.close()

        self.assertEqual(inserted, 2)
        self.assertEqual(stored, [("blob", 32), ("blob", 32)])
        self.assertEqual(
            coordinator.query_transactions(100, 101),
            [
                {"tx_hash": HASH_B, "block_number": 101},
                {"tx_hash": HASH_A, "block_number": 100},
            ],
        )

    def test_legacy_text_schema_is_migrated_without_data_loss(self) -> None:
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            f"""
            CREATE TABLE arbitrage_transactions (
                tx_hash TEXT PRIMARY KEY,
                block_number INTEGER NOT NULL
            );
            CREATE INDEX idx_arbitrage_block_number
                ON arbitrage_transactions(block_number);
            INSERT INTO arbitrage_transactions VALUES ('{HASH_A}', 100);
            INSERT INTO arbitrage_transactions VALUES ('{HASH_B}', 101);
            """
        )
        connection.close()

        coordinator = LabelCoordinator(self.db_path)
        coordinator.initialize()

        migrated = sqlite3.connect(self.db_path)
        rows = migrated.execute(
            "SELECT block_number, typeof(tx_hash), length(tx_hash) "
            "FROM arbitrage_transactions ORDER BY block_number"
        ).fetchall()
        old_index = migrated.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'idx_arbitrage_block_number'"
        ).fetchone()
        migrated.close()

        self.assertEqual(rows, [(100, "blob", 32), (101, "blob", 32)])
        self.assertIsNone(old_index)
        self.assertEqual(coordinator.count_transactions(), 2)
        self.assertEqual(coordinator.recent_transactions(1)[0]["tx_hash"], HASH_B)


if __name__ == "__main__":
    unittest.main()
