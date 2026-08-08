"""Canonical filesystem paths for transaction-analysis artifacts."""

from __future__ import annotations

import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ANALYSIS_ROOT = (REPOSITORY_ROOT / "data_base" / "analysis").resolve()
TX_HASH_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")


def analysis_directory(tx_hash: str) -> Path:
    """Return the artifact directory for one transaction hash."""
    if not TX_HASH_RE.fullmatch(tx_hash):
        raise ValueError("transaction hash must contain exactly 64 hexadecimal digits")
    normalized_hash = tx_hash.lower()
    if normalized_hash.startswith("0x"):
        normalized_hash = normalized_hash[2:]
    return ANALYSIS_ROOT / normalized_hash
