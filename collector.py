from __future__ import annotations

import argparse
import logging
import math
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
    get_machine_config,
)
from db import Connection, cleanup_old_data, executemany, to_db_datetime, transaction, utc_now
from opcua_auth import patch_blank_basic256_username_token

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class MachineRecord:
    id: int
    machine_name: str
    endpoint_url: str
    auth_mode: str


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
    started_at_utc: datetime
    finished_at_utc: datetime
    duration_seconds: float
    tags_attempted: int
    tags_ok: int
    tags_failed: int
    connection_ok: bool
    error_text: str | None
    sampled_failures: list[str]


@dataclass(slots=True)
class PollRunStats:
    started_at_utc: datetime
    finished_at_utc: datetime
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
    parser.add_argument("--interval-seconds", type=int, default=DEFAULT_POLL_INTERVAL_SECONDS)
    parser.add_argument("--no-sleep-align", action="store_true")
    parser.add_argument("--init-db", action="store_true", help="Create MySQL tables and exit")
    parser.add_argument("--init-only", action="store_true", help="Initialize DB and import tags without polling")
    parser.add_argument("--machine", help="Poll only a specific machine_name")
    parser.add_argument("--cleanup-now", action="store_true")
    parser.add_argument("--clear-poll-runs", action="store_true")
    parser.add_argument("--clear-bad-samples", action="store_true")
    parser.add_argument("--clear-all-samples", action="store_true")
    parser.add_argument("--clear-monitoring-data", action="store_true")
    parser.add_argument("--db-stats", action="store_true")
    parser.add_argument("--show-config", action="store_true")
    parser.add_argument("--allow-multiple", action="store_true")
    parser.add_argument("--yes", action="store_true")
    return parser


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def load_enabled_machines(conn: Connection, machine_name: str | None = None) -> list[MachineRecord]:
    with conn.cursor() as cursor:
        if machine_name:
            cursor.execute(
                """
                SELECT id, machine_name, endpoint_url, auth_mode
                FROM machines
                WHERE enabled = 1 AND machine_name = %s
                ORDER BY machine_name
                """,
                (machine_name,),
            )
        else:
            cursor.execute(
                """
                SELECT id, machine_name, endpoint_url, auth_mode
                FROM machines
                WHERE enabled = 1
                ORDER BY machine_name
                """
            )
        rows = cursor.fetchall()
    return [
        MachineRecord(
            id=int(row["id"]),
            machine_name=str(row["machine_name"]),
            endpoint_url=str(row["endpoint_url"]),
            auth_mode=str(row["auth_mode"]),
        )
        for row in rows
    ]


def load_enabled_tags_for_machine(conn: Connection, machine_id: int) -> list[TagRecord]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, machine_id, node_id, opc_path, display_name
            FROM tags
            WHERE enabled = 1 AND machine_id = %s
            ORDER BY id
            """,
            (machine_id,),
        )
        rows = cursor.fetchall()
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
    resolved = get_machine_config(machine.machine_name, machine.endpoint_url)
    endpoint_url = str(resolved.get("endpoint_url") or machine.endpoint_url)
    client = Client(endpoint_url, timeout=DEFAULT_CONNECT_TIMEOUT_SECONDS)
    client.session_timeout = DEFAULT_SESSION_TIMEOUT_MS

    application_uri = str(resolved.get("application_uri") or "").strip()
    if application_uri:
        client.application_uri = application_uri

    if machine.auth_mode == AUTH_MODE_USERNAME_BLANK_BASIC256_TOKEN:
        patch_blank_basic256_username_token(
            client=client,
            username=str(resolved.get("username", "")),
            password=str(resolved.get("password", "")),
            policy_id=str(resolved.get("username_token_policy_id", "3")),
            policy_uri=str(
                resolved.get(
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


def _normalize_utc_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return None


def _format_status_code(status_code: Any) -> str | None:
    if status_code is None:
        return None
    if hasattr(status_code, "name"):
        return str(status_code.name)
    return str(status_code)


def _status_code_is_good(status_code: Any) -> bool | None:
    if status_code is None:
        return None
    checker = getattr(status_code, "is_good", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return None
    text = str(status_code)
    if "Good" in text:
        return True
    if "Bad" in text or "Uncertain" in text:
        return False
    return None


def read_node_sample(node: Any) -> tuple[Any, str | None, datetime | None, datetime | None]:
    try:
        data_value = node.get_data_value()
        variant = getattr(data_value, "Value", None)
        value = getattr(variant, "Value", None) if variant is not None else None
        status_code = getattr(data_value, "StatusCode", None)
        return (
            value,
            _format_status_code(status_code),
            _normalize_utc_datetime(getattr(data_value, "SourceTimestamp", None)),
            _normalize_utc_datetime(getattr(data_value, "ServerTimestamp", None)),
        )
    except Exception:
        return (node.get_value(), None, None, None)


def poll_machine(conn: Connection, machine: MachineRecord, sampled_at_utc: datetime) -> MachinePollResult:
    machine_started = utc_now()
    tags = load_enabled_tags_for_machine(conn, machine.id)
    if not tags:
        finished = utc_now()
        LOGGER.info("Skipping %s because no enabled tags were found", machine.machine_name)
        return MachinePollResult(
            machine_id=machine.id,
            machine_name=machine.machine_name,
            endpoint_url=machine.endpoint_url,
            started_at_utc=machine_started,
            finished_at_utc=finished,
            duration_seconds=(finished - machine_started).total_seconds(),
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
    LOGGER.info("Machine start %s endpoint=%s tags=%s", machine.machine_name, machine.endpoint_url, len(tags))

    client = create_client(machine)
    try:
        client.connect()
        LOGGER.info("Machine connected %s", machine.machine_name)
        created_at = to_db_datetime(utc_now())

        for tag in tags:
            try:
                node = client.get_node(tag.node_id)
                value, status_code, source_ts, server_ts = read_node_sample(node)
                value_text, value_numeric = normalize_value(value)
                is_good_status = _status_code_is_good(status_code)
                quality = "good" if is_good_status is not False else "bad"
                error_text = None
                if quality == "good":
                    tags_ok += 1
                else:
                    tags_failed += 1
                    error_text = f"StatusCode {status_code}" if status_code else "Read returned non-good status"
                    if len(sampled_failures) < DEFAULT_TAG_FAILURE_LOG_SAMPLE:
                        sampled_failures.append(f"{tag.display_name or tag.node_id}: {error_text}")

                rows_to_insert.append(
                    (
                        tag.id,
                        machine.id,
                        to_db_datetime(sampled_at_utc),
                        to_db_datetime(source_ts),
                        to_db_datetime(server_ts),
                        value_numeric,
                        value_text,
                        quality,
                        status_code,
                        error_text,
                        created_at,
                    )
                )
            except Exception as exc:
                tags_failed += 1
                if len(sampled_failures) < DEFAULT_TAG_FAILURE_LOG_SAMPLE:
                    sampled_failures.append(f"{tag.display_name or tag.node_id}: {exc}")
                rows_to_insert.append(
                    (
                        tag.id,
                        machine.id,
                        to_db_datetime(sampled_at_utc),
                        None,
                        None,
                        None,
                        None,
                        "bad",
                        None,
                        str(exc),
                        created_at,
                    )
                )
    finally:
        try:
            client.disconnect()
            LOGGER.info("Machine disconnected %s", machine.machine_name)
        except Exception as exc:
            LOGGER.warning("Disconnect failed for %s: %s", machine.machine_name, exc)

    with transaction(conn):
        executemany(
            conn,
            """
            INSERT INTO tag_samples (
                tag_id,
                machine_id,
                sampled_at_utc,
                source_timestamp_utc,
                server_timestamp_utc,
                value_numeric,
                value_text,
                quality,
                status_code,
                error_text,
                created_at_utc
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            rows_to_insert,
        )

    finished = utc_now()
    duration_seconds = (finished - machine_started).total_seconds()
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
        started_at_utc=machine_started,
        finished_at_utc=finished,
        duration_seconds=duration_seconds,
        tags_attempted=len(tags),
        tags_ok=tags_ok,
        tags_failed=tags_failed,
        connection_ok=True,
        error_text=None,
        sampled_failures=sampled_failures,
    )


def run_poll_cycle(conn: Connection, machine_name: str | None = None) -> PollRunStats:
    cycle_started = utc_now()
    started_monotonic = time.monotonic()
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
        machine_started = utc_now()
        machine_tags = load_enabled_tags_for_machine(conn, machine.id)
        tags_attempted += len(machine_tags)
        try:
            machine_result = poll_machine(conn, machine, cycle_started)
        except Exception as exc:
            machines_failed += 1
            tags_failed += len(machine_tags)
            failed_machines.append(machine.machine_name)
            LOGGER.exception("Machine poll failed for %s: %s", machine.machine_name, exc)
            finished = utc_now()
            machine_results.append(
                MachinePollResult(
                    machine_id=machine.id,
                    machine_name=machine.machine_name,
                    endpoint_url=machine.endpoint_url,
                    started_at_utc=machine_started,
                    finished_at_utc=finished,
                    duration_seconds=(finished - machine_started).total_seconds(),
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

    cycle_finished = utc_now()
    duration = time.monotonic() - started_monotonic
    stats = PollRunStats(
        started_at_utc=cycle_started,
        finished_at_utc=cycle_finished,
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
    LOGGER.info(
        "Poll summary started=%s duration=%.2fs machines attempted=%s ok=%s failed=%s tags attempted=%s ok=%s failed=%s",
        stats.started_at_utc.replace(microsecond=0).isoformat(),
        duration,
        machines_attempted,
        machines_ok,
        machines_failed,
        tags_attempted,
        tags_ok,
        tags_failed,
    )
    return stats


def persist_poll_run(conn: Connection, stats: PollRunStats) -> None:
    created_at = to_db_datetime(utc_now())
    with transaction(conn):
        with conn.cursor() as cursor:
            cursor.execute(
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
                    tags_failed,
                    created_at_utc
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    to_db_datetime(stats.started_at_utc),
                    to_db_datetime(stats.finished_at_utc),
                    stats.duration_seconds,
                    stats.machines_attempted,
                    stats.machines_ok,
                    stats.machines_failed,
                    stats.tags_attempted,
                    stats.tags_ok,
                    stats.tags_failed,
                    created_at,
                ),
            )
            poll_run_id = int(cursor.lastrowid)

        executemany(
            conn,
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
                error_text,
                created_at_utc
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    poll_run_id,
                    result.machine_id,
                    result.machine_name,
                    result.endpoint_url,
                    to_db_datetime(result.started_at_utc),
                    to_db_datetime(result.finished_at_utc),
                    result.duration_seconds,
                    result.tags_attempted,
                    result.tags_ok,
                    result.tags_failed,
                    1 if result.connection_ok else 0,
                    result.error_text,
                    created_at,
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


def run_scheduled_cleanup(conn: Connection) -> None:
    try:
        cleanup_old_data(conn=conn)
    except Exception as exc:
        LOGGER.exception("Cleanup failed: %s", exc)


def collector_loop(
    conn: Connection,
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
