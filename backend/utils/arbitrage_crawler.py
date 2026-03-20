"""Arbitrage transaction crawler using Dune Analytics API."""
import os
import time
import logging
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

DUNE_API_KEY = os.getenv("DUNE_API", "")
DUNE_QUERY_ID = 6789800
_BASE = "https://api.dune.com/api/v1"
_POLL_INTERVAL = 1   # seconds between status polls
_POLL_TIMEOUT  = 60 # max seconds to wait for execution

logger = logging.getLogger(__name__)

# cache dict for latest results
_cache: dict = {
    "transactions": [],  # list of {"tx_hash": str, "block_number": int | None}
    "fetched_at": None,
    "source": "dune",
    "query_id": DUNE_QUERY_ID,
}

def _headers() -> dict:
    """Just for readability."""
    return {"x-dune-api-key": DUNE_API_KEY}


def _execute_query() -> str:
    """Trigger a fresh execution of the Dune query. Returns execution_id."""
    resp = requests.post(f"{_BASE}/query/{DUNE_QUERY_ID}/execute",
                         headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()["execution_id"]


def _poll_until_complete(execution_id: str) -> None:
    """Block until the execution finishes or the timeout is reached."""
    url = f"{_BASE}/execution/{execution_id}/status"
    deadline = time.time() + _POLL_TIMEOUT
    while time.time() < deadline:
        resp = requests.get(url, headers=_headers(), timeout=30)
        resp.raise_for_status()
        state = resp.json().get("state", "")
        if state == "QUERY_STATE_COMPLETED":
            return
        if state == "QUERY_STATE_FAILED":
            raise RuntimeError(f"Dune execution failed: {resp.json()}")
        time.sleep(_POLL_INTERVAL)
    raise TimeoutError(
        f"Dune query {DUNE_QUERY_ID} did not complete within {_POLL_TIMEOUT}s"
    )


def _extract_transactions(execution_id: str) -> list[dict]:
    """Fetch result rows and pull out tx hash and block number."""
    resp = requests.get(f"{_BASE}/execution/{execution_id}/results",
                        headers=_headers(), timeout=30)
    resp.raise_for_status()
    rows = resp.json().get("result", {}).get("rows", [])
    transactions = []
    for row in rows:
        tx = row.get("tx_hash") or row.get("transaction_hash") or row.get("hash")
        if not (tx and isinstance(tx, str) and tx.startswith("0x")):
            continue
        block = row.get("block_number") or row.get("block_num") or row.get("block")
        transactions.append({
            "tx_hash": tx,
            "block_number": int(block) if block is not None else None,
        })
    return transactions


def fetch_arbitrage_hashes() -> list[dict]:
    """Run a fresh Dune query, wait for results, update the in-memory cache."""
    if not DUNE_API_KEY:
        logger.warning("DUNE_API key not set — skipping arbitrage crawl")
        return []

    try:
        logger.info("Executing Dune query %s …", DUNE_QUERY_ID)
        execution_id = _execute_query()

        logger.info("Execution %s started, polling …", execution_id)
        _poll_until_complete(execution_id)

        transactions = _extract_transactions(execution_id)
        _cache["transactions"] = transactions
        _cache["fetched_at"] = datetime.now(timezone.utc).isoformat()
        logger.info("Cached %d arbitrage transactions from Dune", len(transactions))
        return transactions
    except TimeoutError as e:
        logger.error("Dune query timed out: %s", e)
    except RuntimeError as e:
        logger.error("Dune query failed: %s", e)
    except Exception as e:
        logger.error("Unexpected error fetching arbitrage hashes: %s", e)
    return []


def get_cached_hashes() -> dict:
    """Return the last-fetched result (hashes + metadata)."""
    return dict(_cache)
