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
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_SESSION_TIMEOUT_MS,
    MACHINE_AUTH_CONFIG,
)
from db import transaction
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
    return parser


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_enabled_machines(conn: sqlite3.Connection) -> list[MachineRecord]:
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

    client = Client(machine.endpoint_url, timeout=DEFAULT_CONNECT_TIMEOUT_SECONDS)
    client.session_timeout = DEFAULT_SESSION_TIMEOUT_MS

    if machine.auth_mode == AUTH_MODE_USERNAME_BLANK_BASIC256_TOKEN:
        overrides = MACHINE_AUTH_CONFIG.get(machine.machine_name, {})
        patch_blank_basic256_username_token(
            client=client,
            username=str(overrides.get("username", "")),
            password=str(overrides.get("password", "")),
            policy_id=str(overrides.get("policy_id", "3")),
            policy_uri=str(
                overrides.get(
                    "policy_uri",
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


def poll_machine(conn: sqlite3.Connection, machine: MachineRecord, started_ts: str) -> tuple[int, int]:
    tags = load_enabled_tags_for_machine(conn, machine.id)
    if not tags:
        LOGGER.info("Skipping %s because no enabled tags were found", machine.machine_name)
        return (0, 0)

    rows_to_insert: list[tuple[object, ...]] = []
    tags_ok = 0
    tags_failed = 0

    client = create_client(machine)
    try:
        client.connect()
        LOGGER.info("Connected to %s at %s", machine.machine_name, machine.endpoint_url)

        for tag in tags:
            try:
                node = client.get_node(tag.node_id)
                value = node.get_value()
                value_text, value_numeric = normalize_value(value)
                rows_to_insert.append(
                    (
                        tag.id,
                        machine.id,
                        started_ts,
                        value_text,
                        value_numeric,
                        "good",
                        None,
                    )
                )
                tags_ok += 1
            except Exception as exc:
                rows_to_insert.append(
                    (
                        tag.id,
                        machine.id,
                        started_ts,
                        None,
                        None,
                        "bad",
                        str(exc),
                    )
                )
                tags_failed += 1
                LOGGER.warning(
                    "Read failed for %s [%s]: %s",
                    tag.display_name or tag.node_id,
                    machine.machine_name,
                    exc,
                )
    finally:
        try:
            client.disconnect()
        except Exception as exc:
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
                error_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows_to_insert,
        )
    return (tags_ok, tags_failed)


def run_poll_cycle(conn: sqlite3.Connection) -> PollRunStats:
    started = time.monotonic()
    started_ts = utc_now_iso()
    machines = load_enabled_machines(conn)

    machines_attempted = 0
    machines_ok = 0
    machines_failed = 0
    tags_attempted = 0
    tags_ok = 0
    tags_failed = 0

    for machine in machines:
        machines_attempted += 1
        machine_tags = load_enabled_tags_for_machine(conn, machine.id)
        tags_attempted += len(machine_tags)
        try:
            ok_count, failed_count = poll_machine(conn, machine, started_ts)
        except Exception as exc:
            machines_failed += 1
            tags_failed += len(machine_tags)
            LOGGER.exception("Machine poll failed for %s: %s", machine.machine_name, exc)
            continue

        machines_ok += 1
        tags_ok += ok_count
        tags_failed += failed_count

    finished_ts = utc_now_iso()
    duration = time.monotonic() - started
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
    )
    persist_poll_run(conn, stats)
    LOGGER.info(
        "Poll summary started=%s duration=%.2fs machines ok=%s failed=%s tags ok=%s failed=%s",
        stats.started_at_utc,
        stats.duration_seconds,
        stats.machines_ok,
        stats.machines_failed,
        stats.tags_ok,
        stats.tags_failed,
    )
    return stats


def persist_poll_run(conn: sqlite3.Connection, stats: PollRunStats) -> None:
    with transaction(conn):
        conn.execute(
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


def collector_loop(conn: sqlite3.Connection, once: bool, interval_seconds: int, align_sleep: bool) -> None:
    while True:
        if not once and align_sleep:
            sleep_until_next_interval(interval_seconds, True)
        run_poll_cycle(conn)
        if once:
            return
        if not align_sleep:
            sleep_until_next_interval(interval_seconds, False)
