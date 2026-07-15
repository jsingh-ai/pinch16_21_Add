from __future__ import annotations

from config import (
    ACCEPTED_AUTH_MODES,
    AUTH_MODE_ANONYMOUS,
    BAD_SAMPLE_RETENTION_DAYS,
    CLEANUP_INTERVAL_MINUTES,
    DEFAULT_MACHINE_NAMES,
    POLL_RUN_RETENTION_DAYS,
    SAMPLE_RETENTION_DAYS,
    get_all_machine_configs,
)
from db import get_connection, get_index_names, get_table_column_types, get_tables

REQUIRED_TABLES = {"machines", "tags", "tag_samples", "poll_runs", "machine_poll_runs"}
REQUIRED_TAG_SAMPLE_COLUMNS = {
    "id", "tag_id", "machine_id", "sampled_at_utc", "source_timestamp_utc",
    "server_timestamp_utc", "value_numeric", "value_text", "quality", "status_code",
    "error_text", "created_at_utc",
}
REQUIRED_INDEXES = {
    "uq_machines_machine_name", "idx_machines_enabled", "uq_tags_machine_node",
    "idx_tags_machine_enabled", "idx_tags_machine_id", "idx_tags_display_name",
    "idx_samples_machine_time", "idx_samples_tag_time", "idx_samples_quality_time",
    "idx_samples_machine_quality_time", "idx_samples_created_at", "idx_poll_runs_started",
    "idx_poll_runs_finished", "idx_machine_poll_machine_finished",
    "idx_machine_poll_poll_run", "idx_machine_poll_connection",
}


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    """Run read-only schema/configuration checks; never initialize or alter tables."""
    if DEFAULT_MACHINE_NAMES != ["Press 14", "Press 15"]:
        return fail(f"Unexpected default machines: {DEFAULT_MACHINE_NAMES}")
    for machine_name, machine_config in get_all_machine_configs().items():
        if machine_config["auth_mode"] not in ACCEPTED_AUTH_MODES:
            return fail(f"Unsupported auth mode for {machine_name}")
    if SAMPLE_RETENTION_DAYS <= 0 or BAD_SAMPLE_RETENTION_DAYS < SAMPLE_RETENTION_DAYS:
        return fail("Retention settings are not sane")
    if POLL_RUN_RETENTION_DAYS <= 0 or CLEANUP_INTERVAL_MINUTES <= 0:
        return fail("Poll run retention or cleanup interval is invalid")

    conn = get_connection()
    try:
        tables = get_tables(conn)
        missing_tables = REQUIRED_TABLES - tables
        if missing_tables:
            return fail(f"Missing required tables: {sorted(missing_tables)}")
        columns = get_table_column_types(conn, "tag_samples")
        missing_columns = REQUIRED_TAG_SAMPLE_COLUMNS - set(columns)
        if missing_columns:
            return fail(f"Missing required tag_samples columns: {sorted(missing_columns)}")
        indexes = get_index_names(conn)
        missing_indexes = REQUIRED_INDEXES - indexes
        if missing_indexes:
            return fail(f"Missing required indexes: {sorted(missing_indexes)}")
        with conn.cursor() as cursor:
            cursor.execute("SELECT machine_name FROM machines WHERE enabled=1")
            machine_names = {str(row["machine_name"]) for row in cursor.fetchall()}
        missing_machines = set(DEFAULT_MACHINE_NAMES) - machine_names
        if missing_machines:
            return fail(f"Configured presses missing from DB: {sorted(missing_machines)}")
        print("PASS: read-only smoke checks succeeded")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
