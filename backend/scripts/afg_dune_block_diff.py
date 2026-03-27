"""Compare block-level arbitrage detection results between AFG and Dune."""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from web3 import Web3

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_ROOT = os.path.dirname(CURRENT_DIR)
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

from utils.arbitrage_crawler import fetch_arbitrage_hashes, get_cached_hashes  # noqa: E402
from utils.arbitrage_light import analyze_tx_arbitrage_light  # noqa: E402


def _to_hex_hash(value: Any) -> str:
    if hasattr(value, "hex"):
        hex_value = value.hex()
        return hex_value if hex_value.startswith("0x") else f"0x{hex_value}"
    as_str = str(value)
    return as_str if as_str.startswith("0x") else f"0x{as_str}"


def _extract_block_tx_hashes(block: Any) -> list[str]:
    txs = block.get("transactions", [])
    tx_hashes: list[str] = []
    for tx in txs:
        if hasattr(tx, "get"):
            tx_hash = tx.get("hash")
        else:
            tx_hash = getattr(tx, "hash", tx)
        tx_hashes.append(_to_hex_hash(tx_hash).lower())
    return tx_hashes


def _build_dune_detected_set(dune_rows: list[dict[str, Any]], block_number: int) -> set[str]:
    hashes: set[str] = set()
    for row in dune_rows:
        row_block = row.get("block_number")
        tx_hash = row.get("tx_hash")
        if row_block != block_number or not isinstance(tx_hash, str):
            continue
        hashes.add(tx_hash.lower())
    return hashes


def _refresh_dune_or_raise() -> dict[str, Any]:
    if not os.getenv("DUNE_API"):
        raise RuntimeError("DUNE_API is not set")

    before = get_cached_hashes().get("fetched_at")
    fetch_arbitrage_hashes()
    cache = get_cached_hashes()
    after = cache.get("fetched_at")

    if not after or after == before:
        raise RuntimeError("Dune refresh did not complete; fetched_at did not update")

    return cache


def _analyze_block_transactions(
    tx_hashes: list[str], provider_url: str, workers: int, fail_fast: bool
) -> list[dict[str, Any]]:
    detailed_results: list[dict[str, Any]] = []
    index_by_hash = {h: i for i, h in enumerate(tx_hashes)}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(analyze_tx_arbitrage_light, tx_hash, provider_url): tx_hash
            for tx_hash in tx_hashes
        }

        for future in as_completed(future_map):
            tx_hash = future_map[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "tx_hash": tx_hash,
                    "is_arbitrage": False,
                    "status": "failed",
                    "reason": "worker_exception",
                    "error": str(exc),
                }

            detailed_results.append(result)
            if fail_fast and result.get("status") == "failed":
                for pending in future_map:
                    if not pending.done():
                        pending.cancel()
                raise RuntimeError(
                    f"Fail-fast enabled: tx {result.get('tx_hash')} failed ({result.get('error')})"
                )

    detailed_results.sort(key=lambda item: index_by_hash.get(item.get("tx_hash", ""), 10**18))
    return detailed_results


def run(block_number: int, output: str, workers: int, fail_fast: bool) -> dict[str, Any]:
    provider_url = os.getenv("GETH_API", "")
    if not provider_url:
        raise RuntimeError("GETH_API is not set")

    web3 = Web3(Web3.HTTPProvider(provider_url))
    if not web3.is_connected():
        raise RuntimeError(f"Cannot connect to GETH_API endpoint: {provider_url}")

    dune_cache = _refresh_dune_or_raise()
    dune_rows = dune_cache.get("transactions", [])

    block = web3.eth.get_block(block_number, full_transactions=True)
    tx_hashes = _extract_block_tx_hashes(block)
    detailed_results = _analyze_block_transactions(tx_hashes, provider_url, workers, fail_fast)

    afg_detected_set = {
        item["tx_hash"]
        for item in detailed_results
        if item.get("status") == "analyzed" and bool(item.get("is_arbitrage"))
    }
    dune_detected_set = _build_dune_detected_set(dune_rows, block_number)
    afg_only_set = sorted(afg_detected_set - dune_detected_set)

    analyzed = sum(1 for item in detailed_results if item.get("status") == "analyzed")
    failed = sum(1 for item in detailed_results if item.get("status") == "failed")
    skipped = sum(1 for item in detailed_results if item.get("status") == "skipped")
    arbitrage_detected = sum(
        1
        for item in detailed_results
        if item.get("status") == "analyzed" and bool(item.get("is_arbitrage"))
    )

    payload = {
        "block_number": block_number,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dune": {
            "query_id": dune_cache.get("query_id"),
            "fetched_at": dune_cache.get("fetched_at"),
            "total_rows": len(dune_rows),
            "unique_in_block": len(dune_detected_set),
        },
        "afg": {
            "total_transactions": len(tx_hashes),
            "analyzed": analyzed,
            "arbitrage_detected": arbitrage_detected,
            "failed": failed,
            "skipped": skipped,
        },
        "afg_only_transactions": [
            {"tx_hash": tx_hash, "reason": "detected_by_afg_not_in_dune"} for tx_hash in afg_only_set
        ],
        "detailed_results": detailed_results,
    }

    output_dir = os.path.dirname(output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare AFG arbitrage detection vs Dune for all transactions in the same block."
        )
    )
    parser.add_argument("--block-number", type=int, required=True, help="Target block number")
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output JSON path (default: backend/Result/block_<num>_afg_minus_dune.json)",
    )
    parser.add_argument("--workers", type=int, default=4, help="Thread workers for AFG analysis")
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop immediately when any transaction analysis fails",
    )
    return parser


def main() -> int:
    load_dotenv(os.path.join(BACKEND_ROOT, ".env"))
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()

    block_number: int = args.block_number
    workers = max(1, int(args.workers))
    output = args.output or os.path.join(
        BACKEND_ROOT, "Result", f"block_{block_number}_afg_minus_dune.json"
    )

    try:
        payload = run(
            block_number=block_number,
            output=output,
            workers=workers,
            fail_fast=bool(args.fail_fast),
        )
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    print(
        "Done. "
        f"block={payload['block_number']} total={payload['afg']['total_transactions']} "
        f"analyzed={payload['afg']['analyzed']} skipped={payload['afg']['skipped']} "
        f"failed={payload['afg']['failed']} afg_only={len(payload['afg_only_transactions'])}"
    )
    print(f"Output: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
