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
    SQLITE_DB_PATH,
)
from db import get_connection, get_index_names, get_table_columns, initialize_database

REQUIRED_TABLES = {"machines", "tags", "tag_samples", "poll_runs", "machine_poll_runs"}
REQUIRED_TAG_SAMPLE_COLUMNS = {
    "id",
    "tag_id",
    "machine_id",
    "ts_utc",
    "value_text",
    "value_numeric",
    "quality",
    "error_text",
    "status_code",
    "source_timestamp_utc",
    "server_timestamp_utc",
}
REQUIRED_INDEXES = {
    "idx_tag_samples_ts_utc",
    "idx_tag_samples_tag_id_ts_utc",
    "idx_tag_samples_machine_id_ts_utc",
    "idx_tag_samples_quality_ts_utc",
    "idx_tag_samples_machine_quality_ts_utc",
    "idx_tag_samples_machine_tag_ts_utc",
    "idx_tags_machine_id",
    "idx_tags_machine_id_node_id",
    "idx_poll_runs_finished_at_utc",
    "idx_poll_runs_started_at_utc",
    "idx_machine_poll_runs_machine_finished",
    "idx_machine_poll_runs_poll_run_id",
}


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    conn = get_connection(SQLITE_DB_PATH)
    try:
        initialize_database(conn)
        table_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables = {str(row["name"]) for row in table_rows}
        missing_tables = REQUIRED_TABLES - tables
        if missing_tables:
            return fail(f"Missing required tables: {sorted(missing_tables)}")

        tag_sample_columns = get_table_columns(conn, "tag_samples")
        missing_columns = REQUIRED_TAG_SAMPLE_COLUMNS - tag_sample_columns
        if missing_columns:
            return fail(f"Missing required tag_samples columns: {sorted(missing_columns)}")

        existing_indexes = get_index_names(conn)
        missing_indexes = REQUIRED_INDEXES - existing_indexes
        if missing_indexes:
            return fail(f"Missing required indexes: {sorted(missing_indexes)}")

        machine_names = {
            str(row["machine_name"])
            for row in conn.execute("SELECT machine_name FROM machines").fetchall()
        }
        missing_machines = set(DEFAULT_MACHINE_NAMES) - machine_names
        if missing_machines:
            return fail(f"Configured machines missing from DB: {sorted(missing_machines)}")

        pinch20 = MACHINE_AUTH_CONFIG["Pinch 20"]
        if pinch20.get("auth_mode") != AUTH_MODE_USERNAME_BLANK_BASIC256_TOKEN:
            return fail("Pinch 20 auth mode is not username_blank_basic256_token")
        if MACHINE_AUTH_CONFIG["Pinch 16"].get("auth_mode") != AUTH_MODE_ANONYMOUS:
            return fail("Pinch 16 auth mode is not anonymous")
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
