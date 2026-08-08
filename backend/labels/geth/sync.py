"""Incrementally label arbitrages from Geth call traces."""

from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from dotenv import load_dotenv

from labels.geth.detector import detect_profitable_transactions
from labels.geth.models import Trace
from labels.geth.rpc import GethRpcClient
from labels.coordinator import LabelCoordinator
from labels.geth.store import DEFAULT_GETH_LOCK_PATH, GethStore

BACKEND_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(BACKEND_ROOT / ".env")

SYNC_INTERVAL_SECONDS = int(os.getenv("GETH_ARBITRAGE_SYNC_INTERVAL", "60"))
logger = logging.getLogger(__name__)


class TraceClient(Protocol):
    def chain_id(self) -> int: ...

    def latest_block_number(self) -> int: ...

    def trace_block(self, block_number: int) -> list[Trace]: ...


@dataclass(frozen=True)
class SyncOutcome:
    start_block: int
    end_block: int
    processed_blocks: int
    inserted_transactions: int


class SyncFileLock:
    def __init__(self, path: str | Path = DEFAULT_GETH_LOCK_PATH):
        self.path = Path(path)
        self._file = None

    def acquire(self) -> bool:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._file.close()
            self._file = None
            return False
        return True

    def release(self) -> None:
        if self._file is None:
            return
        import fcntl

        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()
        self._file = None


def _build_client(rpc_url: str | None = None) -> GethRpcClient:
    endpoint = (rpc_url or os.getenv("GETH_API", "")).strip()
    if not endpoint:
        raise RuntimeError("GETH_API is required for Geth arbitrage labeling")
    return GethRpcClient(endpoint)


def sync_arbitrages(
    *,
    latest_block: int | None = None,
    store: GethStore | None = None,
    client: TraceClient | None = None,
    rpc_url: str | None = None,
    progress_callback: Callable[[int, int, int], None] | None = None,
) -> SyncOutcome:
    """Analyze every block after the durable Geth cursor through ``latest_block``.

    A block is committed only after its trace and labels are complete. Any exception
    stops this attempt, leaving that block as the first block retried next time.
    """

    sync_store = store or LabelCoordinator().geth
    sync_store.coordinator.initialize()
    state = sync_store.get_sync_state()
    if state is None:
        raise RuntimeError(
            "Geth arbitrage sync is not bootstrapped; import a complete dataset first"
        )

    trace_client = client or _build_client(rpc_url)
    chain_id = trace_client.chain_id()
    if chain_id != 1:
        raise RuntimeError(f"GETH_API returned chain id {chain_id}; Ethereum mainnet is required")

    end_block = (
        trace_client.latest_block_number()
        if latest_block is None
        else int(latest_block)
    )
    start_block = state.last_scanned_block + 1
    if end_block < start_block:
        return SyncOutcome(start_block, end_block, 0, 0)

    inserted_transactions = 0
    processed_blocks = 0
    for block_number in range(start_block, end_block + 1):
        traces = trace_client.trace_block(block_number)
        transactions = detect_profitable_transactions(traces)
        inserted_transactions += sync_store.complete_block(
            block_number,
            transactions,
        )
        processed_blocks += 1
        if progress_callback is not None:
            progress_callback(block_number, end_block, len(transactions))
        if transactions or processed_blocks % 25 == 0:
            logger.info(
                "Geth arbitrage sync block %d/%d: %d label(s)",
                block_number,
                end_block,
                len(transactions),
            )

    return SyncOutcome(
        start_block=start_block,
        end_block=end_block,
        processed_blocks=processed_blocks,
        inserted_transactions=inserted_transactions,
    )


def start_background_sync(
    latest_block_provider: Callable[[], int] | None = None,
    *,
    store: GethStore | None = None,
) -> threading.Thread:
    """Start one startup-then-periodic Geth incremental labeler."""

    sync_store = store or LabelCoordinator().geth

    def loop() -> None:
        sync_store.coordinator.initialize()
        if sync_store.get_sync_state() is None:
            logger.warning(
                "Geth arbitrage sync is disabled until a complete dataset is imported"
            )
            return
        lock = SyncFileLock()
        if not lock.acquire():
            logger.info(
                "Another process owns the Geth arbitrage sync lock; "
                "this process only serves reads"
            )
            return
        logger.info("Geth arbitrage sync coordinator started")
        try:
            while True:
                try:
                    latest = (
                        latest_block_provider()
                        if latest_block_provider is not None
                        else None
                    )
                    outcome = sync_arbitrages(
                        latest_block=latest,
                        store=sync_store,
                    )
                    if outcome.processed_blocks:
                        logger.info(
                            "Geth arbitrage sync completed %d block(s), inserted %d label(s)",
                            outcome.processed_blocks,
                            outcome.inserted_transactions,
                        )
                except Exception:
                    logger.exception("Geth arbitrage synchronization stopped at its first failed block")
                time.sleep(SYNC_INTERVAL_SECONDS)
        finally:
            lock.release()

    thread = threading.Thread(target=loop, name="geth-arbitrage-sync", daemon=True)
    thread.start()
    return thread


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--to-block", type=int)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    lock = SyncFileLock()
    if not lock.acquire():
        raise SystemExit("another process owns the Geth arbitrage sync lock")
    try:
        outcome = sync_arbitrages(latest_block=args.to_block)
    finally:
        lock.release()
    print(
        f"processed {outcome.processed_blocks} blocks "
        f"({outcome.start_block}-{outcome.end_block}); "
        f"inserted {outcome.inserted_transactions} txs"
    )


if __name__ == "__main__":
    main()
