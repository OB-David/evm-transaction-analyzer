"""SQLite storage and runtime lookup for EVM function signatures.

The full signature remains available for future parameter-type analysis, while
the rendering hot path reads only the precomputed function name. Network access
is intentionally confined to ``signature/sync.py``.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SIGNATURE_DB_PATH = (
    REPOSITORY_ROOT / "data_base" / "signatures" / "function_signatures.sqlite3"
)
SCHEMA_VERSION = "2"
SELECTOR_PATTERN = re.compile(r"^0x[0-9a-f]{8}$")
FUNCTION_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

PRIORITY_FUNCTION_NAMES = [
    "transfer",
    "transferFrom",
    "approve",
    "safeTransfer",
    "safeTransferFrom",
    "swapExactTokensForTokens",
    "swapTokensForExactTokens",
    "swapExactETHForTokens",
    "swapTokensForExactETH",
    "swapExactTokensForETH",
    "swap",
    "exactInputSingle",
    "exactOutputSingle",
    "multicall",
    "flashLoan",
    "flashSwap",
    "executeOperation",
    "flashLoanSimple",
    "getReserves",
    "getAmountOut",
    "getAmountIn",
    "balanceOf",
    "decimals",
    "factory",
    "pairFor",
    "getPool",
    "deposit",
    "withdraw",
    "safeApprove",
    "pull",
    "call",
]
PRIORITY_RANK_BY_NAME = {
    function_name: rank for rank, function_name in enumerate(PRIORITY_FUNCTION_NAMES)
}
PRIORITY_SIGNATURES = [f"{name}()" for name in PRIORITY_FUNCTION_NAMES]
PRIORITY_SIGNATURE_RANK = {
    signature: rank for rank, signature in enumerate(PRIORITY_SIGNATURES)
}


@dataclass(frozen=True)
class FunctionSignatureRecord:
    api_id: int
    selector: str
    text_signature: str
    function_name: str | None
    priority_rank: int | None


def extract_function_name(text_signature: Any) -> str | None:
    """Return the Solidity function name without parameters or parentheses."""
    if not isinstance(text_signature, str):
        return None
    cleaned = text_signature.strip()
    left_paren_idx = cleaned.find("(")
    if left_paren_idx <= 0:
        return None
    function_name = cleaned[:left_paren_idx].strip()
    if not FUNCTION_NAME_PATTERN.fullmatch(function_name):
        return None
    return function_name


def initialize_database(connection: sqlite3.Connection) -> None:
    """Create the compact schema used by synchronization and runtime lookup."""
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS function_signatures (
            api_id INTEGER PRIMARY KEY,
            selector TEXT NOT NULL,
            text_signature TEXT NOT NULL,
            function_name TEXT,
            priority_rank INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_function_signatures_selector
            ON function_signatures(selector);

        CREATE TABLE IF NOT EXISTS sync_pages (
            page_number INTEGER PRIMARY KEY,
            row_count INTEGER NOT NULL,
            fetched_at TEXT NOT NULL
        );
        """
    )
    set_metadata(connection, "schema_version", SCHEMA_VERSION)
    connection.commit()


def set_metadata(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        """
        INSERT INTO metadata(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )


def get_metadata(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): str(row[1])
        for row in connection.execute("SELECT key, value FROM metadata")
    }


def upsert_api_records(
    connection: sqlite3.Connection,
    records: Iterable[Mapping[str, Any]],
) -> int:
    """Store every API row while dropping source fields unused by this project."""
    prepared: list[tuple[int, str, str, str | None, int | None]] = []
    for record in records:
        api_id = int(record["id"])
        selector = str(record.get("hex_signature") or "").strip().lower()
        text_signature = str(record.get("text_signature") or "").strip()
        function_name = extract_function_name(text_signature)
        prepared.append(
            (
                api_id,
                selector,
                text_signature,
                function_name,
                PRIORITY_RANK_BY_NAME.get(function_name) if function_name else None,
            )
        )

    connection.executemany(
        """
        INSERT INTO function_signatures(
            api_id, selector, text_signature, function_name, priority_rank
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(api_id) DO UPDATE SET
            selector = excluded.selector,
            text_signature = excluded.text_signature,
            function_name = excluded.function_name,
            priority_rank = excluded.priority_rank
        """,
        prepared,
    )
    return len(prepared)


_warning_lock = threading.Lock()
_warned_database_messages: set[str] = set()


def _warn_once(message: str) -> None:
    with _warning_lock:
        if message in _warned_database_messages:
            return
        _warned_database_messages.add(message)
    print(f"WARNING: {message}")


class FunctionSignatureStore:
    """A per-render read-only connection to a verified complete signature DB."""

    def __init__(self, path: str | Path = DEFAULT_SIGNATURE_DB_PATH):
        self.path = Path(path).expanduser().resolve()
        self.connection: sqlite3.Connection | None = None
        self.metadata: dict[str, str] = {}

    @property
    def available(self) -> bool:
        return self.connection is not None

    def open(self) -> "FunctionSignatureStore":
        if self.connection is not None:
            return self
        if not self.path.is_file():
            _warn_once(
                f"函数签名库不存在，时序图将只显示 selector：{self.path}。"
                "请先运行 backend/signature/sync.py。"
            )
            return self

        try:
            connection = sqlite3.connect(
                f"file:{self.path.as_posix()}?mode=ro",
                uri=True,
                timeout=5,
            )
            connection.row_factory = sqlite3.Row
            metadata = get_metadata(connection)
            if metadata.get("schema_version") != SCHEMA_VERSION:
                raise RuntimeError(
                    f"schema version {metadata.get('schema_version')!r} is not {SCHEMA_VERSION!r}"
                )
            if metadata.get("sync_complete") != "true":
                raise RuntimeError("database is not marked as a complete snapshot")
        except Exception as exc:
            try:
                connection.close()
            except UnboundLocalError:
                pass
            _warn_once(f"函数签名库不可用，时序图将只显示 selector：{self.path} ({exc})")
            return self

        self.connection = connection
        self.metadata = metadata
        return self

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self) -> "FunctionSignatureStore":
        return self.open()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def lookup(self, selector: str) -> list[FunctionSignatureRecord]:
        normalized = selector.strip().lower()
        if not SELECTOR_PATTERN.fullmatch(normalized) or self.connection is None:
            return []

        rows = self.connection.execute(
            """
            SELECT api_id, selector, text_signature, function_name, priority_rank
            FROM function_signatures
            WHERE selector = ?
            ORDER BY
                CASE WHEN priority_rank IS NULL THEN 1 ELSE 0 END,
                priority_rank,
                api_id,
                function_name,
                text_signature
            """,
            (normalized,),
        )
        return [
            FunctionSignatureRecord(
                api_id=int(row["api_id"]),
                selector=str(row["selector"]),
                text_signature=str(row["text_signature"]),
                function_name=str(row["function_name"]) if row["function_name"] else None,
                priority_rank=int(row["priority_rank"])
                if row["priority_rank"] is not None
                else None,
            )
            for row in rows
        ]

    def lookup_display_names(self, selector: str) -> list[str]:
        """Return unique ``name()`` labels without loading full signatures."""
        normalized = selector.strip().lower()
        if not SELECTOR_PATTERN.fullmatch(normalized) or self.connection is None:
            return []

        rows = self.connection.execute(
            """
            SELECT function_name
            FROM function_signatures
            WHERE selector = ? AND function_name IS NOT NULL
            ORDER BY
                CASE WHEN priority_rank IS NULL THEN 1 ELSE 0 END,
                priority_rank,
                api_id,
                function_name
            """,
            (normalized,),
        )
        names: list[str] = []
        seen: set[str] = set()
        for row in rows:
            display_name = f"{row['function_name']}()"
            if display_name not in seen:
                seen.add(display_name)
                names.append(display_name)
        return names
