#!/usr/bin/env python3
"""Build, refresh, or compact the local 4byte function-signature database."""

from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from signature.store import (  # noqa: E402
    DEFAULT_SIGNATURE_DB_PATH,
    SCHEMA_VERSION,
    get_metadata,
    initialize_database,
    set_metadata,
    upsert_api_records,
)


API_URL = "https://www.4byte.directory/api/v1/signatures/"
SOURCE_NAME = "4byte.directory"
USER_AGENT = "evm-transaction-analyzer-signature-sync/1.0"
FINAL_METADATA_KEYS = {
    "schema_version",
    "source",
    "source_url",
    "record_count",
    "sync_complete",
    "synced_at",
}
_thread_local = threading.local()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        _thread_local.session = session
    return session


def _request_json(params: dict[str, Any], *, timeout: float, retries: int) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = _session().get(API_URL, params=params, timeout=timeout)
            if response.status_code == 429 or response.status_code >= 500:
                response.raise_for_status()
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise RuntimeError("4byte API returned an unexpected payload")
            return payload
        except Exception as exc:
            last_error = exc
            if attempt == retries:
                break
            time.sleep(min(2**attempt, 20))
    raise RuntimeError(f"4byte request failed after {retries + 1} attempts: {last_error}")


def _source_count(*, timeout: float, retries: int) -> int:
    payload = _request_json({"page": 1, "page_size": 1}, timeout=timeout, retries=retries)
    return int(payload["count"])


def _fetch_page(
    page_number: int,
    page_size: int,
    *,
    timeout: float,
    retries: int,
) -> tuple[int, list[dict[str, Any]]]:
    payload = _request_json(
        {"page": page_number, "page_size": page_size},
        timeout=timeout,
        retries=retries,
    )
    return page_number, payload["results"]


def _prepare_partial_database(path: Path, *, restart_partial: bool) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if restart_partial and path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    initialize_database(connection)
    return connection


def _completed_pages(connection: sqlite3.Connection) -> set[int]:
    return {int(row[0]) for row in connection.execute("SELECT page_number FROM sync_pages")}


def _sync_pass(
    connection: sqlite3.Connection,
    *,
    snapshot_count: int,
    page_size: int,
    workers: int,
    timeout: float,
    retries: int,
    resume: bool,
) -> None:
    total_pages = math.ceil(snapshot_count / page_size)
    metadata = get_metadata(connection)
    can_resume = (
        resume
        and metadata.get("source") == SOURCE_NAME
        and metadata.get("snapshot_count") == str(snapshot_count)
        and metadata.get("page_size") == str(page_size)
        and metadata.get("sync_complete") != "true"
    )

    if not can_resume:
        connection.execute("DELETE FROM sync_pages")
        connection.commit()

    set_metadata(connection, "source", SOURCE_NAME)
    set_metadata(connection, "source_url", API_URL)
    set_metadata(connection, "snapshot_count", snapshot_count)
    set_metadata(connection, "page_size", page_size)
    set_metadata(connection, "total_pages", total_pages)
    set_metadata(connection, "sync_complete", "false")
    set_metadata(connection, "sync_started_at", metadata.get("sync_started_at") or _utc_now())
    connection.commit()

    done = _completed_pages(connection) if can_resume else set()
    pending = [page for page in range(1, total_pages + 1) if page not in done]
    if done:
        print(f"从断点继续：已完成 {len(done)}/{total_pages} 页")

    completed = len(done)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _fetch_page,
                page,
                page_size,
                timeout=timeout,
                retries=retries,
            ): page
            for page in pending
        }
        for future in as_completed(futures):
            page_number, records = future.result()
            with connection:
                upsert_api_records(connection, records)
                connection.execute(
                    "INSERT OR REPLACE INTO sync_pages(page_number, row_count, fetched_at) "
                    "VALUES (?, ?, ?)",
                    (page_number, len(records), _utc_now()),
                )
            completed += 1
            if completed == total_pages or completed % 25 == 0:
                row_count = connection.execute(
                    "SELECT COUNT(*) FROM function_signatures"
                ).fetchone()[0]
                print(f"同步进度：{completed}/{total_pages} 页，本地 {row_count:,} 条")


def _trim_build_state(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE IF EXISTS sync_pages")
    placeholders = ",".join("?" for _ in FINAL_METADATA_KEYS)
    connection.execute(
        f"DELETE FROM metadata WHERE key NOT IN ({placeholders})",
        tuple(sorted(FINAL_METADATA_KEYS)),
    )
    connection.commit()


def _close_for_publish(connection: sqlite3.Connection) -> None:
    connection.execute("ANALYZE")
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.close()


def sync_database(args: argparse.Namespace) -> Path:
    target_path = Path(args.db_path).expanduser().resolve()
    partial_path = target_path.with_name(f"{target_path.name}.v{SCHEMA_VERSION}.partial")
    connection = _prepare_partial_database(
        partial_path,
        restart_partial=args.restart_partial,
    )

    try:
        for pass_number in range(1, args.max_passes + 1):
            start_count = _source_count(timeout=args.timeout, retries=args.retries)
            print(
                f"第 {pass_number} 轮：4byte 当前 {start_count:,} 条，"
                f"每页 {args.page_size} 条，{args.workers} 个并发请求"
            )
            _sync_pass(
                connection,
                snapshot_count=start_count,
                page_size=args.page_size,
                workers=args.workers,
                timeout=args.timeout,
                retries=args.retries,
                resume=pass_number == 1,
            )
            end_count = _source_count(timeout=args.timeout, retries=args.retries)
            local_count = int(
                connection.execute("SELECT COUNT(*) FROM function_signatures").fetchone()[0]
            )
            if start_count == end_count == local_count:
                set_metadata(connection, "record_count", local_count)
                set_metadata(connection, "sync_complete", "true")
                set_metadata(connection, "synced_at", _utc_now())
                connection.commit()
                break
            print(
                "快照在同步期间发生变化或有页面缺失："
                f"开始 {start_count:,}，结束 {end_count:,}，本地 {local_count:,}；"
                "将重新扫描补齐。"
            )
        else:
            raise RuntimeError(
                f"连续 {args.max_passes} 轮仍无法得到稳定完整快照；"
                f"未完成数据库保留在 {partial_path}，可稍后直接重试。"
            )

        _trim_build_state(connection)
        _close_for_publish(connection)
        os.replace(partial_path, target_path)
        size_mb = target_path.stat().st_size / (1024 * 1024)
        print(f"完整签名库已生成：{target_path} ({local_count:,} 条，{size_mb:.1f} MiB)")
        return target_path
    except Exception:
        connection.close()
        raise


def compact_existing_database(path: str | Path) -> Path:
    """Atomically migrate a v1 database without downloading the API again."""
    target_path = Path(path).expanduser().resolve()
    if not target_path.is_file():
        raise FileNotFoundError(f"签名数据库不存在：{target_path}")

    source_connection = sqlite3.connect(target_path)
    source_metadata = get_metadata(source_connection)
    source_version = source_metadata.get("schema_version")
    if source_version == SCHEMA_VERSION:
        source_connection.close()
        print(f"数据库已经是精简 schema v{SCHEMA_VERSION}：{target_path}")
        return target_path
    if source_version != "1":
        source_connection.close()
        raise RuntimeError(f"不支持从 schema {source_version!r} 迁移")

    old_size_mb = target_path.stat().st_size / (1024 * 1024)
    compact_path = target_path.with_name(f"{target_path.name}.v{SCHEMA_VERSION}.compact")
    if compact_path.exists():
        compact_path.unlink()

    connection = sqlite3.connect(compact_path)
    try:
        initialize_database(connection)
        connection.execute("ATTACH DATABASE ? AS old_database", (str(target_path),))
        connection.execute(
            """
            INSERT INTO function_signatures(
                api_id, selector, text_signature, function_name, priority_rank
            )
            SELECT api_id, selector, text_signature, function_name, priority_rank
            FROM old_database.function_signatures
            """
        )
        connection.commit()
        connection.execute("DETACH DATABASE old_database")

        local_count = int(
            connection.execute("SELECT COUNT(*) FROM function_signatures").fetchone()[0]
        )
        expected_count = int(source_metadata.get("record_count", local_count))
        if local_count != expected_count:
            raise RuntimeError(
                f"迁移记录数不一致：原库 {expected_count:,}，新库 {local_count:,}"
            )

        set_metadata(connection, "source", source_metadata.get("source", SOURCE_NAME))
        set_metadata(connection, "source_url", source_metadata.get("source_url", API_URL))
        set_metadata(connection, "record_count", local_count)
        set_metadata(connection, "sync_complete", "true")
        set_metadata(connection, "synced_at", source_metadata.get("synced_at", _utc_now()))
        connection.commit()
        _trim_build_state(connection)

        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"迁移后数据库完整性检查失败：{integrity}")

        _close_for_publish(connection)
        source_connection.close()
        os.replace(compact_path, target_path)
    except Exception:
        connection.close()
        source_connection.close()
        raise

    new_size_mb = target_path.stat().st_size / (1024 * 1024)
    saved_mb = old_size_mb - new_size_mb
    print(
        f"数据库精简完成：{target_path}，{local_count:,} 条，"
        f"{old_size_mb:.1f} → {new_size_mb:.1f} MiB，减少 {saved_mb:.1f} MiB"
    )
    return target_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="完整镜像或精简本地 4byte.directory 函数签名 SQLite"
    )
    parser.add_argument("--db-path", default=str(DEFAULT_SIGNATURE_DB_PATH))
    parser.add_argument(
        "--compact-existing",
        action="store_true",
        help="将现有 schema v1 数据库原子迁移为精简版，不访问网络",
    )
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--max-passes", type=int, default=3)
    parser.add_argument(
        "--restart-partial",
        action="store_true",
        help="丢弃未完成的 .partial 数据库后重新开始",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.compact_existing:
        compact_existing_database(args.db_path)
        return 0
    if args.page_size <= 0 or args.workers <= 0 or args.max_passes <= 0:
        raise SystemExit("page-size、workers 和 max-passes 必须为正数")
    if args.page_size > 1000:
        raise SystemExit("4byte API 当前单页上限为 1000，page-size 不能超过 1000")
    sync_database(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
