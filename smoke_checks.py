from __future__ import annotations

import sys

from config import (
    AUTH_MODE_ANONYMOUS,
    AUTH_MODE_USERNAME_BLANK_BASIC256_TOKEN,
    BAD_SAMPLE_RETENTION_DAYS,
    CLEANUP_INTERVAL_MINUTES,
    DEFAULT_MACHINE_NAMES,
    MACHINE_AUTH_CONFIG,
    POLL_RUN_RETENTION_DAYS,
    SAMPLE_RETENTION_DAYS,
)
from db import get_connection, get_index_names, get_table_column_types, get_tables, init_database

REQUIRED_TABLES = {"machines", "tags", "tag_samples", "poll_runs", "machine_poll_runs"}
REQUIRED_TAG_SAMPLE_COLUMNS = {
    "id",
    "tag_id",
    "machine_id",
    "sampled_at_utc",
    "source_timestamp_utc",
    "server_timestamp_utc",
    "value_numeric",
    "value_text",
    "quality",
    "status_code",
    "error_text",
    "created_at_utc",
}
REQUIRED_INDEXES = {
    "uq_machines_machine_name",
    "idx_machines_enabled",
    "uq_tags_machine_node",
    "idx_tags_machine_enabled",
    "idx_tags_machine_id",
    "idx_tags_display_name",
    "idx_samples_machine_time",
    "idx_samples_tag_time",
    "idx_samples_quality_time",
    "idx_samples_machine_quality_time",
    "idx_samples_created_at",
    "idx_poll_runs_started",
    "idx_poll_runs_finished",
    "idx_machine_poll_machine_finished",
    "idx_machine_poll_poll_run",
    "idx_machine_poll_connection",
}


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    conn = get_connection()
    try:
        init_database(conn)
        tables = get_tables(conn)
        missing_tables = REQUIRED_TABLES - tables
        if missing_tables:
            return fail(f"Missing required tables: {sorted(missing_tables)}")

        tag_sample_columns = get_table_column_types(conn, "tag_samples")
        missing_columns = REQUIRED_TAG_SAMPLE_COLUMNS - set(tag_sample_columns)
        if missing_columns:
            return fail(f"Missing required tag_samples columns: {sorted(missing_columns)}")

        for column_name in ("sampled_at_utc", "source_timestamp_utc", "server_timestamp_utc"):
            if tag_sample_columns.get(column_name) != "datetime":
                return fail(f"{column_name} is not DATETIME in tag_samples")

        poll_run_columns = get_table_column_types(conn, "poll_runs")
        for column_name in ("started_at_utc", "finished_at_utc", "created_at_utc"):
            if poll_run_columns.get(column_name) != "datetime":
                return fail(f"{column_name} is not DATETIME in poll_runs")

        existing_indexes = get_index_names(conn)
        missing_indexes = REQUIRED_INDEXES - existing_indexes
        if missing_indexes:
            return fail(f"Missing required indexes: {sorted(missing_indexes)}")

        with conn.cursor() as cursor:
            cursor.execute("SELECT machine_name FROM machines")
            machine_names = {str(row["machine_name"]) for row in cursor.fetchall()}
            cursor.execute("SELECT COUNT(*) AS count FROM tags")
            tag_count = int(cursor.fetchone()["count"])

        missing_machines = set(DEFAULT_MACHINE_NAMES) - machine_names
        if missing_machines:
            return fail(f"Configured machines missing from DB: {sorted(missing_machines)}")
        if tag_count <= 0:
            return fail("No tags loaded. Run python run_collector.py --init-only first.")

        pinch20 = MACHINE_AUTH_CONFIG["Pinch 20"]
        if pinch20.get("auth_mode") != AUTH_MODE_USERNAME_BLANK_BASIC256_TOKEN:
            return fail("Pinch 20 auth mode is not username_blank_basic256_token")
        if MACHINE_AUTH_CONFIG["Pinch 21"].get("auth_mode") != AUTH_MODE_ANONYMOUS:
            return fail("Pinch 21 auth mode is not anonymous")
        if SAMPLE_RETENTION_DAYS <= 0 or BAD_SAMPLE_RETENTION_DAYS < SAMPLE_RETENTION_DAYS:
            return fail("Retention settings are not sane")
        if POLL_RUN_RETENTION_DAYS <= 0 or CLEANUP_INTERVAL_MINUTES <= 0:
            return fail("Poll run retention or cleanup interval is invalid")

        print("PASS: smoke checks succeeded")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
