from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from config import (
    BAD_SAMPLE_RETENTION_DAYS,
    CLEANUP_INTERVAL_MINUTES,
    MYSQL_DATABASE,
    POLL_RUN_RETENTION_DAYS,
    SAMPLE_RETENTION_DAYS,
)
from db import Connection, from_db_datetime, get_connection, get_db_stats, get_tables, to_db_datetime, to_iso_utc, utc_now

RECENT_WINDOW_SECONDS = 90
STALE_THRESHOLD_SECONDS = 150
RECENT_ERROR_LIMIT = 20
RECENT_POLL_LIMIT = 10
REQUIRED_TABLES = {"machines", "tags", "tag_samples", "poll_runs"}


def age_seconds(value: datetime | str | None, now_utc: datetime) -> float | None:
    dt: datetime | None = None
    if isinstance(value, datetime):
        dt = from_db_datetime(value)
    elif isinstance(value, str):
        try:
            normalized = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
        except ValueError:
            dt = None
    if dt is None:
        return None
    return max(0.0, (now_utc - dt).total_seconds())


def table_exists(conn: Connection, table_name: str) -> bool:
    return table_name in get_tables(conn)


def get_dashboard_status() -> dict[str, Any]:
    generated_at = utc_now()
    base_response = {
        "overall": {
            "status": "CRITICAL",
            "message": "",
            "generated_at_utc": to_iso_utc(generated_at),
            "db_name": MYSQL_DATABASE,
            "db_size_mb": 0.0,
            "last_poll_finished_at_utc": None,
            "last_poll_age_seconds": None,
            "total_enabled_machines": 0,
            "total_enabled_tags": 0,
            "total_sample_rows": 0,
            "total_good_sample_rows": 0,
            "total_bad_sample_rows": 0,
            "oldest_sample_utc": None,
            "newest_sample_utc": None,
            "poll_run_count": 0,
            "recent_good_samples": 0,
            "recent_bad_samples": 0,
            "latest_poll_duration_seconds": None,
            "sample_retention_days": SAMPLE_RETENTION_DAYS,
            "bad_sample_retention_days": BAD_SAMPLE_RETENTION_DAYS,
            "poll_run_retention_days": POLL_RUN_RETENTION_DAYS,
            "cleanup_interval_minutes": CLEANUP_INTERVAL_MINUTES,
        },
        "machines": [],
        "recent_poll_runs": [],
        "recent_errors": [],
    }

    try:
        conn = get_connection()
    except Exception as exc:
        base_response["overall"]["message"] = f"Dashboard cannot read DB: {exc}"
        return base_response

    try:
        if not REQUIRED_TABLES.issubset(get_tables(conn)):
            base_response["overall"]["message"] = "Database is missing required collector tables."
            return base_response

        stats = get_db_stats(conn=conn)
        latest_poll = get_latest_poll_run(conn)
        recent_cutoff = generated_at - timedelta(seconds=RECENT_WINDOW_SECONDS)
        recent_sample_totals = get_recent_sample_totals(conn, recent_cutoff)
        machines = get_machine_status_rows(
            conn,
            generated_at,
            recent_cutoff,
            use_machine_poll_runs=table_exists(conn, "machine_poll_runs"),
        )
        recent_polls = get_recent_poll_runs(conn, generated_at)
        recent_errors = get_recent_errors(conn, generated_at)

        overall = base_response["overall"]
        overall["db_size_mb"] = stats["db_size_mb"]
        overall["total_enabled_machines"] = stats["enabled_machine_count"]
        overall["total_enabled_tags"] = stats["enabled_tag_count"]
        overall["total_sample_rows"] = stats["total_sample_rows"]
        overall["total_good_sample_rows"] = stats["good_sample_rows"]
        overall["total_bad_sample_rows"] = stats["bad_sample_rows"]
        overall["oldest_sample_utc"] = stats["oldest_sampled_at_utc"]
        overall["newest_sample_utc"] = stats["newest_sampled_at_utc"]
        overall["poll_run_count"] = stats["total_poll_runs"]
        overall["recent_good_samples"] = recent_sample_totals["good"]
        overall["recent_bad_samples"] = recent_sample_totals["bad"]

        if latest_poll:
            overall["last_poll_finished_at_utc"] = to_iso_utc(latest_poll["finished_at_utc"])
            overall["last_poll_age_seconds"] = round(
                age_seconds(latest_poll["finished_at_utc"], generated_at) or 0.0,
                1,
            )
            overall["latest_poll_duration_seconds"] = float(latest_poll["duration_seconds"] or 0.0)

        overall_status, overall_message = determine_overall_status(
            machines=machines,
            latest_poll_finished_at_utc=overall["last_poll_finished_at_utc"],
            latest_poll_age_seconds=overall["last_poll_age_seconds"],
            enabled_machine_count=stats["enabled_machine_count"],
        )
        overall["status"] = overall_status
        overall["message"] = overall_message
        base_response["machines"] = machines
        base_response["recent_poll_runs"] = recent_polls
        base_response["recent_errors"] = recent_errors
        return base_response
    except Exception as exc:
        base_response["overall"]["message"] = f"Dashboard query failure: {exc}"
        return base_response
    finally:
        conn.close()


def get_latest_poll_run(conn: Connection) -> dict[str, Any] | None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT started_at_utc, finished_at_utc, duration_seconds, machines_attempted, machines_ok,
                   machines_failed, tags_attempted, tags_ok, tags_failed
            FROM poll_runs
            ORDER BY COALESCE(finished_at_utc, started_at_utc) DESC
            LIMIT 1
            """
        )
        return cursor.fetchone()


def get_recent_sample_totals(conn: Connection, recent_cutoff: datetime) -> dict[str, int]:
    cutoff = to_db_datetime(recent_cutoff)
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN quality = 'good' AND sampled_at_utc >= %s THEN 1 ELSE 0 END), 0) AS good,
                COALESCE(SUM(CASE WHEN quality <> 'good' AND sampled_at_utc >= %s THEN 1 ELSE 0 END), 0) AS bad
            FROM tag_samples
            """,
            (cutoff, cutoff),
        )
        row = cursor.fetchone()
    return {"good": int(row["good"] or 0), "bad": int(row["bad"] or 0)}


def get_machine_status_rows(
    conn: Connection,
    now_utc: datetime,
    recent_cutoff: datetime,
    use_machine_poll_runs: bool,
) -> list[dict[str, Any]]:
    sql = f"""
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
            SELECT machine_id, MAX(sampled_at_utc) AS latest_sample_utc
            FROM tag_samples
            GROUP BY machine_id
        ),
        latest_good_samples AS (
            SELECT machine_id, MAX(sampled_at_utc) AS latest_good_sample_utc
            FROM tag_samples
            WHERE quality = 'good'
            GROUP BY machine_id
        ),
        recent_counts AS (
            SELECT
                machine_id,
                SUM(CASE WHEN quality = 'good' AND sampled_at_utc >= %s THEN 1 ELSE 0 END) AS recent_good_samples,
                SUM(CASE WHEN quality <> 'good' AND sampled_at_utc >= %s THEN 1 ELSE 0 END) AS recent_bad_samples,
                SUM(CASE WHEN sampled_at_utc >= %s THEN 1 ELSE 0 END) AS recent_total_samples
            FROM tag_samples
            GROUP BY machine_id
        ),
        recent_error_candidates AS (
            SELECT
                ts.machine_id,
                ts.error_text,
                ts.sampled_at_utc,
                ROW_NUMBER() OVER (
                    PARTITION BY ts.machine_id
                    ORDER BY ts.sampled_at_utc DESC, ts.id DESC
                ) AS rn
            FROM tag_samples ts
            WHERE ts.quality <> 'good' AND ts.error_text IS NOT NULL AND ts.error_text <> ''
        ),
        latest_machine_poll_runs AS (
            SELECT
                mpr.machine_id,
                mpr.finished_at_utc,
                mpr.tags_attempted,
                mpr.tags_failed,
                mpr.connection_ok,
                mpr.error_text,
                ROW_NUMBER() OVER (
                    PARTITION BY mpr.machine_id
                    ORDER BY COALESCE(mpr.finished_at_utc, mpr.started_at_utc) DESC, mpr.id DESC
                ) AS rn
            FROM machine_poll_runs mpr
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
            rec.error_text AS top_recent_error,
            {"mpr.tags_attempted AS last_poll_attempted_tags," if use_machine_poll_runs else "NULL AS last_poll_attempted_tags,"}
            {"mpr.tags_failed AS last_poll_failed_tags," if use_machine_poll_runs else "NULL AS last_poll_failed_tags,"}
            {"mpr.connection_ok AS last_poll_connection_ok," if use_machine_poll_runs else "NULL AS last_poll_connection_ok,"}
            {"mpr.error_text AS last_poll_error_text" if use_machine_poll_runs else "NULL AS last_poll_error_text"}
        FROM enabled_machines m
        LEFT JOIN enabled_tags t ON t.machine_id = m.id
        LEFT JOIN latest_samples ls ON ls.machine_id = m.id
        LEFT JOIN latest_good_samples lgs ON lgs.machine_id = m.id
        LEFT JOIN recent_counts rc ON rc.machine_id = m.id
        LEFT JOIN recent_error_candidates rec ON rec.machine_id = m.id AND rec.rn = 1
        {"LEFT JOIN latest_machine_poll_runs mpr ON mpr.machine_id = m.id AND mpr.rn = 1" if use_machine_poll_runs else ""}
        ORDER BY m.machine_name
    """
    with conn.cursor() as cursor:
        cursor.execute(
            sql,
            (to_db_datetime(recent_cutoff), to_db_datetime(recent_cutoff), to_db_datetime(recent_cutoff)),
        )
        rows = cursor.fetchall()

    results: list[dict[str, Any]] = []
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
            last_poll_connection_ok=row["last_poll_connection_ok"],
            last_poll_error_text=row["last_poll_error_text"],
        )
        results.append(
            {
                "machine_name": row["machine_name"],
                "endpoint_url": row["endpoint_url"],
                "status": status,
                "status_reason": reason,
                "enabled_tags": int(row["enabled_tags"] or 0),
                "latest_sample_utc": to_iso_utc(row["latest_sample_utc"]),
                "latest_sample_age_seconds": round(latest_sample_age, 1) if latest_sample_age is not None else None,
                "latest_good_sample_utc": to_iso_utc(row["latest_good_sample_utc"]),
                "latest_good_age_seconds": round(latest_good_age, 1) if latest_good_age is not None else None,
                "recent_good_samples": recent_good,
                "recent_bad_samples": recent_bad,
                "recent_total_samples": recent_total,
                "recent_success_rate": success_rate,
                "last_poll_attempted_tags": int(row["last_poll_attempted_tags"] or row["enabled_tags"] or 0),
                "last_poll_failed_tags": int(row["last_poll_failed_tags"] or 0),
                "top_recent_error": row["last_poll_error_text"] or row["top_recent_error"],
            }
        )
    return results


def determine_machine_status(
    latest_good_age_seconds: float | None,
    recent_good_samples: int,
    recent_bad_samples: int,
    recent_success_rate: float,
    last_poll_connection_ok: int | None,
    last_poll_error_text: str | None,
) -> tuple[str, str]:
    if last_poll_connection_ok == 0:
        return ("CRITICAL", last_poll_error_text or "Latest machine poll could not connect.")
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


def get_recent_poll_runs(conn: Connection, now_utc: datetime) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT started_at_utc, finished_at_utc, duration_seconds, machines_attempted, machines_ok,
                   machines_failed, tags_attempted, tags_ok, tags_failed
            FROM poll_runs
            ORDER BY COALESCE(finished_at_utc, started_at_utc) DESC
            LIMIT %s
            """,
            (RECENT_POLL_LIMIT,),
        )
        rows = cursor.fetchall()
    return [
        {
            "started_at_utc": to_iso_utc(row["started_at_utc"]),
            "finished_at_utc": to_iso_utc(row["finished_at_utc"]),
            "finished_age_seconds": round(age_seconds(row["finished_at_utc"], now_utc) or 0.0, 1)
            if row["finished_at_utc"]
            else None,
            "duration_seconds": float(row["duration_seconds"] or 0.0) if row["duration_seconds"] is not None else None,
            "machines_attempted": int(row["machines_attempted"] or 0),
            "machines_ok": int(row["machines_ok"] or 0),
            "machines_failed": int(row["machines_failed"] or 0),
            "tags_attempted": int(row["tags_attempted"] or 0),
            "tags_ok": int(row["tags_ok"] or 0),
            "tags_failed": int(row["tags_failed"] or 0),
        }
        for row in rows
    ]


def get_recent_errors(conn: Connection, now_utc: datetime) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            WITH recent_errors AS (
                SELECT
                    ts.id,
                    ts.sampled_at_utc,
                    ts.machine_id,
                    ts.tag_id,
                    ts.error_text,
                    ROW_NUMBER() OVER (
                        PARTITION BY ts.machine_id, ts.tag_id, ts.error_text
                        ORDER BY ts.sampled_at_utc DESC, ts.id DESC
                    ) AS rn
                FROM tag_samples ts
                WHERE ts.quality <> 'good' AND ts.error_text IS NOT NULL AND ts.error_text <> ''
            )
            SELECT
                re.sampled_at_utc,
                m.machine_name,
                COALESCE(t.display_name, t.browse_name, t.node_id) AS tag_name,
                t.node_id,
                re.error_text
            FROM recent_errors re
            JOIN machines m ON m.id = re.machine_id
            JOIN tags t ON t.id = re.tag_id
            WHERE re.rn = 1
            ORDER BY re.sampled_at_utc DESC
            LIMIT %s
            """,
            (RECENT_ERROR_LIMIT,),
        )
        rows = cursor.fetchall()
    return [
        {
            "timestamp_utc": to_iso_utc(row["sampled_at_utc"]),
            "age_seconds": round(age_seconds(row["sampled_at_utc"], now_utc) or 0.0, 1)
            if row["sampled_at_utc"]
            else None,
            "machine_name": row["machine_name"],
            "tag_name": row["tag_name"],
            "node_id": row["node_id"],
            "error_text": row["error_text"],
        }
        for row in rows
    ]
