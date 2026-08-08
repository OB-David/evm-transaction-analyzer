"""One-off Dune backfill for history older than the existing label index."""

from __future__ import annotations

import logging
import sys

from labels.dune.sync import (
    SyncFileLock,
    discover_api_credentials,
    run_sync,
)
from labels.coordinator import HISTORY_START_BLOCK, LabelCoordinator
from utils.block_exploration import (
    ETHEREUM_MAINNET_CHAIN_ID,
    get_chain_id,
)


# Extend the existing label index toward older history. Both ends are inclusive.
BACKFILL_START_BLOCK = HISTORY_START_BLOCK
BACKFILL_END_BLOCK = 24_136_052


def print_progress(event: dict) -> None:
    chunk = event["chunk"]
    run = event["run"]
    progress = event["progress"]
    total_blocks = run.end_block - run.start_block + 1
    completed_blocks = progress["completed_blocks"]
    percent = completed_blocks * 100 / total_blocks if total_blocks else 100.0
    message = (
        f"[{event['event']}] chunks {progress['completed']}/{run.total_chunks} "
        f"blocks {completed_blocks}/{total_blocks} ({percent:.2f}%) "
        f"range=[{chunk.chunk_start}, {chunk.chunk_end}] "
        f"api={event['api_slot']} execution={event.get('execution_id') or '-'} "
        f"rows={event.get('result_count', 0)} db_total={event['transaction_count']} "
        f"pending={progress['pending']} running={progress['running']} "
        f"failed={progress['failed']}"
    )
    if event.get("error"):
        message += f" error={event['error']}"
    print(message, flush=True)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    store = LabelCoordinator().dune
    store.coordinator.initialize()
    if not store.initial_sync_complete():
        print(
            "The current Dune history is incomplete; finish it before backfilling older blocks.",
            file=sys.stderr,
        )
        return 5

    existing_run = store.find_run("initial", BACKFILL_START_BLOCK, BACKFILL_END_BLOCK)
    if existing_run and existing_run.status == "completed":
        print(
            f"Historical backfill [{BACKFILL_START_BLOCK}, {BACKFILL_END_BLOCK}] "
            "is already complete.",
            flush=True,
        )
        return 0

    credentials = discover_api_credentials()
    if not credentials:
        print("No DUNE_API1, DUNE_API2, ... values were found in backend/.env.", file=sys.stderr)
        return 2

    lock = SyncFileLock()
    if not lock.acquire():
        print("Another backend or bootstrap process already owns sync.lock.", file=sys.stderr)
        return 3

    try:
        chain_id = get_chain_id()
        if chain_id != ETHEREUM_MAINNET_CHAIN_ID:
            print(
                f"GETH_API returned chain_id={chain_id}; Ethereum mainnet chain_id=1 is required.",
                file=sys.stderr,
                flush=True,
            )
            return 4
        run = existing_run or store.create_run(
            "initial", BACKFILL_START_BLOCK, BACKFILL_END_BLOCK
        )
        print(
            f"Historical backfill run {run.run_id}: "
            f"blocks [{run.start_block}, {run.end_block}], "
            f"chunks={run.total_chunks}, workers={','.join(item.slot for item in credentials)}",
            flush=True,
        )
        outcome = run_sync(
            store,
            run.run_id,
            credentials,
            progress_callback=print_progress,
        )
        if outcome.successful:
            print(
                f"Historical backfill run {outcome.run.run_id} completed; "
                f"stored transactions={store.coordinator.count_transactions()}.",
                flush=True,
            )
            return 0
        print(
            f"Historical backfill run {outcome.run.run_id} is incomplete. "
            "Run this script again to retry only unfinished chunks.",
            file=sys.stderr,
            flush=True,
        )
        return 1
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
