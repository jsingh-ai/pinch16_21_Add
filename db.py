from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import pymysql
from pymysql.connections import Connection
from pymysql.cursors import DictCursor

from config import (
    BAD_SAMPLE_RETENTION_DAYS,
    CLEANUP_INTERVAL_MINUTES,
    MYSQL_DATABASE,
    POLL_RUN_RETENTION_DAYS,
    SAMPLE_RETENTION_DAYS,
    get_mysql_connection_kwargs,
)

LOGGER = logging.getLogger(__name__)

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS machines (
        id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        machine_name VARCHAR(100) NOT NULL,
        endpoint_url VARCHAR(255) NOT NULL,
        auth_mode VARCHAR(80) NOT NULL,
        enabled TINYINT(1) NOT NULL DEFAULT 1,
        created_at_utc DATETIME(6) NOT NULL,
        updated_at_utc DATETIME(6) NOT NULL,
        UNIQUE KEY uq_machines_machine_name(machine_name),
        KEY idx_machines_enabled(enabled)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS tags (
        id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        machine_id BIGINT UNSIGNED NOT NULL,
        node_id VARCHAR(512) NOT NULL,
        opc_path TEXT NULL,
        display_name VARCHAR(255) NULL,
        browse_name VARCHAR(255) NULL,
        data_type VARCHAR(120) NULL,
        parent_branch VARCHAR(120) NULL,
        enabled TINYINT(1) NOT NULL DEFAULT 1,
        created_at_utc DATETIME(6) NOT NULL,
        updated_at_utc DATETIME(6) NOT NULL,
        UNIQUE KEY uq_tags_machine_node(machine_id, node_id),
        KEY idx_tags_machine_enabled(machine_id, enabled),
        KEY idx_tags_machine_id(machine_id),
        KEY idx_tags_display_name(display_name),
        CONSTRAINT fk_tags_machine_id FOREIGN KEY (machine_id) REFERENCES machines(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS tag_samples (
        id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        tag_id BIGINT UNSIGNED NOT NULL,
        machine_id BIGINT UNSIGNED NOT NULL,
        sampled_at_utc DATETIME(6) NOT NULL,
        source_timestamp_utc DATETIME(6) NULL,
        server_timestamp_utc DATETIME(6) NULL,
        value_numeric DOUBLE NULL,
        value_text TEXT NULL,
        quality VARCHAR(40) NOT NULL,
        status_code VARCHAR(120) NULL,
        error_text TEXT NULL,
        created_at_utc DATETIME(6) NOT NULL,
        KEY idx_samples_machine_time(machine_id, sampled_at_utc),
        KEY idx_samples_tag_time(tag_id, sampled_at_utc),
        KEY idx_samples_quality_time(quality, sampled_at_utc),
        KEY idx_samples_machine_quality_time(machine_id, quality, sampled_at_utc),
        KEY idx_samples_created_at(created_at_utc),
        CONSTRAINT fk_tag_samples_machine_id FOREIGN KEY (machine_id) REFERENCES machines(id),
        CONSTRAINT fk_tag_samples_tag_id FOREIGN KEY (tag_id) REFERENCES tags(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS poll_runs (
        id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        started_at_utc DATETIME(6) NOT NULL,
        finished_at_utc DATETIME(6) NULL,
        duration_seconds DOUBLE NULL,
        machines_attempted INT NOT NULL DEFAULT 0,
        machines_ok INT NOT NULL DEFAULT 0,
        machines_failed INT NOT NULL DEFAULT 0,
        tags_attempted INT NOT NULL DEFAULT 0,
        tags_ok INT NOT NULL DEFAULT 0,
        tags_failed INT NOT NULL DEFAULT 0,
        created_at_utc DATETIME(6) NOT NULL,
        KEY idx_poll_runs_started(started_at_utc),
        KEY idx_poll_runs_finished(finished_at_utc)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS machine_poll_runs (
        id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        poll_run_id BIGINT UNSIGNED NULL,
        machine_id BIGINT UNSIGNED NOT NULL,
        machine_name VARCHAR(100) NOT NULL,
        endpoint_url VARCHAR(255) NULL,
        started_at_utc DATETIME(6) NOT NULL,
        finished_at_utc DATETIME(6) NULL,
        duration_seconds DOUBLE NULL,
        tags_attempted INT NOT NULL DEFAULT 0,
        tags_ok INT NOT NULL DEFAULT 0,
        tags_failed INT NOT NULL DEFAULT 0,
        connection_ok TINYINT(1) NOT NULL DEFAULT 0,
        error_text TEXT NULL,
        created_at_utc DATETIME(6) NOT NULL,
        KEY idx_machine_poll_machine_finished(machine_id, finished_at_utc),
        KEY idx_machine_poll_poll_run(poll_run_id),
        KEY idx_machine_poll_connection(machine_id, connection_ok, finished_at_utc),
        CONSTRAINT fk_machine_poll_runs_machine_id FOREIGN KEY (machine_id) REFERENCES machines(id),
        CONSTRAINT fk_machine_poll_runs_poll_run_id FOREIGN KEY (poll_run_id) REFERENCES poll_runs(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
]

TABLES = {"machines", "tags", "tag_samples", "poll_runs", "machine_poll_runs"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_db_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.replace(tzinfo=None)


def from_db_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_iso_utc(value: datetime | None) -> str | None:
    converted = from_db_datetime(value)
    if converted is None:
        return None
    return converted.replace(microsecond=0).isoformat()


def get_connection(database: str | None = None) -> Connection:
    conn = pymysql.connect(
        cursorclass=DictCursor,
        **get_mysql_connection_kwargs(database=database),
    )
    with conn.cursor() as cursor:
        cursor.execute("SET time_zone = '+00:00'")
    return conn


def init_database(conn: Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        with conn.cursor() as cursor:
            cursor.execute(statement)
    conn.commit()
    warn_if_legacy_schema(conn)


initialize_database = init_database


def warn_if_legacy_schema(conn: Connection) -> None:
    columns = get_table_column_types(conn, "tag_samples")
    if not columns:
        return
    if "ts_utc" in columns:
        LOGGER.warning(
            "Legacy schema detected: tag_samples.ts_utc exists. Manual migration to DATETIME(6) schema is recommended."
        )
    sampled_type = columns.get("sampled_at_utc")
    if sampled_type and "datetime" not in sampled_type.lower():
        LOGGER.warning(
            "tag_samples.sampled_at_utc is not DATETIME-compatible. Manual migration is recommended before production use."
        )


def get_tables(conn: Connection) -> set[str]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
            """,
            (MYSQL_DATABASE,),
        )
        return {str(row["table_name"]) for row in cursor.fetchall()}


def get_table_columns(conn: Connection, table_name: str) -> set[str]:
    return set(get_table_column_types(conn, table_name))


def get_table_column_types(conn: Connection, table_name: str) -> dict[str, str]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            """,
            (MYSQL_DATABASE, table_name),
        )
        rows = cursor.fetchall()
    return {str(row["column_name"]): str(row["data_type"]) for row in rows}


def get_index_names(conn: Connection) -> set[str]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT index_name
            FROM information_schema.statistics
            WHERE table_schema = %s
            """,
            (MYSQL_DATABASE,),
        )
        return {str(row["index_name"]) for row in cursor.fetchall()}


@contextmanager
def transaction(conn: Connection) -> Iterator[Connection]:
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def executemany(conn: Connection, sql: str, rows: list[tuple[object, ...]]) -> None:
    if not rows:
        return
    with conn.cursor() as cursor:
        cursor.executemany(sql, rows)


def cleanup_old_data(conn: Connection | None = None, now_utc: datetime | None = None) -> dict[str, int]:
    owns_connection = conn is None
    connection = conn or get_connection()
    current_utc = now_utc or utc_now()
    good_cutoff = to_db_datetime(current_utc - timedelta(days=SAMPLE_RETENTION_DAYS))
    bad_cutoff = to_db_datetime(current_utc - timedelta(days=BAD_SAMPLE_RETENTION_DAYS))
    poll_cutoff = to_db_datetime(current_utc - timedelta(days=POLL_RUN_RETENTION_DAYS))
    results = {
        "good_samples_deleted": 0,
        "bad_samples_deleted": 0,
        "poll_runs_deleted": 0,
        "machine_poll_runs_deleted": 0,
    }
    try:
        with transaction(connection):
            with connection.cursor() as cursor:
                results["good_samples_deleted"] = cursor.execute(
                    "DELETE FROM tag_samples WHERE quality = %s AND sampled_at_utc < %s",
                    ("good", good_cutoff),
                )
                results["bad_samples_deleted"] = cursor.execute(
                    "DELETE FROM tag_samples WHERE quality <> %s AND sampled_at_utc < %s",
                    ("good", bad_cutoff),
                )
                results["machine_poll_runs_deleted"] = cursor.execute(
                    "DELETE FROM machine_poll_runs WHERE COALESCE(finished_at_utc, started_at_utc) < %s",
                    (poll_cutoff,),
                )
                results["poll_runs_deleted"] = cursor.execute(
                    "DELETE FROM poll_runs WHERE COALESCE(finished_at_utc, started_at_utc) < %s",
                    (poll_cutoff,),
                )
        LOGGER.info("Cleanup complete %s", results)
        return results
    finally:
        if owns_connection:
            connection.close()


def clear_poll_runs(conn: Connection | None = None) -> int:
    owns_connection = conn is None
    connection = conn or get_connection()
    try:
        with transaction(connection):
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM machine_poll_runs")
                deleted = cursor.execute("DELETE FROM poll_runs")
        LOGGER.warning("Cleared poll_runs deleted=%s", deleted)
        return deleted
    finally:
        if owns_connection:
            connection.close()


def clear_bad_samples(conn: Connection | None = None) -> int:
    owns_connection = conn is None
    connection = conn or get_connection()
    try:
        with transaction(connection):
            with connection.cursor() as cursor:
                deleted = cursor.execute("DELETE FROM tag_samples WHERE quality <> %s", ("good",))
        LOGGER.warning("Cleared bad tag_samples deleted=%s", deleted)
        return deleted
    finally:
        if owns_connection:
            connection.close()


def clear_all_samples(conn: Connection | None = None) -> int:
    owns_connection = conn is None
    connection = conn or get_connection()
    try:
        with transaction(connection):
            with connection.cursor() as cursor:
                deleted = cursor.execute("DELETE FROM tag_samples")
        LOGGER.warning("Cleared all tag_samples deleted=%s", deleted)
        return deleted
    finally:
        if owns_connection:
            connection.close()


def clear_monitoring_data(conn: Connection | None = None) -> dict[str, int]:
    owns_connection = conn is None
    connection = conn or get_connection()
    results = {"tag_samples_deleted": 0, "poll_runs_deleted": 0, "machine_poll_runs_deleted": 0}
    try:
        with transaction(connection):
            with connection.cursor() as cursor:
                results["tag_samples_deleted"] = cursor.execute("DELETE FROM tag_samples")
                results["machine_poll_runs_deleted"] = cursor.execute("DELETE FROM machine_poll_runs")
                results["poll_runs_deleted"] = cursor.execute("DELETE FROM poll_runs")
        LOGGER.warning("Cleared monitoring data %s", results)
        return results
    finally:
        if owns_connection:
            connection.close()


def get_db_stats(conn: Connection | None = None) -> dict[str, object]:
    owns_connection = conn is None
    connection = conn or get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS count FROM machines WHERE enabled = 1")
            enabled_machine_count = int(cursor.fetchone()["count"])

            cursor.execute("SELECT COUNT(*) AS count FROM tags WHERE enabled = 1")
            enabled_tag_count = int(cursor.fetchone()["count"])

            cursor.execute(
                """
                SELECT
                    COUNT(*) AS total_sample_rows,
                    SUM(CASE WHEN quality = 'good' THEN 1 ELSE 0 END) AS good_sample_rows,
                    SUM(CASE WHEN quality <> 'good' THEN 1 ELSE 0 END) AS bad_sample_rows,
                    MIN(sampled_at_utc) AS oldest_sampled_at_utc,
                    MAX(sampled_at_utc) AS newest_sampled_at_utc
                FROM tag_samples
                """
            )
            sample_row = cursor.fetchone()

            cursor.execute("SELECT COUNT(*) AS count FROM poll_runs")
            total_poll_runs = int(cursor.fetchone()["count"])

            cursor.execute("SELECT COUNT(*) AS count FROM machine_poll_runs")
            total_machine_poll_runs = int(cursor.fetchone()["count"])

            cursor.execute(
                """
                SELECT COALESCE(SUM(data_length + index_length), 0) AS size_bytes
                FROM information_schema.tables
                WHERE table_schema = %s
                """,
                (MYSQL_DATABASE,),
            )
            size_row = cursor.fetchone()

        samples_per_day = enabled_tag_count * 24 * 60
        estimated_retained_rows = samples_per_day * SAMPLE_RETENTION_DAYS
        warning_threshold_rows = 25_000_000
        return {
            "database": MYSQL_DATABASE,
            "enabled_machine_count": enabled_machine_count,
            "enabled_tag_count": enabled_tag_count,
            "total_sample_rows": int(sample_row["total_sample_rows"] or 0),
            "good_sample_rows": int(sample_row["good_sample_rows"] or 0),
            "bad_sample_rows": int(sample_row["bad_sample_rows"] or 0),
            "oldest_sampled_at_utc": to_iso_utc(sample_row["oldest_sampled_at_utc"]),
            "newest_sampled_at_utc": to_iso_utc(sample_row["newest_sampled_at_utc"]),
            "total_poll_runs": total_poll_runs,
            "total_machine_poll_runs": total_machine_poll_runs,
            "samples_per_day": samples_per_day,
            "estimated_retained_rows": estimated_retained_rows,
            "db_size_mb": round(int(size_row["size_bytes"] or 0) / (1024 * 1024), 2),
            "warning_threshold_rows": warning_threshold_rows,
            "warning": (
                "Estimated retained rows exceed 25 million; long retention should move to a larger MySQL tier or another warehouse."
                if estimated_retained_rows > warning_threshold_rows
                else ""
            ),
        }
    finally:
        if owns_connection:
            connection.close()
