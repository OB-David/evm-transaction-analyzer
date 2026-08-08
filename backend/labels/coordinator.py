"""Coordinate the shared label database and provider-specific stores."""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING


HISTORY_START_BLOCK = 22_630_960
MAX_API_BLOCK_RANGE = 5_000

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPOSITORY_ROOT / "data_base" / "arbitrage_txs"
DEFAULT_DB_PATH = DATA_DIR / "arbitrage.sqlite3"
TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

if TYPE_CHECKING:
    from labels.dune.store import DuneStore
    from labels.geth.store import GethStore


class LabelCoordinator:
    """Own the shared connection and coordinate Dune/Geth label coverage."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        from labels.dune.store import DuneStore
        from labels.geth.store import GethStore

        self.db_path = Path(db_path).resolve()
        self._initialized = False
        self._initialization_lock = threading.Lock()
        self.dune: DuneStore = DuneStore(self)
        self.geth: GethStore = GethStore(self)

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(
                f"file:{self.db_path.as_posix()}?mode=ro",
                uri=True,
                timeout=5,
            )
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        if not read_only:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialization_lock:
            if self._initialized:
                return
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS arbitrage_transactions (
                        tx_hash TEXT PRIMARY KEY,
                        block_number INTEGER NOT NULL CHECK (block_number >= 0)
                    );
                    CREATE INDEX IF NOT EXISTS idx_arbitrage_block_number
                        ON arbitrage_transactions(block_number);
                    """
                )
                self.dune.initialize_schema(connection)
                self.geth.initialize_schema(connection)
            self._initialized = True

    def count_transactions(self) -> int:
        self.initialize()
        with self._connect(read_only=True) as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM arbitrage_transactions"
                ).fetchone()[0]
            )

    def max_arbitrage_block(self) -> int | None:
        self.initialize()
        with self._connect(read_only=True) as connection:
            value = connection.execute(
                "SELECT MAX(block_number) FROM arbitrage_transactions"
            ).fetchone()[0]
        return int(value) if value is not None else None

    def query_transactions(self, from_block: int, to_block: int) -> list[dict]:
        self.initialize()
        with self._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT tx_hash, block_number
                FROM arbitrage_transactions
                WHERE block_number BETWEEN ? AND ?
                ORDER BY block_number DESC, tx_hash DESC
                """,
                (from_block, to_block),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_transactions(self, limit: int = 500) -> list[dict]:
        self.initialize()
        with self._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT tx_hash, block_number FROM arbitrage_transactions
                ORDER BY block_number DESC, tx_hash DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def coverage_complete(self, from_block: int, to_block: int) -> bool:
        if from_block < HISTORY_START_BLOCK or to_block < from_block:
            return False
        self.initialize()
        intervals = [
            *self.dune.coverage_intervals(from_block, to_block),
            *self.geth.coverage_intervals(from_block, to_block),
        ]
        intervals.sort()

        next_uncovered = from_block
        for interval_start, interval_end in intervals:
            if interval_end < next_uncovered:
                continue
            if interval_start > next_uncovered:
                return False
            next_uncovered = max(next_uncovered, interval_end + 1)
            if next_uncovered > to_block:
                return True
        return False
