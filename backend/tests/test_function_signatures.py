from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from signature.store import (
    FunctionSignatureStore,
    extract_function_name,
    initialize_database,
    set_metadata,
    upsert_api_records,
)
from signature.sync import compact_existing_database
from utils.sequence_diagram import _resolve_probable_signatures


class FunctionSignatureStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "signatures.sqlite3"
        connection = sqlite3.connect(self.db_path)
        initialize_database(connection)
        upsert_api_records(
            connection,
            [
                {
                    "id": 20,
                    "hex_signature": "0x12345678",
                    "text_signature": "ordinary(uint256)",
                },
                {
                    "id": 10,
                    "hex_signature": "0x12345678",
                    "text_signature": "transfer(address,uint256)",
                },
                {
                    "id": 30,
                    "hex_signature": "0x12345678",
                    "text_signature": "ordinary(bytes32)",
                },
                {
                    "id": 15,
                    "hex_signature": "0x12345678",
                    "text_signature": "approve(address,uint256)",
                },
                {
                    "id": 40,
                    "hex_signature": "0x12345678",
                    "text_signature": "invalid signature",
                },
            ],
        )
        set_metadata(connection, "sync_complete", "true")
        set_metadata(connection, "record_count", "5")
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_all_full_signatures_are_retained(self) -> None:
        with FunctionSignatureStore(self.db_path) as store:
            records = store.lookup("0x12345678")

        self.assertEqual(len(records), 5)
        self.assertEqual(
            {record.text_signature for record in records},
            {
                "ordinary(uint256)",
                "transfer(address,uint256)",
                "ordinary(bytes32)",
                "approve(address,uint256)",
                "invalid signature",
            },
        )

    def test_schema_drops_unused_per_record_fields(self) -> None:
        connection = sqlite3.connect(self.db_path)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(function_signatures)")
        }
        connection.close()

        self.assertEqual(
            columns,
            {"api_id", "selector", "text_signature", "function_name", "priority_rank"},
        )

    def test_display_names_are_priority_sorted_and_deduplicated(self) -> None:
        with FunctionSignatureStore(self.db_path) as store:
            names = store.lookup_display_names("0x12345678")

        self.assertEqual(names, ["transfer()", "approve()", "ordinary()"])

    def test_per_render_cache_avoids_repeated_sqlite_queries(self) -> None:
        with FunctionSignatureStore(self.db_path) as store:
            cache: dict[str, list[str]] = {}
            first = _resolve_probable_signatures("0x12345678", cache, store)
            second = _resolve_probable_signatures("0x12345678", cache, store)

        self.assertIs(first, second)
        self.assertEqual(first[0], "transfer()")

    def test_extract_function_name(self) -> None:
        self.assertEqual(extract_function_name("swap(address,uint256)"), "swap")
        self.assertIsNone(extract_function_name("not a signature"))

    def test_compact_existing_database_migrates_v1_atomically(self) -> None:
        old_path = Path(self.temp_dir.name) / "old.sqlite3"
        connection = sqlite3.connect(old_path)
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE function_signatures (
                api_id INTEGER PRIMARY KEY,
                selector TEXT NOT NULL,
                text_signature TEXT NOT NULL,
                function_name TEXT,
                priority_rank INTEGER,
                source TEXT NOT NULL,
                created_at TEXT
            );
            INSERT INTO metadata VALUES ('schema_version', '1');
            INSERT INTO metadata VALUES ('record_count', '1');
            INSERT INTO metadata VALUES ('sync_complete', 'true');
            INSERT INTO function_signatures VALUES (
                1, '0xa9059cbb', 'transfer(address,uint256)',
                'transfer', 0, '4byte.directory', '2020-01-01'
            );
            """
        )
        connection.commit()
        connection.close()

        compact_existing_database(old_path)

        migrated = sqlite3.connect(old_path)
        metadata = dict(migrated.execute("SELECT key, value FROM metadata"))
        columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(function_signatures)")
        }
        migrated.close()
        self.assertEqual(metadata["schema_version"], "2")
        self.assertEqual(metadata["record_count"], "1")
        self.assertNotIn("source", columns)
        self.assertNotIn("created_at", columns)


if __name__ == "__main__":
    unittest.main()
