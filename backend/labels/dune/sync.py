"""Parallel, block-chunked Dune synchronization tools."""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv

from labels.coordinator import HISTORY_START_BLOCK, LabelCoordinator, TX_HASH_RE
from labels.dune.store import (
    DEFAULT_LOCK_PATH,
    DuneStore,
    MAX_BLOCKS_PER_QUERY,
    SyncChunk,
    SyncRun,
)


load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DUNE_QUERY_ID = int(os.getenv("DUNE_QUERY_ID", "8200562"))
DUNE_API_BASE = "https://api.dune.com/api/v1"
POLL_INTERVAL_SECONDS = float(os.getenv("DUNE_POLL_INTERVAL", "2"))
POLL_TIMEOUT_SECONDS = float(os.getenv("DUNE_POLL_TIMEOUT", "600"))
MAX_ATTEMPTS_PER_RUN = int(os.getenv("DUNE_CHUNK_MAX_ATTEMPTS", "3"))
SYNC_INTERVAL_SECONDS = int(os.getenv("ARBITRAGE_SYNC_INTERVAL", "3600"))

logger = logging.getLogger(__name__)
API_SLOT_RE = re.compile(r"^DUNE_API(\d+)$")


class DuneError(RuntimeError):
    pass


class DuneAuthError(DuneError):

    pass


class DuneTransientError(DuneError):

    pass


@dataclass(frozen=True)
class ApiCredential:
    slot: str
    key: str


@dataclass(frozen=True)
class ChunkResult:
    chunk: SyncChunk
    api_slot: str
    transactions: list[dict]
    execution_id: str | None
    error: str | None = None
    auth_error: bool = False

    @property
    def success(self) -> bool:
        return self.error is None and self.execution_id is not None


@dataclass(frozen=True)
class SyncOutcome:
    run: SyncRun
    successful: bool
    completed_this_attempt: int
    failed_this_attempt: int


def discover_api_credentials(environ: dict[str, str] | None = None) -> list[ApiCredential]:
    """Discover numbered keys, ignoring empty and duplicate secret values."""
    source = os.environ if environ is None else environ
    numbered: list[tuple[int, str, str]] = []
    for name, value in source.items():
        match = API_SLOT_RE.fullmatch(name)
        if match and value.strip():
            numbered.append((int(match.group(1)), name, value.strip()))
    numbered.sort(key=lambda item: item[0])

    credentials: list[ApiCredential] = []
    seen_keys: set[str] = set()
    for _, slot, key in numbered:
        if key in seen_keys:
            logger.warning("Ignoring duplicate Dune credential in %s", slot)
            continue
        seen_keys.add(key)
        credentials.append(ApiCredential(slot=slot, key=key))

    # Backward compatibility for deployments which still only define DUNE_API.
    legacy_key = source.get("DUNE_API", "").strip()
    if not credentials and legacy_key:
        credentials.append(ApiCredential(slot="DUNE_API", key=legacy_key))

    raw_limit = source.get("DUNE_MAX_WORKERS", "").strip()
    if raw_limit:
        try:
            limit = max(1, int(raw_limit))
        except ValueError:
            logger.warning("Ignoring invalid DUNE_MAX_WORKERS=%r", raw_limit)
        else:
            credentials = credentials[:limit]
    return credentials


class DuneClient:
    """One client belongs to one worker and therefore one API key."""

    def __init__(
        self,
        credential: ApiCredential,
        *,
        query_id: int = DUNE_QUERY_ID,
        session: requests.Session | None = None,
    ):
        self.credential = credential
        self.query_id = query_id
        self.session = session or requests.Session()

    @property
    def headers(self) -> dict[str, str]:
        return {"x-dune-api-key": self.credential.key}

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        backoff = 2.0
        for rate_attempt in range(4):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=self.headers,
                    timeout=30,
                    **kwargs,
                )
            except requests.RequestException as exc:
                raise DuneTransientError(f"Dune network request failed: {exc}") from exc

            if response.status_code in (401, 403):
                raise DuneAuthError(
                    f"Dune rejected credential {self.credential.slot} "
                    f"with HTTP {response.status_code}"
                )
            if response.status_code == 429:
                if rate_attempt == 3:
                    raise DuneTransientError(
                        f"Dune rate limit persisted for {self.credential.slot}"
                    )
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = min(60.0, max(backoff, float(retry_after or 0)))
                except ValueError:
                    delay = backoff
                logger.warning(
                    "%s rate limited; only that worker sleeps for %.1fs",
                    self.credential.slot,
                    delay,
                )
                time.sleep(delay)
                backoff *= 2
                continue
            if response.status_code >= 500:
                raise DuneTransientError(f"Dune returned HTTP {response.status_code}")
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                raise DuneError(f"Dune returned HTTP {response.status_code}") from exc
            return response
        raise DuneTransientError("Dune request exhausted retries")

    def execute(self, chunk: SyncChunk) -> str:
        block_count = chunk.chunk_end - chunk.chunk_start + 1
        if block_count <= 0 or block_count > MAX_BLOCKS_PER_QUERY:
            raise ValueError(
                f"Dune chunk must contain 1..{MAX_BLOCKS_PER_QUERY} blocks, got {block_count}"
            )
        response = self._request(
            "POST",
            f"{DUNE_API_BASE}/query/{self.query_id}/execute",
            json={
                "query_parameters": {
                    "StartBlock": chunk.chunk_start,
                    "EndBlock": chunk.chunk_end,
                }
            },
        )
        execution_id = response.json().get("execution_id")
        if not execution_id:
            raise DuneError("Dune execute response omitted execution_id")
        return str(execution_id)

    def poll(self, execution_id: str) -> None:
        deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        last_state = ""
        while time.monotonic() < deadline:
            response = self._request(
                "GET", f"{DUNE_API_BASE}/execution/{execution_id}/status"
            )
            state = str(response.json().get("state", ""))
            if state != last_state:
                logger.info(
                    "%s execution %s: %s",
                    self.credential.slot,
                    execution_id,
                    state or "unknown",
                )
                last_state = state
            if state == "QUERY_STATE_COMPLETED":
                return
            if state in {
                "QUERY_STATE_FAILED",
                "QUERY_STATE_CANCELLED",
                "QUERY_STATE_EXPIRED",
            }:
                raise DuneError(f"Dune execution ended in {state}")
            time.sleep(POLL_INTERVAL_SECONDS)
        raise DuneTransientError(
            f"Dune execution {execution_id} exceeded {POLL_TIMEOUT_SECONDS:g}s"
        )

    def results(self, execution_id: str) -> list[dict]:
        url = f"{DUNE_API_BASE}/execution/{execution_id}/results?limit=1000&offset=0"
        rows: list[dict] = []
        visited: set[str] = set()

        while url:
            if url in visited:
                raise DuneError("Dune results pagination repeated the same URL")
            visited.add(url)
            response = self._request("GET", url)
            payload = response.json()
            result = payload.get("result") or {}
            page_rows = result.get("rows") or []
            if not isinstance(page_rows, list):
                raise DuneError("Dune results rows were not a list")
            rows.extend(page_rows)

            next_uri = payload.get("next_uri") or result.get("next_uri")
            if next_uri:
                next_url = urljoin(f"{DUNE_API_BASE}/", str(next_uri))
                if not next_url.startswith(f"{DUNE_API_BASE}/"):
                    raise DuneError("Dune returned an unexpected pagination URL")
                url = next_url
                continue

            next_offset = payload.get("next_offset")
            if next_offset is None:
                next_offset = result.get("next_offset")
            if next_offset is None:
                metadata = result.get("metadata") or {}
                next_offset = metadata.get("next_offset")
            url = (
                f"{DUNE_API_BASE}/execution/{execution_id}/results"
                f"?limit=1000&offset={int(next_offset)}"
                if next_offset is not None
                else ""
            )

        transactions: list[dict] = []
        for row in rows:
            tx_hash = row.get("tx_hash") or row.get("transaction_hash") or row.get("hash")
            block_number = row.get("block_number")
            if block_number is None:
                block_number = row.get("block_num")
            if block_number is None:
                block_number = row.get("block")
            if not isinstance(tx_hash, str) or not TX_HASH_RE.fullmatch(tx_hash):
                raise DuneError("Dune result contained an invalid transaction hash")
            try:
                parsed_block = int(block_number)
            except (TypeError, ValueError) as exc:
                raise DuneError("Dune result contained an invalid block number") from exc
            transactions.append(
                {"tx_hash": tx_hash.lower(), "block_number": parsed_block}
            )
        return transactions

    def query_chunk(self, chunk: SyncChunk) -> tuple[str, list[dict]]:
        execution_id = self.execute(chunk)
        logger.info(
            "%s started execution %s for blocks [%d, %d]",
            self.credential.slot,
            execution_id,
            chunk.chunk_start,
            chunk.chunk_end,
        )
        self.poll(execution_id)
        return execution_id, self.results(execution_id)


def _worker_loop(
    credential: ApiCredential,
    task_queue: queue.Queue[SyncChunk | None],
    result_queue: queue.Queue[ChunkResult],
    client_factory: Callable[[ApiCredential], DuneClient],
) -> None:
    client = client_factory(credential)
    while True:
        chunk = task_queue.get()
        if chunk is None:
            return
        execution_id: str | None = None
        try:
            execution_id, transactions = client.query_chunk(chunk)
            result = ChunkResult(
                chunk=chunk,
                api_slot=credential.slot,
                transactions=transactions,
                execution_id=execution_id,
            )
        except DuneAuthError as exc:
            result = ChunkResult(
                chunk=chunk,
                api_slot=credential.slot,
                transactions=[],
                execution_id=execution_id,
                error=str(exc),
                auth_error=True,
            )
        except Exception as exc:
            result = ChunkResult(
                chunk=chunk,
                api_slot=credential.slot,
                transactions=[],
                execution_id=execution_id,
                error=str(exc),
            )
        result_queue.put(result)


def run_sync(
    store: DuneStore,
    run_id: int,
    credentials: list[ApiCredential] | None = None,
    *,
    client_factory: Callable[[ApiCredential], DuneClient] = DuneClient,
    progress_callback: Callable[[dict], None] | None = None,
) -> SyncOutcome:
    """Run Dune calls concurrently while this coordinator alone writes SQLite."""
    selected_credentials = credentials or discover_api_credentials()
    if not selected_credentials:
        raise RuntimeError("No DUNE_API1, DUNE_API2, ... credential is configured")

    run = store.resume_run(run_id)
    pending = deque(store.pending_chunks(run_id))
    result_queue: queue.Queue[ChunkResult] = queue.Queue(
        maxsize=max(1, len(selected_credentials) * 2)
    )
    worker_queues: dict[str, queue.Queue[SyncChunk | None]] = {}
    worker_threads: list[threading.Thread] = []
    in_flight: dict[str, SyncChunk] = {}
    disabled: set[str] = set()
    attempt_counts: defaultdict[tuple[int, int], int] = defaultdict(int)
    failed_chunks = 0
    completed_chunks = 0

    for credential in selected_credentials:
        task_queue: queue.Queue[SyncChunk | None] = queue.Queue(maxsize=1)
        worker_queues[credential.slot] = task_queue
        thread = threading.Thread(
            target=_worker_loop,
            args=(credential, task_queue, result_queue, client_factory),
            name=f"dune-{credential.slot}",
            daemon=True,
        )
        thread.start()
        worker_threads.append(thread)

    try:
        while pending or in_flight:
            for credential in selected_credentials:
                slot = credential.slot
                if not pending:
                    break
                if slot in disabled or slot in in_flight:
                    continue
                chunk = pending.popleft()
                store.mark_chunk_running(chunk, slot)
                in_flight[slot] = chunk
                worker_queues[slot].put(chunk)

            if not in_flight:
                # Pending work exists but every credential was rejected.
                break

            result = result_queue.get()
            in_flight.pop(result.api_slot, None)
            chunk_key = (result.chunk.chunk_start, result.chunk.chunk_end)

            if result.success:
                try:
                    run = store.complete_chunk(
                        result.chunk,
                        result.transactions,
                        result.execution_id or "",
                        result.api_slot,
                    )
                except Exception as exc:
                    result = ChunkResult(
                        chunk=result.chunk,
                        api_slot=result.api_slot,
                        transactions=[],
                        execution_id=result.execution_id,
                        error=f"result validation/write failed: {exc}",
                    )
                else:
                    completed_chunks += 1
                    if progress_callback:
                        progress_callback(
                            {
                                "event": "completed",
                                "chunk": result.chunk,
                                "api_slot": result.api_slot,
                                "execution_id": result.execution_id,
                                "result_count": len(result.transactions),
                                "run": run,
                                "progress": store.progress(run_id),
                                "transaction_count": store.coordinator.count_transactions(),
                            }
                        )
                    continue

            error = result.error or "unknown Dune error"
            if result.auth_error:
                disabled.add(result.api_slot)
                logger.error("Disabling %s: %s", result.api_slot, error)

            attempt_counts[chunk_key] += 1
            healthy_workers = len(selected_credentials) - len(disabled)
            if attempt_counts[chunk_key] < MAX_ATTEMPTS_PER_RUN and healthy_workers > 0:
                store.mark_chunk_retry(result.chunk, result.execution_id, error)
                pending.append(result.chunk)
                event = "retrying"
            else:
                store.mark_chunk_failed(result.chunk, result.execution_id, error)
                failed_chunks += 1
                event = "failed"
            if progress_callback:
                progress_callback(
                    {
                        "event": event,
                        "chunk": result.chunk,
                        "api_slot": result.api_slot,
                        "execution_id": result.execution_id,
                        "error": error,
                        "run": store.get_run(run_id),
                        "progress": store.progress(run_id),
                        "transaction_count": store.coordinator.count_transactions(),
                    }
                )

        run = store.finish_run(run_id)
        return SyncOutcome(
            run=run,
            successful=run.status == "completed",
            completed_this_attempt=completed_chunks,
            failed_this_attempt=failed_chunks,
        )
    finally:
        for task_queue in worker_queues.values():
            try:
                task_queue.put_nowait(None)
            except queue.Full:
                pass
        for thread in worker_threads:
            thread.join(timeout=1)


class SyncFileLock:
    """Process-wide non-blocking lock; the OS releases it after a crash."""

    def __init__(self, path: str | Path = DEFAULT_LOCK_PATH):
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

    def __enter__(self) -> "SyncFileLock":
        if not self.acquire():
            raise RuntimeError(f"another arbitrage sync owns {self.path}")
        return self

    def __exit__(self, *_args) -> None:
        self.release()


def prepare_initial_run(store: DuneStore, latest_block: int) -> SyncRun:
    unfinished = store.find_unfinished_run("initial")
    if unfinished:
        return unfinished
    if store.initial_sync_complete():
        raise RuntimeError("initial arbitrage synchronization is already complete")
    if latest_block < HISTORY_START_BLOCK:
        raise RuntimeError(
            f"Ethereum RPC latest block {latest_block} is below required start block "
            f"{HISTORY_START_BLOCK}; verify that GETH_API points to Ethereum mainnet"
        )
    return store.create_run("initial", HISTORY_START_BLOCK, latest_block)


def prepare_incremental_run(store: DuneStore, latest_block: int) -> SyncRun | None:
    unfinished = store.find_unfinished_run("incremental")
    if unfinished:
        return unfinished
    start_block = store.coordinator.max_arbitrage_block()
    if start_block is None:
        start_block = HISTORY_START_BLOCK
    if latest_block < start_block:
        return None
    return store.create_run("incremental", start_block, latest_block)


def start_background_sync(
    latest_block_provider: Callable[[], int],
    *,
    store: DuneStore | None = None,
) -> threading.Thread:
    """Start the startup-then-hourly incremental loop in one daemon thread."""
    sync_store = store or LabelCoordinator().dune

    def loop() -> None:
        sync_store.coordinator.initialize()
        if not sync_store.initial_sync_complete():
            logger.warning(
                "Initial arbitrage history is incomplete; run "
                "the Dune history import before automatic updates"
            )
            return
        credentials = discover_api_credentials()
        if not credentials:
            logger.warning("No numbered Dune API credentials; arbitrage sync is disabled")
            return
        lock = SyncFileLock()
        if not lock.acquire():
            logger.info("Another process owns the arbitrage sync lock; this process only serves reads")
            return
        logger.info(
            "Arbitrage sync coordinator started with %d Dune worker(s): %s",
            len(credentials),
            ", ".join(item.slot for item in credentials),
        )
        try:
            while True:
                try:
                    run = prepare_incremental_run(sync_store, latest_block_provider())
                    if run:
                        outcome = run_sync(sync_store, run.run_id, credentials)
                        if not outcome.successful:
                            logger.error("Incremental arbitrage run %d remains incomplete", run.run_id)
                    else:
                        logger.info("No incremental arbitrage block range is available")
                except Exception:
                    logger.exception("Incremental arbitrage synchronization failed")
                time.sleep(SYNC_INTERVAL_SECONDS)
        finally:
            lock.release()

    thread = threading.Thread(target=loop, name="arbitrage-sync", daemon=True)
    thread.start()
    return thread


# Compatibility helpers for callers of the old in-memory cache API.
def get_cached_hashes() -> dict:
    coordinator = LabelCoordinator()
    return {
        "transactions": coordinator.recent_transactions(500),
        "fetched_at": None,
        "source": "local_sqlite",
        "query_id": DUNE_QUERY_ID,
    }


def fetch_arbitrage_hashes() -> list[dict]:
    """Deprecated: refresh is now managed by the startup/hourly coordinator."""
    logger.warning("Direct arbitrage refresh is deprecated; returning local records")
    return get_cached_hashes()["transactions"]
