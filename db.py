from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from config import (
    BAD_SAMPLE_RETENTION_DAYS,
    CLEANUP_INTERVAL_MINUTES,
    DATA_DIR,
    POLL_RUN_RETENTION_DAYS,
    SAMPLE_RETENTION_DAYS,
    SQLITE_DB_PATH,
    VACUUM_AFTER_DELETE,
)

LOGGER = logging.getLogger(__name__)


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS machines (
        id INTEGER PRIMARY KEY,
        machine_name TEXT UNIQUE,
        endpoint_url TEXT,
        auth_mode TEXT,
        enabled INTEGER,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY,
        machine_id INTEGER,
        node_id TEXT,
        opc_path TEXT,
        display_name TEXT,
        browse_name TEXT,
        data_type TEXT,
        parent_branch TEXT,
        enabled INTEGER,
        created_at TEXT,
        UNIQUE(machine_id, node_id),
        FOREIGN KEY(machine_id) REFERENCES machines(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tag_samples (
        id INTEGER PRIMARY KEY,
        tag_id INTEGER,
        machine_id INTEGER,
        ts_utc TEXT,
        value_text TEXT,
        value_numeric REAL NULL,
        quality TEXT,
        error_text TEXT NULL,
        FOREIGN KEY(tag_id) REFERENCES tags(id),
        FOREIGN KEY(machine_id) REFERENCES machines(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS poll_runs (
        id INTEGER PRIMARY KEY,
        started_at_utc TEXT,
        finished_at_utc TEXT,
        duration_seconds REAL,
        machines_attempted INTEGER,
        machines_ok INTEGER,
        machines_failed INTEGER,
        tags_attempted INTEGER,
        tags_ok INTEGER,
        tags_failed INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tag_samples_ts_utc ON tag_samples(ts_utc)",
    "CREATE INDEX IF NOT EXISTS idx_tag_samples_tag_id_ts_utc ON tag_samples(tag_id, ts_utc)",
    "CREATE INDEX IF NOT EXISTS idx_tag_samples_machine_id_ts_utc ON tag_samples(machine_id, ts_utc)",
    "CREATE INDEX IF NOT EXISTS idx_tags_machine_id ON tags(machine_id)",
    "CREATE INDEX IF NOT EXISTS idx_tags_machine_id_node_id ON tags(machine_id, node_id)",
]


def get_connection(db_path: Path | str = SQLITE_DB_PATH) -> sqlite3.Connection:
    db_file = Path(db_path)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize_database(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        conn.execute("BEGIN")
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def executemany(conn: sqlite3.Connection, sql: str, rows: Iterable[Sequence[object]]) -> None:
    conn.executemany(sql, list(rows))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_now_utc(now_utc: datetime | None) -> datetime:
    if now_utc is None:
        return _utc_now()
    if now_utc.tzinfo is None:
        return now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(timezone.utc)


def _as_sqlite_timestamp(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _rowcount(cursor: sqlite3.Cursor) -> int:
    return max(cursor.rowcount, 0)


def cleanup_old_data(conn: sqlite3.Connection | None = None, now_utc: datetime | None = None) -> dict[str, int]:
    owns_connection = conn is None
    connection = conn or get_connection()
    current_utc = _normalize_now_utc(now_utc)
    good_cutoff = _as_sqlite_timestamp(current_utc - timedelta(days=SAMPLE_RETENTION_DAYS))
    bad_cutoff = _as_sqlite_timestamp(current_utc - timedelta(days=BAD_SAMPLE_RETENTION_DAYS))
    poll_run_cutoff = _as_sqlite_timestamp(current_utc - timedelta(days=POLL_RUN_RETENTION_DAYS))

    results = {
        "good_samples_deleted": 0,
        "bad_samples_deleted": 0,
        "poll_runs_deleted": 0,
    }

    try:
        with transaction(connection):
            good_cursor = connection.execute(
                """
                DELETE FROM tag_samples
                WHERE quality = 'good' AND ts_utc < ?
                """,
                (good_cutoff,),
            )
            bad_cursor = connection.execute(
                """
                DELETE FROM tag_samples
                WHERE quality = 'bad' AND ts_utc < ?
                """,
                (bad_cutoff,),
            )
            poll_cursor = connection.execute(
                """
                DELETE FROM poll_runs
                WHERE COALESCE(finished_at_utc, started_at_utc) < ?
                """,
                (poll_run_cutoff,),
            )
            results["good_samples_deleted"] = _rowcount(good_cursor)
            results["bad_samples_deleted"] = _rowcount(bad_cursor)
            results["poll_runs_deleted"] = _rowcount(poll_cursor)

        LOGGER.info(
            "Cleanup complete good_samples_deleted=%s bad_samples_deleted=%s poll_runs_deleted=%s",
            results["good_samples_deleted"],
            results["bad_samples_deleted"],
            results["poll_runs_deleted"],
        )

        if VACUUM_AFTER_DELETE and any(results.values()):
            LOGGER.info("Running VACUUM after cleanup")
            connection.execute("VACUUM")
    finally:
        if owns_connection:
            connection.close()

    return results


def clear_poll_runs(conn: sqlite3.Connection | None = None) -> int:
    owns_connection = conn is None
    connection = conn or get_connection()
    try:
        with transaction(connection):
            cursor = connection.execute("DELETE FROM poll_runs")
            deleted = _rowcount(cursor)
        LOGGER.warning("Cleared poll_runs deleted=%s", deleted)
        if VACUUM_AFTER_DELETE and deleted > 0:
            connection.execute("VACUUM")
        return deleted
    finally:
        if owns_connection:
            connection.close()


def clear_bad_samples(conn: sqlite3.Connection | None = None) -> int:
    owns_connection = conn is None
    connection = conn or get_connection()
    try:
        with transaction(connection):
            cursor = connection.execute("DELETE FROM tag_samples WHERE quality = 'bad'")
            deleted = _rowcount(cursor)
        LOGGER.warning("Cleared bad tag_samples deleted=%s", deleted)
        if VACUUM_AFTER_DELETE and deleted > 0:
            connection.execute("VACUUM")
        return deleted
    finally:
        if owns_connection:
            connection.close()


def clear_all_samples(conn: sqlite3.Connection | None = None) -> int:
    owns_connection = conn is None
    connection = conn or get_connection()
    try:
        with transaction(connection):
            cursor = connection.execute("DELETE FROM tag_samples")
            deleted = _rowcount(cursor)
        LOGGER.warning("Cleared all tag_samples deleted=%s", deleted)
        if VACUUM_AFTER_DELETE and deleted > 0:
            connection.execute("VACUUM")
        return deleted
    finally:
        if owns_connection:
            connection.close()


def clear_monitoring_data(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    owns_connection = conn is None
    connection = conn or get_connection()
    results = {"tag_samples_deleted": 0, "poll_runs_deleted": 0}
    try:
        with transaction(connection):
            tag_cursor = connection.execute("DELETE FROM tag_samples")
            poll_cursor = connection.execute("DELETE FROM poll_runs")
            results["tag_samples_deleted"] = _rowcount(tag_cursor)
            results["poll_runs_deleted"] = _rowcount(poll_cursor)
        LOGGER.warning(
            "Cleared monitoring data tag_samples_deleted=%s poll_runs_deleted=%s",
            results["tag_samples_deleted"],
            results["poll_runs_deleted"],
        )
        if VACUUM_AFTER_DELETE and any(results.values()):
            connection.execute("VACUUM")
        return results
    finally:
        if owns_connection:
            connection.close()
