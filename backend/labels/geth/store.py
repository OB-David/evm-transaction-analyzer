"""SQLite cursor and label writes for Geth synchronization."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

from labels.coordinator import DATA_DIR, TX_HASH_RE

if TYPE_CHECKING:
    from labels.coordinator import LabelCoordinator


DEFAULT_GETH_LOCK_PATH = DATA_DIR / "geth_sync.lock"


@dataclass(frozen=True)
class GethSyncState:
    coverage_start_block: int
    last_scanned_block: int


class GethStore:
    def __init__(self, coordinator: LabelCoordinator):
        self.coordinator = coordinator

    def initialize_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS geth_sync_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                coverage_start_block INTEGER NOT NULL CHECK (coverage_start_block >= 0),
                last_scanned_block INTEGER NOT NULL CHECK (
                    last_scanned_block >= coverage_start_block
                )
            )
            """
        )

    def get_sync_state(self) -> GethSyncState | None:
        self.coordinator.initialize()
        with self.coordinator._connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT coverage_start_block, last_scanned_block
                FROM geth_sync_state WHERE singleton = 1
                """
            ).fetchone()
        return GethSyncState(**dict(row)) if row else None

    def import_transactions(
        self,
        transactions: Iterable[dict],
        *,
        coverage_start_block: int,
        last_scanned_block: int,
    ) -> int:
        if coverage_start_block < 0 or last_scanned_block < coverage_start_block:
            raise ValueError("invalid Geth coverage range")
        normalized = self._normalize_transactions(
            transactions,
            minimum_block=coverage_start_block,
            maximum_block=last_scanned_block,
        )
        self.coordinator.initialize()
        with self.coordinator._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_state = connection.execute(
                """
                SELECT coverage_start_block, last_scanned_block
                FROM geth_sync_state WHERE singleton = 1
                """
            ).fetchone()
            requested_state = (coverage_start_block, last_scanned_block)
            if existing_state is None:
                connection.execute(
                    """
                    INSERT INTO geth_sync_state(
                        singleton, coverage_start_block, last_scanned_block
                    ) VALUES (1, ?, ?)
                    """,
                    requested_state,
                )
            elif tuple(existing_state) != requested_state:
                raise RuntimeError(
                    "Geth sync state already exists with a different coverage range"
                )
            inserted = self._insert_transactions(connection, normalized)
        return inserted

    def complete_block(self, block_number: int, transactions: Iterable[dict]) -> int:
        if block_number < 0:
            raise ValueError("block_number must be non-negative")
        normalized = self._normalize_transactions(
            transactions,
            minimum_block=block_number,
            maximum_block=block_number,
        )
        self.coordinator.initialize()
        with self.coordinator._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                """
                SELECT coverage_start_block, last_scanned_block
                FROM geth_sync_state WHERE singleton = 1
                """
            ).fetchone()
            if state is None:
                raise RuntimeError(
                    "Geth sync is not bootstrapped; import a complete dataset first"
                )
            expected_block = int(state["last_scanned_block"]) + 1
            if block_number != expected_block:
                raise RuntimeError(
                    f"Geth block {block_number} is not the next block; "
                    f"expected {expected_block}"
                )
            inserted = self._insert_transactions(connection, normalized)
            changed = connection.execute(
                """
                UPDATE geth_sync_state SET last_scanned_block = ?
                WHERE singleton = 1 AND last_scanned_block = ?
                """,
                (block_number, expected_block - 1),
            ).rowcount
            if changed != 1:
                raise RuntimeError("Geth cursor update lost its consistency guard")
        return inserted

    def coverage_intervals(self, from_block: int, to_block: int) -> list[tuple[int, int]]:
        with self.coordinator._connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT coverage_start_block, last_scanned_block FROM geth_sync_state
                WHERE singleton = 1 AND last_scanned_block >= ?
                  AND coverage_start_block <= ?
                """,
                (from_block, to_block),
            ).fetchone()
        if row is None:
            return []
        return [(int(row["coverage_start_block"]), int(row["last_scanned_block"]))]

    @staticmethod
    def _normalize_transactions(
        transactions: Iterable[dict],
        *,
        minimum_block: int,
        maximum_block: int,
    ) -> list[tuple[str, int]]:
        by_hash: dict[str, int] = {}
        for transaction in transactions:
            tx_hash = str(transaction["tx_hash"]).lower()
            block_number = int(transaction["block_number"])
            if not TX_HASH_RE.fullmatch(tx_hash):
                raise ValueError(f"invalid Geth transaction hash: {tx_hash!r}")
            if not minimum_block <= block_number <= maximum_block:
                raise ValueError(
                    f"block {block_number} outside Geth range "
                    f"[{minimum_block}, {maximum_block}]"
                )
            prior_block = by_hash.setdefault(tx_hash, block_number)
            if prior_block != block_number:
                raise ValueError(f"transaction {tx_hash} appears in multiple blocks")
        return sorted(by_hash.items())

    @staticmethod
    def _insert_transactions(
        connection: sqlite3.Connection,
        transactions: list[tuple[str, int]],
    ) -> int:
        for tx_hash, block_number in transactions:
            existing = connection.execute(
                "SELECT block_number FROM arbitrage_transactions WHERE tx_hash = ?",
                (tx_hash,),
            ).fetchone()
            if existing is not None and int(existing["block_number"]) != block_number:
                raise RuntimeError(
                    f"transaction {tx_hash} is already stored at block "
                    f"{existing['block_number']}, not {block_number}"
                )
        before = connection.total_changes
        connection.executemany(
            """
            INSERT OR IGNORE INTO arbitrage_transactions(tx_hash, block_number)
            VALUES (?, ?)
            """,
            transactions,
        )
        return connection.total_changes - before
