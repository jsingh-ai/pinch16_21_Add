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
        status_code TEXT NULL,
        source_timestamp_utc TEXT NULL,
        server_timestamp_utc TEXT NULL,
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
    """
    CREATE TABLE IF NOT EXISTS machine_poll_runs (
        id INTEGER PRIMARY KEY,
        poll_run_id INTEGER,
        machine_id INTEGER,
        machine_name TEXT,
        endpoint_url TEXT,
        started_at_utc TEXT,
        finished_at_utc TEXT,
        duration_seconds REAL,
        tags_attempted INTEGER,
        tags_ok INTEGER,
        tags_failed INTEGER,
        connection_ok INTEGER,
        error_text TEXT,
        FOREIGN KEY(poll_run_id) REFERENCES poll_runs(id) ON DELETE CASCADE,
        FOREIGN KEY(machine_id) REFERENCES machines(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tag_samples_ts_utc ON tag_samples(ts_utc)",
    "CREATE INDEX IF NOT EXISTS idx_tag_samples_tag_id_ts_utc ON tag_samples(tag_id, ts_utc)",
    "CREATE INDEX IF NOT EXISTS idx_tag_samples_machine_id_ts_utc ON tag_samples(machine_id, ts_utc)",
    "CREATE INDEX IF NOT EXISTS idx_tag_samples_quality_ts_utc ON tag_samples(quality, ts_utc)",
    "CREATE INDEX IF NOT EXISTS idx_tag_samples_machine_quality_ts_utc ON tag_samples(machine_id, quality, ts_utc)",
    "CREATE INDEX IF NOT EXISTS idx_tag_samples_machine_tag_ts_utc ON tag_samples(machine_id, tag_id, ts_utc)",
    "CREATE INDEX IF NOT EXISTS idx_tags_machine_id ON tags(machine_id)",
    "CREATE INDEX IF NOT EXISTS idx_tags_machine_id_node_id ON tags(machine_id, node_id)",
    "CREATE INDEX IF NOT EXISTS idx_poll_runs_finished_at_utc ON poll_runs(finished_at_utc)",
    "CREATE INDEX IF NOT EXISTS idx_poll_runs_started_at_utc ON poll_runs(started_at_utc)",
    "CREATE INDEX IF NOT EXISTS idx_machine_poll_runs_machine_finished ON machine_poll_runs(machine_id, finished_at_utc)",
    "CREATE INDEX IF NOT EXISTS idx_machine_poll_runs_poll_run_id ON machine_poll_runs(poll_run_id)",
]

TAG_SAMPLES_REQUIRED_COLUMNS = {
    "status_code": "ALTER TABLE tag_samples ADD COLUMN status_code TEXT NULL",
    "source_timestamp_utc": "ALTER TABLE tag_samples ADD COLUMN source_timestamp_utc TEXT NULL",
    "server_timestamp_utc": "ALTER TABLE tag_samples ADD COLUMN server_timestamp_utc TEXT NULL",
}


def get_connection(db_path: Path | str = SQLITE_DB_PATH) -> sqlite3.Connection:
    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
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
    _ensure_tag_samples_columns(conn)
    conn.commit()
    conn.execute("PRAGMA optimize")


def _ensure_tag_samples_columns(conn: sqlite3.Connection) -> None:
    existing_columns = get_table_columns(conn, "tag_samples")
    for column_name, statement in TAG_SAMPLES_REQUIRED_COLUMNS.items():
        if column_name not in existing_columns:
            conn.execute(statement)


def get_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def get_index_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row["name"]) for row in rows}


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


def _vacuum_if_enabled(conn: sqlite3.Connection, deleted_any: bool) -> None:
    if VACUUM_AFTER_DELETE and deleted_any:
        LOGGER.info("Running VACUUM after delete operation")
        conn.execute("VACUUM")


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
                "DELETE FROM tag_samples WHERE quality = 'good' AND ts_utc < ?",
                (good_cutoff,),
            )
            bad_cursor = connection.execute(
                "DELETE FROM tag_samples WHERE quality <> 'good' AND ts_utc < ?",
                (bad_cutoff,),
            )
            poll_cursor = connection.execute(
                "DELETE FROM poll_runs WHERE COALESCE(finished_at_utc, started_at_utc) < ?",
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
        _vacuum_if_enabled(connection, any(results.values()))
        connection.execute("PRAGMA optimize")
    finally:
        if owns_connection:
            connection.close()

    return results


def clear_poll_runs(conn: sqlite3.Connection | None = None) -> int:
    owns_connection = conn is None
    connection = conn or get_connection()
    try:
        with transaction(connection):
            deleted = _rowcount(connection.execute("DELETE FROM poll_runs"))
        LOGGER.warning("Cleared poll_runs deleted=%s", deleted)
        _vacuum_if_enabled(connection, deleted > 0)
        return deleted
    finally:
        if owns_connection:
            connection.close()


def clear_bad_samples(conn: sqlite3.Connection | None = None) -> int:
    owns_connection = conn is None
    connection = conn or get_connection()
    try:
        with transaction(connection):
            deleted = _rowcount(connection.execute("DELETE FROM tag_samples WHERE quality <> 'good'"))
        LOGGER.warning("Cleared bad tag_samples deleted=%s", deleted)
        _vacuum_if_enabled(connection, deleted > 0)
        return deleted
    finally:
        if owns_connection:
            connection.close()


def clear_all_samples(conn: sqlite3.Connection | None = None) -> int:
    owns_connection = conn is None
    connection = conn or get_connection()
    try:
        with transaction(connection):
            deleted = _rowcount(connection.execute("DELETE FROM tag_samples"))
        LOGGER.warning("Cleared all tag_samples deleted=%s", deleted)
        _vacuum_if_enabled(connection, deleted > 0)
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
            results["tag_samples_deleted"] = _rowcount(connection.execute("DELETE FROM tag_samples"))
            results["poll_runs_deleted"] = _rowcount(connection.execute("DELETE FROM poll_runs"))
        LOGGER.warning(
            "Cleared monitoring data tag_samples_deleted=%s poll_runs_deleted=%s",
            results["tag_samples_deleted"],
            results["poll_runs_deleted"],
        )
        _vacuum_if_enabled(connection, any(results.values()))
        return results
    finally:
        if owns_connection:
            connection.close()


def checkpoint_database(
    conn: sqlite3.Connection | None = None,
    mode: str = "PASSIVE",
) -> dict[str, int | str]:
    owns_connection = conn is None
    connection = conn or get_connection()
    normalized_mode = mode.upper()
    try:
        row = connection.execute(f"PRAGMA wal_checkpoint({normalized_mode})").fetchone()
        result = {
            "mode": normalized_mode,
            "busy": int(row[0]) if row is not None else 0,
            "log_frames": int(row[1]) if row is not None else 0,
            "checkpointed_frames": int(row[2]) if row is not None else 0,
        }
        LOGGER.info("Checkpoint complete %s", result)
        connection.execute("PRAGMA optimize")
        return result
    finally:
        if owns_connection:
            connection.close()


def get_db_stats(conn: sqlite3.Connection | None = None, db_path: Path | str = SQLITE_DB_PATH) -> dict[str, object]:
    owns_connection = conn is None
    connection = conn or get_connection(db_path)
    db_file = Path(db_path)
    try:
        enabled_machine_count = int(
            connection.execute("SELECT COUNT(*) FROM machines WHERE enabled = 1").fetchone()[0]
        )
        enabled_tag_count = int(
            connection.execute("SELECT COUNT(*) FROM tags WHERE enabled = 1").fetchone()[0]
        )
        total_sample_rows = int(connection.execute("SELECT COUNT(*) FROM tag_samples").fetchone()[0])
        total_poll_runs = int(connection.execute("SELECT COUNT(*) FROM poll_runs").fetchone()[0])
        samples_per_day = enabled_tag_count * 24 * 60
        estimated_good_rows_retained = samples_per_day * SAMPLE_RETENTION_DAYS
        worst_case_rows_retained = samples_per_day * BAD_SAMPLE_RETENTION_DAYS
        warning_threshold_rows = 25_000_000
        return {
            "db_path": str(db_file),
            "db_size_mb": round(db_file.stat().st_size / (1024 * 1024), 2) if db_file.exists() else 0.0,
            "enabled_machine_count": enabled_machine_count,
            "enabled_tag_count": enabled_tag_count,
            "samples_per_day": samples_per_day,
            "estimated_good_rows_retained": estimated_good_rows_retained,
            "estimated_worst_case_rows_retained": worst_case_rows_retained,
            "total_sample_rows": total_sample_rows,
            "total_poll_runs": total_poll_runs,
            "retention_days": {
                "good_samples": SAMPLE_RETENTION_DAYS,
                "bad_samples": BAD_SAMPLE_RETENTION_DAYS,
                "poll_runs": POLL_RUN_RETENTION_DAYS,
            },
            "cleanup_interval_minutes": CLEANUP_INTERVAL_MINUTES,
            "warning_threshold_rows": warning_threshold_rows,
            "warning": (
                "Estimated retained rows exceed 25 million; plan PostgreSQL/MySQL for long retention."
                if estimated_good_rows_retained > warning_threshold_rows
                else ""
            ),
        }
    finally:
        if owns_connection:
            connection.close()
