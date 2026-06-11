from __future__ import annotations

import argparse
import logging
import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from opcua import Client

from config import (
    AUTH_MODE_USERNAME_BLANK_BASIC256_TOKEN,
    CLEANUP_INTERVAL_MINUTES,
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_SESSION_TIMEOUT_MS,
    DEFAULT_TAG_FAILURE_LOG_SAMPLE,
    MACHINE_AUTH_CONFIG,
    get_machine_config,
)
from db import cleanup_old_data, transaction
from opcua_auth import patch_blank_basic256_username_token

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class MachineRecord:
    id: int
    machine_name: str
    endpoint_url: str | None
    auth_mode: str | None


@dataclass(slots=True)
class TagRecord:
    id: int
    machine_id: int
    node_id: str
    opc_path: str | None
    display_name: str | None


@dataclass(slots=True)
class MachinePollResult:
    machine_id: int
    machine_name: str
    endpoint_url: str | None
    started_at_utc: str
    finished_at_utc: str
    duration_seconds: float
    tags_attempted: int
    tags_ok: int
    tags_failed: int
    connection_ok: bool
    error_text: str | None
    sampled_failures: list[str]


@dataclass(slots=True)
class PollRunStats:
    started_at_utc: str
    finished_at_utc: str
    duration_seconds: float
    machines_attempted: int
    machines_ok: int
    machines_failed: int
    tags_attempted: int
    tags_ok: int
    tags_failed: int
    failed_machines: list[str]
    machine_tag_failures: list[str]
    machine_results: list[MachinePollResult]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OPC UA tag collector")
    parser.add_argument("--once", action="store_true", help="Run one poll cycle and exit")
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="Polling interval in seconds",
    )
    parser.add_argument(
        "--no-sleep-align",
        action="store_true",
        help="Do not align polling to interval boundaries",
    )
    parser.add_argument(
        "--init-only",
        action="store_true",
        help="Initialize schema and import tags without polling",
    )
    parser.add_argument("--machine", help="Poll only a specific machine_name")
    parser.add_argument("--cleanup-now", action="store_true", help="Run retention cleanup once and exit")
    parser.add_argument("--clear-poll-runs", action="store_true", help="Clear poll_runs only; requires --yes")
    parser.add_argument("--clear-bad-samples", action="store_true", help="Clear bad tag_samples only; requires --yes")
    parser.add_argument("--clear-all-samples", action="store_true", help="Clear all tag_samples; requires --yes")
    parser.add_argument(
        "--clear-monitoring-data",
        action="store_true",
        help="Clear tag_samples and poll_runs; requires --yes",
    )
    parser.add_argument("--checkpoint-db", action="store_true", help="Run WAL checkpoint and exit")
    parser.add_argument("--truncate", action="store_true", help="Use TRUNCATE checkpoint mode with --checkpoint-db")
    parser.add_argument("--db-stats", action="store_true", help="Show SQLite growth/retention stats and exit")
    parser.add_argument("--show-config", action="store_true", help="Show resolved collector config and exit")
    parser.add_argument("--allow-multiple", action="store_true", help="Allow multiple collector processes")
    parser.add_argument("--yes", action="store_true", help="Confirm destructive clear commands")
    return parser


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_enabled_machines(conn: sqlite3.Connection, machine_name: str | None = None) -> list[MachineRecord]:
    if machine_name:
        rows = conn.execute(
            """
            SELECT id, machine_name, endpoint_url, auth_mode
            FROM machines
            WHERE enabled = 1 AND machine_name = ?
            ORDER BY machine_name
            """,
            (machine_name,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, machine_name, endpoint_url, auth_mode
            FROM machines
            WHERE enabled = 1
            ORDER BY machine_name
            """
        ).fetchall()
    return [
        MachineRecord(
            id=int(row["id"]),
            machine_name=str(row["machine_name"]),
            endpoint_url=row["endpoint_url"],
            auth_mode=row["auth_mode"],
        )
        for row in rows
    ]


def load_enabled_tags_for_machine(conn: sqlite3.Connection, machine_id: int) -> list[TagRecord]:
    rows = conn.execute(
        """
        SELECT id, machine_id, node_id, opc_path, display_name
        FROM tags
        WHERE enabled = 1 AND machine_id = ?
        ORDER BY id
        """,
        (machine_id,),
    ).fetchall()
    return [
        TagRecord(
            id=int(row["id"]),
            machine_id=int(row["machine_id"]),
            node_id=str(row["node_id"]),
            opc_path=row["opc_path"],
            display_name=row["display_name"],
        )
        for row in rows
    ]


def create_client(machine: MachineRecord) -> Client:
    if not machine.endpoint_url:
        raise ValueError(f"Machine {machine.machine_name} has no endpoint_url configured")

    resolved_config = get_machine_config(machine.machine_name, machine.endpoint_url)
    endpoint_url = str(resolved_config.get("endpoint_url") or machine.endpoint_url)
    client = Client(endpoint_url, timeout=DEFAULT_CONNECT_TIMEOUT_SECONDS)
    client.session_timeout = DEFAULT_SESSION_TIMEOUT_MS

    application_uri = str(resolved_config.get("application_uri") or "").strip()
    if application_uri:
        client.application_uri = application_uri

    if machine.auth_mode == AUTH_MODE_USERNAME_BLANK_BASIC256_TOKEN:
        patch_blank_basic256_username_token(
            client=client,
            username=str(resolved_config.get("username", "")),
            password=str(resolved_config.get("password", "")),
            policy_id=str(resolved_config.get("username_token_policy_id", "3")),
            policy_uri=str(
                resolved_config.get(
                    "username_token_policy_uri",
                    "http://opcfoundation.org/UA/SecurityPolicy#Basic256",
                )
            ),
        )

    return client


def normalize_value(value: Any) -> tuple[str, float | None]:
    if value is None:
        return ("", None)
    if isinstance(value, bool):
        return (str(value), 1.0 if value else 0.0)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (str(value), float(value))
    text = str(value)
    try:
        numeric_value = float(text)
    except (TypeError, ValueError):
        numeric_value = None
    return (text, numeric_value)


def _format_status_code(status_code: Any) -> str | None:
    if status_code is None:
        return None
    if hasattr(status_code, "name"):
        return str(status_code.name)
    return str(status_code)


def _status_code_is_good(status_code: Any) -> bool | None:
    if status_code is None:
        return None
    is_good = getattr(status_code, "is_good", None)
    if callable(is_good):
        try:
            return bool(is_good())
        except Exception:
            return None
    text = str(status_code)
    if "Good" in text:
        return True
    if "Bad" in text or "Uncertain" in text:
        return False
    return None


def _timestamp_to_iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "tzinfo") and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if hasattr(value, "astimezone"):
        value = value.astimezone(timezone.utc)
    if hasattr(value, "isoformat"):
        return value.replace(microsecond=0).isoformat()
    return str(value)


def read_node_sample(node: Any) -> tuple[Any, str | None, str | None, str | None, str | None]:
    try:
        data_value = node.get_data_value()
        variant = getattr(data_value, "Value", None)
        value = getattr(variant, "Value", None) if variant is not None else None
        status_code = getattr(data_value, "StatusCode", None)
        return (
            value,
            _format_status_code(status_code),
            _timestamp_to_iso(getattr(data_value, "SourceTimestamp", None)),
            _timestamp_to_iso(getattr(data_value, "ServerTimestamp", None)),
            None,
        )
    except Exception:
        value = node.get_value()
        return (value, None, None, None, None)


def poll_machine(conn: sqlite3.Connection, machine: MachineRecord, poll_started_ts: str) -> MachinePollResult:
    machine_started_monotonic = time.monotonic()
    machine_started_ts = utc_now_iso()
    tags = load_enabled_tags_for_machine(conn, machine.id)
    if not tags:
        LOGGER.info("Skipping %s because no enabled tags were found", machine.machine_name)
        finished_ts = utc_now_iso()
        return MachinePollResult(
            machine_id=machine.id,
            machine_name=machine.machine_name,
            endpoint_url=machine.endpoint_url,
            started_at_utc=machine_started_ts,
            finished_at_utc=finished_ts,
            duration_seconds=time.monotonic() - machine_started_monotonic,
            tags_attempted=0,
            tags_ok=0,
            tags_failed=0,
            connection_ok=True,
            error_text=None,
            sampled_failures=[],
        )

    rows_to_insert: list[tuple[object, ...]] = []
    tags_ok = 0
    tags_failed = 0
    sampled_failures: list[str] = []
    LOGGER.info(
        "Machine start %s endpoint=%s tags=%s",
        machine.machine_name,
        machine.endpoint_url,
        len(tags),
    )

    client = create_client(machine)
    try:
        client.connect()
        LOGGER.info("Machine connected %s", machine.machine_name)

        for tag in tags:
            try:
                node = client.get_node(tag.node_id)
                value, status_code, source_ts, server_ts, error_text = read_node_sample(node)
                value_text, value_numeric = normalize_value(value)
                is_good_status = _status_code_is_good(status_code)
                quality = "good" if is_good_status is not False else "bad"
                derived_error_text = error_text
                if quality != "good":
                    derived_error_text = f"StatusCode {status_code}" if status_code else "Read returned non-good status"
                    tags_failed += 1
                    if len(sampled_failures) < DEFAULT_TAG_FAILURE_LOG_SAMPLE:
                        sampled_failures.append(f"{tag.display_name or tag.node_id}: {derived_error_text}")
                else:
                    tags_ok += 1

                rows_to_insert.append(
                    (
                        tag.id,
                        machine.id,
                        poll_started_ts,
                        value_text,
                        value_numeric,
                        quality,
                        derived_error_text,
                        status_code,
                        source_ts,
                        server_ts,
                    )
                )
            except Exception as exc:
                rows_to_insert.append(
                    (
                        tag.id,
                        machine.id,
                        poll_started_ts,
                        None,
                        None,
                        "bad",
                        str(exc),
                        None,
                        None,
                        None,
                    )
                )
                tags_failed += 1
                if len(sampled_failures) < DEFAULT_TAG_FAILURE_LOG_SAMPLE:
                    sampled_failures.append(f"{tag.display_name or tag.node_id}: {exc}")
    finally:
        try:
            client.disconnect()
            LOGGER.info("Machine disconnected %s", machine.machine_name)
        except Exception as exc:
            error_text = str(exc)
            if "10038" in error_text:
                LOGGER.debug("Disconnect skipped for %s after failed connect/auth", machine.machine_name)
            else:
                LOGGER.warning("Disconnect failed for %s: %s", machine.machine_name, exc)

    with transaction(conn):
        conn.executemany(
            """
            INSERT INTO tag_samples (
                tag_id,
                machine_id,
                ts_utc,
                value_text,
                value_numeric,
                quality,
                error_text,
                status_code,
                source_timestamp_utc,
                server_timestamp_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows_to_insert,
        )

    finished_ts = utc_now_iso()
    duration_seconds = time.monotonic() - machine_started_monotonic
    LOGGER.info(
        "Machine complete %s tags_attempted=%s tags_ok=%s tags_failed=%s",
        machine.machine_name,
        len(tags),
        tags_ok,
        tags_failed,
    )
    if sampled_failures:
        LOGGER.warning(
            "Machine tag failure samples %s sample_count=%s total_tag_failures=%s samples=%s",
            machine.machine_name,
            len(sampled_failures),
            tags_failed,
            " | ".join(sampled_failures),
        )
    return MachinePollResult(
        machine_id=machine.id,
        machine_name=machine.machine_name,
        endpoint_url=machine.endpoint_url,
        started_at_utc=machine_started_ts,
        finished_at_utc=finished_ts,
        duration_seconds=duration_seconds,
        tags_attempted=len(tags),
        tags_ok=tags_ok,
        tags_failed=tags_failed,
        connection_ok=True,
        error_text=None,
        sampled_failures=sampled_failures,
    )


def run_poll_cycle(conn: sqlite3.Connection, machine_name: str | None = None) -> PollRunStats:
    started_monotonic = time.monotonic()
    started_ts = utc_now_iso()
    machines = load_enabled_machines(conn, machine_name=machine_name)

    machines_attempted = 0
    machines_ok = 0
    machines_failed = 0
    tags_attempted = 0
    tags_ok = 0
    tags_failed = 0
    failed_machines: list[str] = []
    machine_tag_failures: list[str] = []
    machine_results: list[MachinePollResult] = []

    for machine in machines:
        machines_attempted += 1
        machine_started_monotonic = time.monotonic()
        machine_started_ts = utc_now_iso()
        machine_tags = load_enabled_tags_for_machine(conn, machine.id)
        tags_attempted += len(machine_tags)
        try:
            machine_result = poll_machine(conn, machine, started_ts)
        except Exception as exc:
            finished_ts = utc_now_iso()
            duration_seconds = time.monotonic() - machine_started_monotonic
            machines_failed += 1
            tags_failed += len(machine_tags)
            failed_machines.append(machine.machine_name)
            LOGGER.exception("Machine poll failed for %s: %s", machine.machine_name, exc)
            machine_results.append(
                MachinePollResult(
                    machine_id=machine.id,
                    machine_name=machine.machine_name,
                    endpoint_url=machine.endpoint_url,
                    started_at_utc=machine_started_ts,
                    finished_at_utc=finished_ts,
                    duration_seconds=duration_seconds,
                    tags_attempted=len(machine_tags),
                    tags_ok=0,
                    tags_failed=len(machine_tags),
                    connection_ok=False,
                    error_text=str(exc),
                    sampled_failures=[],
                )
            )
            continue

        machines_ok += 1
        tags_ok += machine_result.tags_ok
        tags_failed += machine_result.tags_failed
        if machine_result.tags_failed > 0:
            machine_tag_failures.append(f"{machine.machine_name}:{machine_result.tags_failed}")
        machine_results.append(machine_result)

    finished_ts = utc_now_iso()
    duration = time.monotonic() - started_monotonic
    stats = PollRunStats(
        started_at_utc=started_ts,
        finished_at_utc=finished_ts,
        duration_seconds=duration,
        machines_attempted=machines_attempted,
        machines_ok=machines_ok,
        machines_failed=machines_failed,
        tags_attempted=tags_attempted,
        tags_ok=tags_ok,
        tags_failed=tags_failed,
        failed_machines=failed_machines,
        machine_tag_failures=machine_tag_failures,
        machine_results=machine_results,
    )
    persist_poll_run(conn, stats)
    failed_machines_text = ", ".join(stats.failed_machines) if stats.failed_machines else "none"
    tag_failures_text = ", ".join(stats.machine_tag_failures) if stats.machine_tag_failures else "none"
    LOGGER.info(
        "Poll summary started=%s duration=%.2fs machines attempted=%s ok=%s failed=%s [%s] tags attempted=%s ok=%s failed=%s tag_failures_by_machine=[%s]",
        stats.started_at_utc,
        stats.duration_seconds,
        stats.machines_attempted,
        stats.machines_ok,
        stats.machines_failed,
        failed_machines_text,
        stats.tags_attempted,
        stats.tags_ok,
        stats.tags_failed,
        tag_failures_text,
    )
    return stats


def persist_poll_run(conn: sqlite3.Connection, stats: PollRunStats) -> None:
    with transaction(conn):
        cursor = conn.execute(
            """
            INSERT INTO poll_runs (
                started_at_utc,
                finished_at_utc,
                duration_seconds,
                machines_attempted,
                machines_ok,
                machines_failed,
                tags_attempted,
                tags_ok,
                tags_failed
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stats.started_at_utc,
                stats.finished_at_utc,
                stats.duration_seconds,
                stats.machines_attempted,
                stats.machines_ok,
                stats.machines_failed,
                stats.tags_attempted,
                stats.tags_ok,
                stats.tags_failed,
            ),
        )
        poll_run_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO machine_poll_runs (
                poll_run_id,
                machine_id,
                machine_name,
                endpoint_url,
                started_at_utc,
                finished_at_utc,
                duration_seconds,
                tags_attempted,
                tags_ok,
                tags_failed,
                connection_ok,
                error_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    poll_run_id,
                    result.machine_id,
                    result.machine_name,
                    result.endpoint_url,
                    result.started_at_utc,
                    result.finished_at_utc,
                    result.duration_seconds,
                    result.tags_attempted,
                    result.tags_ok,
                    result.tags_failed,
                    1 if result.connection_ok else 0,
                    result.error_text,
                )
                for result in stats.machine_results
            ],
        )


def sleep_until_next_interval(interval_seconds: int, align: bool) -> None:
    if interval_seconds <= 0:
        return
    if align:
        now = time.time()
        delay = interval_seconds - math.fmod(now, interval_seconds)
        if math.isclose(delay, interval_seconds, abs_tol=0.001):
            delay = 0.0
    else:
        delay = float(interval_seconds)
    if delay > 0:
        time.sleep(delay)


def run_scheduled_cleanup(conn: sqlite3.Connection) -> None:
    try:
        cleanup_old_data(conn=conn)
    except Exception as exc:
        LOGGER.exception("Cleanup failed: %s", exc)


def collector_loop(
    conn: sqlite3.Connection,
    once: bool,
    interval_seconds: int,
    align_sleep: bool,
    machine_name: str | None = None,
) -> None:
    run_scheduled_cleanup(conn)
    cleanup_interval_seconds = max(CLEANUP_INTERVAL_MINUTES, 1) * 60
    last_cleanup_monotonic = time.monotonic()
    while True:
        if not once and align_sleep:
            sleep_until_next_interval(interval_seconds, True)
        stats = run_poll_cycle(conn, machine_name=machine_name)
        if stats.duration_seconds > interval_seconds:
            LOGGER.warning(
                "Poll cycle exceeded interval duration_seconds=%.2f interval_seconds=%s",
                stats.duration_seconds,
                interval_seconds,
            )
        if once:
            return
        now_monotonic = time.monotonic()
        if now_monotonic - last_cleanup_monotonic >= cleanup_interval_seconds:
            run_scheduled_cleanup(conn)
            last_cleanup_monotonic = now_monotonic
        if not align_sleep:
            sleep_until_next_interval(interval_seconds, False)
