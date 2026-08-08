"""Import a finalized arbitrage dataset into the shared label database."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from labels.coordinator import DEFAULT_DB_PATH, TX_HASH_RE, LabelCoordinator


def load_dataset(dataset_dir: Path) -> tuple[int, int, list[dict]]:
    manifest_path = dataset_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    start_block = int(manifest["start_block"])
    end_block = int(manifest["end_block"])
    expected_block_count = end_block - start_block + 1
    if manifest.get("range_semantics") != "inclusive":
        raise ValueError("dataset manifest must use inclusive range semantics")
    if int(manifest["block_count"]) != expected_block_count:
        raise ValueError("dataset manifest block count does not match its range")

    block_dir = dataset_dir / "blocks"
    block_numbers: set[int] = set()
    for path in block_dir.glob("*.json"):
        try:
            block_numbers.add(int(path.stem))
        except ValueError as exc:
            raise ValueError(f"unexpected block filename: {path.name}") from exc
    expected_blocks = set(range(start_block, end_block + 1))
    if block_numbers != expected_blocks:
        missing = sorted(expected_blocks - block_numbers)
        extra = sorted(block_numbers - expected_blocks)
        raise ValueError(
            "dataset per-block coverage is incomplete: "
            f"missing={missing[:3]}, extra={extra[:3]}"
        )

    artifact_name = manifest["artifacts"]["successful_profitable_jsonl"]
    transactions: list[dict] = []
    with (dataset_dir / artifact_name).open(encoding="utf-8") as input_file:
        for line_number, line in enumerate(input_file, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            tx_hash = str(record["transaction_hash"]).lower()
            block_number = int(record["block_number"])
            if not TX_HASH_RE.fullmatch(tx_hash):
                raise ValueError(f"invalid tx hash on JSONL line {line_number}")
            if record.get("error") is not None or int(record["profit_amount"]) <= 0:
                raise ValueError(f"non-strict arbitrage on JSONL line {line_number}")
            if not start_block <= block_number <= end_block:
                raise ValueError(f"out-of-range block on JSONL line {line_number}")
            transactions.append(
                {"tx_hash": tx_hash, "block_number": block_number}
            )
    return start_block, end_block, transactions


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset_dir", type=Path)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    start_block, end_block, transactions = load_dataset(args.dataset_dir.resolve())
    unique_transactions = {
        (item["tx_hash"], item["block_number"]) for item in transactions
    }
    if args.dry_run:
        print(
            f"validated blocks {start_block}-{end_block}; "
            f"{len(transactions)} path labels, {len(unique_transactions)} unique txs"
        )
        return

    store = LabelCoordinator(args.db).geth
    inserted = store.import_transactions(
        transactions,
        coverage_start_block=start_block,
        last_scanned_block=end_block,
    )
    print(
        f"imported {inserted} new txs; Geth coverage is "
        f"{start_block}-{end_block}"
    )


if __name__ == "__main__":
    main()
