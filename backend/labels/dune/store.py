"""SQLite state for Dune label synchronization."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Literal

from labels.coordinator import DATA_DIR, TX_HASH_RE, encode_tx_hash

if TYPE_CHECKING:
    from labels.coordinator import LabelCoordinator


MAX_BLOCKS_PER_QUERY = 1_000
DEFAULT_LOCK_PATH = DATA_DIR / "sync.lock"
RunKind = Literal["initial", "incremental"]


@dataclass(frozen=True)
class SyncChunk:
    run_id: int
    chunk_start: int
    chunk_end: int
    attempts: int = 0


@dataclass(frozen=True)
class SyncRun:
    run_id: int
    kind: str
    start_block: int
    end_block: int
    status: str
    total_chunks: int
    completed_chunks: int


def split_block_range(
    start_block: int,
    end_block: int,
    max_blocks: int = MAX_BLOCKS_PER_QUERY,
) -> list[tuple[int, int]]:
    """Split an inclusive range into newest-first, gap-free chunks."""
    if start_block < 0 or end_block < start_block:
        raise ValueError("invalid block range")
    if max_blocks <= 0:
        raise ValueError("max_blocks must be positive")

    chunks: list[tuple[int, int]] = []
    chunk_end = end_block
    while chunk_end >= start_block:
        chunk_start = max(start_block, chunk_end - max_blocks + 1)
        chunks.append((chunk_start, chunk_end))
        chunk_end = chunk_start - 1
    return chunks


class DuneStore:
    def __init__(self, coordinator: LabelCoordinator):
        self.coordinator = coordinator

    def initialize_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sync_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK (kind IN ('initial', 'incremental')),
                start_block INTEGER NOT NULL,
                end_block INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'running', 'completed', 'failed')
                ),
                total_chunks INTEGER NOT NULL,
                completed_chunks INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sync_chunks (
                run_id INTEGER NOT NULL REFERENCES sync_runs(run_id) ON DELETE CASCADE,
                chunk_start INTEGER NOT NULL,
                chunk_end INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'running', 'completed', 'failed')
                ),
                attempts INTEGER NOT NULL DEFAULT 0,
                execution_id TEXT,
                api_slot TEXT,
                result_count INTEGER,
                last_error TEXT,
                PRIMARY KEY (run_id, chunk_start, chunk_end)
            );
            CREATE INDEX IF NOT EXISTS idx_sync_chunks_status
                ON sync_chunks(run_id, status, chunk_end DESC);
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO sync_state(key, value) VALUES (?, ?)",
            ("initial_sync_complete", "false"),
        )

    def initial_sync_complete(self) -> bool:
        self.coordinator.initialize()
        with self.coordinator._connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT value FROM sync_state WHERE key = ?",
                ("initial_sync_complete",),
            ).fetchone()
        return bool(row and row["value"] == "true")

    def create_run(self, kind: RunKind, start_block: int, end_block: int) -> SyncRun:
        self.coordinator.initialize()
        chunks = split_block_range(start_block, end_block)
        with self.coordinator._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO sync_runs(
                    kind, start_block, end_block, status, total_chunks, completed_chunks
                ) VALUES (?, ?, ?, 'pending', ?, 0)
                """,
                (kind, start_block, end_block, len(chunks)),
            )
            run_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO sync_chunks(run_id, chunk_start, chunk_end, status)
                VALUES (?, ?, ?, 'pending')
                """,
                [(run_id, chunk_start, chunk_end) for chunk_start, chunk_end in chunks],
            )
        return self.get_run(run_id)

    def get_run(self, run_id: int) -> SyncRun:
        with self.coordinator._connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM sync_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"sync run {run_id} not found")
        return SyncRun(**dict(row))

    def find_unfinished_run(self, kind: RunKind) -> SyncRun | None:
        self.coordinator.initialize()
        with self.coordinator._connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT * FROM sync_runs
                WHERE kind = ? AND status != 'completed'
                ORDER BY run_id DESC LIMIT 1
                """,
                (kind,),
            ).fetchone()
        return SyncRun(**dict(row)) if row else None

    def find_run(self, kind: RunKind, start_block: int, end_block: int) -> SyncRun | None:
        self.coordinator.initialize()
        with self.coordinator._connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT * FROM sync_runs
                WHERE kind = ? AND start_block = ? AND end_block = ?
                ORDER BY run_id DESC LIMIT 1
                """,
                (kind, start_block, end_block),
            ).fetchone()
        return SyncRun(**dict(row)) if row else None

    def resume_run(self, run_id: int) -> SyncRun:
        with self.coordinator._connect() as connection:
            connection.execute(
                """
                UPDATE sync_chunks SET status = 'pending', api_slot = NULL
                WHERE run_id = ? AND status IN ('running', 'failed')
                """,
                (run_id,),
            )
            connection.execute(
                "UPDATE sync_runs SET status = 'running' WHERE run_id = ?", (run_id,)
            )
        return self.get_run(run_id)

    def pending_chunks(self, run_id: int) -> list[SyncChunk]:
        with self.coordinator._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT run_id, chunk_start, chunk_end, attempts FROM sync_chunks
                WHERE run_id = ? AND status = 'pending'
                ORDER BY chunk_end DESC
                """,
                (run_id,),
            ).fetchall()
        return [SyncChunk(**dict(row)) for row in rows]

    def mark_chunk_running(self, chunk: SyncChunk, api_slot: str) -> None:
        with self.coordinator._connect() as connection:
            changed = connection.execute(
                """
                UPDATE sync_chunks
                SET status = 'running', attempts = attempts + 1,
                    api_slot = ?, last_error = NULL
                WHERE run_id = ? AND chunk_start = ? AND chunk_end = ?
                  AND status = 'pending'
                """,
                (api_slot, chunk.run_id, chunk.chunk_start, chunk.chunk_end),
            ).rowcount
        if changed != 1:
            raise RuntimeError(
                f"chunk [{chunk.chunk_start}, {chunk.chunk_end}] was not pending"
            )

    def mark_chunk_retry(self, chunk: SyncChunk, execution_id: str | None, error: str) -> None:
        with self.coordinator._connect() as connection:
            connection.execute(
                """
                UPDATE sync_chunks SET status = 'pending', execution_id = ?, last_error = ?
                WHERE run_id = ? AND chunk_start = ? AND chunk_end = ?
                """,
                (execution_id, error[:1000], chunk.run_id, chunk.chunk_start, chunk.chunk_end),
            )

    def mark_chunk_failed(self, chunk: SyncChunk, execution_id: str | None, error: str) -> None:
        with self.coordinator._connect() as connection:
            connection.execute(
                """
                UPDATE sync_chunks SET status = 'failed', execution_id = ?, last_error = ?
                WHERE run_id = ? AND chunk_start = ? AND chunk_end = ?
                """,
                (execution_id, error[:1000], chunk.run_id, chunk.chunk_start, chunk.chunk_end),
            )

    def complete_chunk(
        self,
        chunk: SyncChunk,
        transactions: Iterable[dict],
        execution_id: str,
        api_slot: str,
    ) -> SyncRun:
        normalized: list[tuple[int, bytes]] = []
        for transaction in transactions:
            tx_hash = str(transaction["tx_hash"]).lower()
            block_number = int(transaction["block_number"])
            if not TX_HASH_RE.fullmatch(tx_hash):
                raise ValueError(f"invalid transaction hash in Dune result: {tx_hash!r}")
            if not chunk.chunk_start <= block_number <= chunk.chunk_end:
                raise ValueError(
                    f"block {block_number} outside chunk "
                    f"[{chunk.chunk_start}, {chunk.chunk_end}]"
                )
            normalized.append((block_number, encode_tx_hash(tx_hash)))

        with self.coordinator._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """
                INSERT OR IGNORE INTO arbitrage_transactions(block_number, tx_hash)
                VALUES (?, ?)
                """,
                normalized,
            )
            changed = connection.execute(
                """
                UPDATE sync_chunks
                SET status = 'completed', execution_id = ?, api_slot = ?,
                    result_count = ?, last_error = NULL
                WHERE run_id = ? AND chunk_start = ? AND chunk_end = ?
                  AND status = 'running'
                """,
                (
                    execution_id,
                    api_slot,
                    len(normalized),
                    chunk.run_id,
                    chunk.chunk_start,
                    chunk.chunk_end,
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError("chunk completion did not update exactly one running row")
            connection.execute(
                """
                UPDATE sync_runs SET completed_chunks = (
                    SELECT COUNT(*) FROM sync_chunks
                    WHERE run_id = ? AND status = 'completed'
                ) WHERE run_id = ?
                """,
                (chunk.run_id, chunk.run_id),
            )
        return self.get_run(chunk.run_id)

    def finish_run(self, run_id: int) -> SyncRun:
        with self.coordinator._connect() as connection:
            row = connection.execute(
                """
                SELECT kind, total_chunks, completed_chunks
                FROM sync_runs WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"sync run {run_id} not found")
            if row["completed_chunks"] != row["total_chunks"]:
                connection.execute(
                    "UPDATE sync_runs SET status = 'failed' WHERE run_id = ?", (run_id,)
                )
            else:
                connection.execute(
                    "UPDATE sync_runs SET status = 'completed' WHERE run_id = ?", (run_id,)
                )
                if row["kind"] == "initial":
                    connection.execute(
                        """
                        INSERT INTO sync_state(key, value)
                        VALUES ('initial_sync_complete', 'true')
                        ON CONFLICT(key) DO UPDATE SET value = 'true'
                        """
                    )
        return self.get_run(run_id)

    def coverage_intervals(self, from_block: int, to_block: int) -> list[tuple[int, int]]:
        with self.coordinator._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT chunk_start, chunk_end FROM sync_chunks
                WHERE status = 'completed' AND chunk_end >= ? AND chunk_start <= ?
                ORDER BY chunk_start, chunk_end
                """,
                (from_block, to_block),
            ).fetchall()
        return [(int(row["chunk_start"]), int(row["chunk_end"])) for row in rows]

    def progress(self, run_id: int) -> dict:
        with self.coordinator._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count,
                       COALESCE(SUM(chunk_end - chunk_start + 1), 0) AS blocks
                FROM sync_chunks WHERE run_id = ? GROUP BY status
                """,
                (run_id,),
            ).fetchall()
        result = {
            "pending": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
            "completed_blocks": 0,
        }
        for row in rows:
            result[str(row["status"])] = int(row["count"])
            if row["status"] == "completed":
                result["completed_blocks"] = int(row["blocks"])
        return result
