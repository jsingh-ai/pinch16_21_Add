from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

RECENT_WINDOW_SECONDS = 90
STALE_THRESHOLD_SECONDS = 150
EXPECTED_POLL_INTERVAL_SECONDS = 60
RECENT_ERROR_LIMIT = 20
RECENT_POLL_LIMIT = 10
REQUIRED_TABLES = {"machines", "tags", "tag_samples", "poll_runs"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def parse_iso_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def age_seconds(value: str | None, now_utc: datetime) -> float | None:
    dt = parse_iso_timestamp(value)
    if dt is None:
        return None
    return max(0.0, (now_utc - dt).total_seconds())


def get_read_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", timeout=5.0, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA query_only=ON")
    return conn


def tables_exist(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN (?, ?, ?, ?)",
        tuple(sorted(REQUIRED_TABLES)),
    ).fetchall()
    present = {str(row["name"]) for row in rows}
    return REQUIRED_TABLES.issubset(present)


def get_dashboard_status(db_path: str) -> dict[str, Any]:
    db_file = Path(db_path)
    generated_at = utc_now()
    base_response = {
        "overall": {
            "status": "CRITICAL",
            "message": "",
            "generated_at_utc": generated_at.replace(microsecond=0).isoformat(),
            "db_path": str(db_file),
            "last_poll_finished_at_utc": None,
            "last_poll_age_seconds": None,
            "total_enabled_machines": 0,
            "total_enabled_tags": 0,
            "recent_good_samples": 0,
            "recent_bad_samples": 0,
            "latest_poll_duration_seconds": None,
        },
        "machines": [],
        "recent_poll_runs": [],
        "recent_errors": [],
    }

    if not db_file.exists():
        base_response["overall"]["message"] = "Dashboard cannot read DB: file does not exist."
        return base_response

    try:
        conn = get_read_connection(str(db_file))
    except sqlite3.Error as exc:
        base_response["overall"]["message"] = f"Dashboard cannot read DB: {exc}"
        return base_response

    try:
        if not tables_exist(conn):
            base_response["overall"]["message"] = "Database is missing required collector tables."
            return base_response

        enabled_machine_count = get_enabled_machine_count(conn)
        enabled_tag_count = get_enabled_tag_count(conn)
        latest_poll = get_latest_poll_run(conn)
        recent_cutoff_utc = (generated_at - timedelta(seconds=RECENT_WINDOW_SECONDS)).replace(microsecond=0)
        recent_cutoff_iso = recent_cutoff_utc.isoformat()
        recent_sample_totals = get_recent_sample_totals(conn, recent_cutoff_iso)
        machines = get_machine_status_rows(conn, generated_at, recent_cutoff_iso)
        recent_polls = get_recent_poll_runs(conn, generated_at)
        recent_errors = get_recent_errors(conn, generated_at)

        overall = base_response["overall"]
        overall["last_poll_finished_at_utc"] = latest_poll["finished_at_utc"] if latest_poll else None
        overall["last_poll_age_seconds"] = (
            round(age_seconds(latest_poll["finished_at_utc"], generated_at), 1)
            if latest_poll and latest_poll["finished_at_utc"]
            else None
        )
        overall["total_enabled_machines"] = enabled_machine_count
        overall["total_enabled_tags"] = enabled_tag_count
        overall["recent_good_samples"] = recent_sample_totals["good"]
        overall["recent_bad_samples"] = recent_sample_totals["bad"]
        overall["latest_poll_duration_seconds"] = (
            latest_poll["duration_seconds"] if latest_poll else None
        )

        overall_status, overall_message = determine_overall_status(
            machines=machines,
            latest_poll_finished_at_utc=overall["last_poll_finished_at_utc"],
            latest_poll_age_seconds=overall["last_poll_age_seconds"],
            enabled_machine_count=enabled_machine_count,
        )
        overall["status"] = overall_status
        overall["message"] = overall_message

        base_response["machines"] = machines
        base_response["recent_poll_runs"] = recent_polls
        base_response["recent_errors"] = recent_errors
        return base_response
    except sqlite3.Error as exc:
        base_response["overall"]["message"] = f"Dashboard query failure: {exc}"
        return base_response
    finally:
        conn.close()


def get_enabled_machine_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS count FROM machines WHERE enabled = 1").fetchone()
    return int(row["count"]) if row else 0


def get_enabled_tag_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS count FROM tags WHERE enabled = 1").fetchone()
    return int(row["count"]) if row else 0


def get_latest_poll_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT started_at_utc, finished_at_utc, duration_seconds, machines_attempted, machines_ok,
               machines_failed, tags_attempted, tags_ok, tags_failed
        FROM poll_runs
        ORDER BY COALESCE(finished_at_utc, started_at_utc) DESC
        LIMIT 1
        """
    ).fetchone()


def get_recent_sample_totals(conn: sqlite3.Connection, recent_cutoff_iso: str) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN quality = 'good' AND ts_utc >= ? THEN 1 ELSE 0 END), 0) AS good,
            COALESCE(SUM(CASE WHEN quality = 'bad' AND ts_utc >= ? THEN 1 ELSE 0 END), 0) AS bad
        FROM tag_samples
        """,
        (recent_cutoff_iso, recent_cutoff_iso),
    ).fetchone()
    return {
        "good": int(row["good"]) if row else 0,
        "bad": int(row["bad"]) if row else 0,
    }


def get_machine_status_rows(
    conn: sqlite3.Connection,
    now_utc: datetime,
    recent_cutoff_iso: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH enabled_machines AS (
            SELECT id, machine_name, endpoint_url
            FROM machines
            WHERE enabled = 1
        ),
        enabled_tags AS (
            SELECT machine_id, COUNT(*) AS enabled_tags
            FROM tags
            WHERE enabled = 1
            GROUP BY machine_id
        ),
        latest_samples AS (
            SELECT machine_id, MAX(ts_utc) AS latest_sample_utc
            FROM tag_samples
            GROUP BY machine_id
        ),
        latest_good_samples AS (
            SELECT machine_id, MAX(ts_utc) AS latest_good_sample_utc
            FROM tag_samples
            WHERE quality = 'good'
            GROUP BY machine_id
        ),
        recent_counts AS (
            SELECT
                machine_id,
                SUM(CASE WHEN quality = 'good' AND ts_utc >= ? THEN 1 ELSE 0 END) AS recent_good_samples,
                SUM(CASE WHEN quality = 'bad' AND ts_utc >= ? THEN 1 ELSE 0 END) AS recent_bad_samples,
                SUM(CASE WHEN ts_utc >= ? THEN 1 ELSE 0 END) AS recent_total_samples
            FROM tag_samples
            GROUP BY machine_id
        ),
        recent_error_candidates AS (
            SELECT
                ts.machine_id,
                ts.error_text,
                ts.ts_utc,
                ROW_NUMBER() OVER (
                    PARTITION BY ts.machine_id
                    ORDER BY ts.ts_utc DESC, ts.id DESC
                ) AS rn
            FROM tag_samples ts
            WHERE ts.quality = 'bad' AND ts.error_text IS NOT NULL AND ts.error_text <> ''
        )
        SELECT
            m.id,
            m.machine_name,
            m.endpoint_url,
            COALESCE(t.enabled_tags, 0) AS enabled_tags,
            ls.latest_sample_utc,
            lgs.latest_good_sample_utc,
            COALESCE(rc.recent_good_samples, 0) AS recent_good_samples,
            COALESCE(rc.recent_bad_samples, 0) AS recent_bad_samples,
            COALESCE(rc.recent_total_samples, 0) AS recent_total_samples,
            rec.error_text AS top_recent_error
        FROM enabled_machines m
        LEFT JOIN enabled_tags t ON t.machine_id = m.id
        LEFT JOIN latest_samples ls ON ls.machine_id = m.id
        LEFT JOIN latest_good_samples lgs ON lgs.machine_id = m.id
        LEFT JOIN recent_counts rc ON rc.machine_id = m.id
        LEFT JOIN recent_error_candidates rec ON rec.machine_id = m.id AND rec.rn = 1
        ORDER BY m.machine_name
        """,
        (recent_cutoff_iso, recent_cutoff_iso, recent_cutoff_iso),
    ).fetchall()

    machine_rows: list[dict[str, Any]] = []
    for row in rows:
        latest_good_age = age_seconds(row["latest_good_sample_utc"], now_utc)
        latest_sample_age = age_seconds(row["latest_sample_utc"], now_utc)
        recent_total = int(row["recent_total_samples"] or 0)
        recent_good = int(row["recent_good_samples"] or 0)
        recent_bad = int(row["recent_bad_samples"] or 0)
        success_rate = round((recent_good / recent_total) * 100, 1) if recent_total > 0 else 0.0
        status, reason = determine_machine_status(
            latest_good_age_seconds=latest_good_age,
            recent_good_samples=recent_good,
            recent_bad_samples=recent_bad,
            recent_success_rate=success_rate,
        )
        machine_rows.append(
            {
                "machine_name": row["machine_name"],
                "endpoint_url": row["endpoint_url"],
                "status": status,
                "status_reason": reason,
                "enabled_tags": int(row["enabled_tags"] or 0),
                "latest_sample_utc": row["latest_sample_utc"],
                "latest_sample_age_seconds": round(latest_sample_age, 1) if latest_sample_age is not None else None,
                "latest_good_sample_utc": row["latest_good_sample_utc"],
                "latest_good_age_seconds": round(latest_good_age, 1) if latest_good_age is not None else None,
                "recent_good_samples": recent_good,
                "recent_bad_samples": recent_bad,
                "recent_total_samples": recent_total,
                "recent_success_rate": success_rate,
                "last_poll_attempted_tags": int(row["enabled_tags"] or 0),
                "last_poll_failed_tags": recent_bad,
                "top_recent_error": row["top_recent_error"],
                "last_updated_display": row["latest_sample_utc"] or "Never",
            }
        )
    return machine_rows


def determine_machine_status(
    latest_good_age_seconds: float | None,
    recent_good_samples: int,
    recent_bad_samples: int,
    recent_success_rate: float,
) -> tuple[str, str]:
    if latest_good_age_seconds is None or recent_good_samples == 0:
        return ("CRITICAL", "No recent successful data.")
    if latest_good_age_seconds > STALE_THRESHOLD_SECONDS:
        return ("CRITICAL", "Latest good sample is stale.")
    if recent_success_rate < 90.0:
        return ("CRITICAL", "Recent success rate below 90%.")
    if recent_bad_samples > 0 or recent_success_rate < 98.0:
        return ("WARNING", "Recent read errors detected.")
    return ("GOOD", "Recent inserts healthy.")


def determine_overall_status(
    machines: list[dict[str, Any]],
    latest_poll_finished_at_utc: str | None,
    latest_poll_age_seconds: float | None,
    enabled_machine_count: int,
) -> tuple[str, str]:
    if enabled_machine_count == 0:
        return ("CRITICAL", "No enabled machines configured.")
    if latest_poll_finished_at_utc is None:
        return ("CRITICAL", "No poll history available yet.")
    if latest_poll_age_seconds is None or latest_poll_age_seconds > STALE_THRESHOLD_SECONDS:
        return ("CRITICAL", "Latest poll is stale.")

    statuses = {machine["status"] for machine in machines}
    if "CRITICAL" in statuses:
        return ("CRITICAL", "One or more machines are critical.")
    if "WARNING" in statuses:
        return ("WARNING", "One or more machines have recent errors.")
    return ("GOOD", "All enabled machines have healthy recent inserts.")


def get_recent_poll_runs(conn: sqlite3.Connection, now_utc: datetime) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT started_at_utc, finished_at_utc, duration_seconds, machines_attempted, machines_ok,
               machines_failed, tags_attempted, tags_ok, tags_failed
        FROM poll_runs
        ORDER BY COALESCE(finished_at_utc, started_at_utc) DESC
        LIMIT ?
        """,
        (RECENT_POLL_LIMIT,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "started_at_utc": row["started_at_utc"],
                "finished_at_utc": row["finished_at_utc"],
                "finished_age_seconds": round(age_seconds(row["finished_at_utc"], now_utc), 1)
                if row["finished_at_utc"]
                else None,
                "duration_seconds": row["duration_seconds"],
                "machines_attempted": int(row["machines_attempted"] or 0),
                "machines_ok": int(row["machines_ok"] or 0),
                "machines_failed": int(row["machines_failed"] or 0),
                "tags_attempted": int(row["tags_attempted"] or 0),
                "tags_ok": int(row["tags_ok"] or 0),
                "tags_failed": int(row["tags_failed"] or 0),
            }
        )
    return result


def get_recent_errors(conn: sqlite3.Connection, now_utc: datetime) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH recent_errors AS (
            SELECT
                ts.id,
                ts.ts_utc,
                ts.machine_id,
                ts.tag_id,
                ts.error_text,
                ROW_NUMBER() OVER (
                    PARTITION BY ts.machine_id, ts.tag_id, ts.error_text
                    ORDER BY ts.ts_utc DESC, ts.id DESC
                ) AS rn
            FROM tag_samples ts
            WHERE ts.quality = 'bad' AND ts.error_text IS NOT NULL AND ts.error_text <> ''
        )
        SELECT
            re.ts_utc,
            m.machine_name,
            COALESCE(t.display_name, t.browse_name, t.node_id) AS tag_name,
            t.node_id,
            re.error_text
        FROM recent_errors re
        JOIN machines m ON m.id = re.machine_id
        JOIN tags t ON t.id = re.tag_id
        WHERE re.rn = 1
        ORDER BY re.ts_utc DESC
        LIMIT ?
        """,
        (RECENT_ERROR_LIMIT,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "timestamp_utc": row["ts_utc"],
                "age_seconds": round(age_seconds(row["ts_utc"], now_utc), 1) if row["ts_utc"] else None,
                "machine_name": row["machine_name"],
                "tag_name": row["tag_name"],
                "node_id": row["node_id"],
                "error_text": row["error_text"],
            }
        )
    return result
