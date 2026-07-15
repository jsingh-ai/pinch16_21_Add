from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout
from opcua import Client, ua

from config import (
    CSV_FILENAMES,
    LOCK_PATH,
    LOG_PATH,
    OPCUA_READ_BATCH_SIZE,
    POLL_INTERVAL_MINUTES,
    PRESS_NAMES,
    TAG_FILES_DIR,
    endpoint_override,
)
from db import connect, create_tables, db_datetime, execute_many, transaction, utc_now


LOGGER = logging.getLogger("press_opcua_collector")
CSV_FIELDS = (
    "machine_name",
    "endpoint_url",
    "node_id",
    "opc_path",
    "display_name",
    "browse_name",
    "data_type",
    "parent_branch",
)
REQUIRED_FIELDS = ("machine_name", "endpoint_url", "node_id")


@dataclass(frozen=True, slots=True)
class CsvTag:
    node_id: str
    opc_path: str | None
    display_name: str | None
    browse_name: str | None
    data_type: str | None
    parent_branch: str | None


@dataclass(frozen=True, slots=True)
class CsvMachine:
    machine_name: str
    endpoint_url: str
    tags: tuple[CsvTag, ...]


@dataclass(frozen=True, slots=True)
class RuntimeTag:
    id: int
    machine_id: int
    node_id: str
    display_name: str | None


@dataclass(frozen=True, slots=True)
class RuntimeMachine:
    id: int
    machine_name: str
    endpoint_url: str
    tags: tuple[RuntimeTag, ...]


@dataclass(frozen=True, slots=True)
class ReadResult:
    tag: RuntimeTag
    value: Any = None
    status_code: str | None = None
    source_timestamp: datetime | None = None
    server_timestamp: datetime | None = None
    error: str | None = None


class CsvValidationError(ValueError):
    pass


def configure_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(file_handler)
    logging.getLogger("opcua").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read Press 14 and Press 15 OPC UA tags from CSV and save samples to MySQL"
    )
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="Create the machines, tags, and tag_samples tables, then exit",
    )
    parser.add_argument("--once", action="store_true", help="Poll immediately once, then exit")
    parser.add_argument("--machine", choices=PRESS_NAMES, help="Poll only one press")
    parser.add_argument(
        "--interval-minutes",
        type=float,
        default=POLL_INTERVAL_MINUTES,
        help="Minutes between poll starts (default: POLL_INTERVAL_MINUTES or 1)",
    )
    return parser


def clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def validation_error(path: Path, row_number: int, field: str, reason: str) -> str:
    return f"CSV {path.name}, row {row_number}, field {field}: {reason}"


def read_machine_csv(path: Path, expected_machine_name: str) -> CsvMachine:
    if not path.is_file():
        raise CsvValidationError(validation_error(path, 0, "file", "file does not exist"))

    issues: list[str] = []
    tags: list[CsvTag] = []
    seen_nodes: dict[str, int] = {}
    endpoint_url: str | None = None

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        for field in REQUIRED_FIELDS:
            if field not in header:
                issues.append(validation_error(path, 1, field, "required column is missing"))
        if issues:
            raise CsvValidationError("\n".join(issues))

        for row_number, row in enumerate(reader, start=2):
            values = {field: clean(row.get(field)) for field in CSV_FIELDS}
            row_invalid = False
            for field in REQUIRED_FIELDS:
                if values[field] is None:
                    issues.append(validation_error(path, row_number, field, "required value is blank"))
                    row_invalid = True

            if values["machine_name"] not in (None, expected_machine_name):
                issues.append(
                    validation_error(
                        path,
                        row_number,
                        "machine_name",
                        f"must be exactly {expected_machine_name!r}",
                    )
                )
                row_invalid = True

            row_endpoint = values["endpoint_url"]
            if row_endpoint:
                if endpoint_url is None:
                    endpoint_url = row_endpoint
                elif endpoint_url != row_endpoint:
                    issues.append(
                        validation_error(
                            path, row_number, "endpoint_url", "must match earlier rows in this CSV"
                        )
                    )
                    row_invalid = True

            node_id = values["node_id"]
            if node_id:
                if node_id in seen_nodes:
                    issues.append(
                        validation_error(
                            path,
                            row_number,
                            "node_id",
                            f"duplicates row {seen_nodes[node_id]}",
                        )
                    )
                    row_invalid = True
                else:
                    seen_nodes[node_id] = row_number

            if not row_invalid and node_id:
                tags.append(
                    CsvTag(
                        node_id=node_id,
                        opc_path=values["opc_path"],
                        display_name=values["display_name"],
                        browse_name=values["browse_name"],
                        data_type=values["data_type"],
                        parent_branch=values["parent_branch"],
                    )
                )

    if not tags and not issues:
        issues.append(validation_error(path, 2, "row", "CSV has no tag rows"))
    if issues:
        raise CsvValidationError("\n".join(issues))
    assert endpoint_url is not None
    return CsvMachine(
        machine_name=expected_machine_name,
        endpoint_url=endpoint_override(expected_machine_name) or endpoint_url,
        tags=tuple(tags),
    )


def read_csv_files(tag_files_dir: Path = TAG_FILES_DIR) -> tuple[CsvMachine, ...]:
    # Validate both complete files before making any database changes.
    return tuple(
        read_machine_csv(tag_files_dir / CSV_FILENAMES[name], name) for name in PRESS_NAMES
    )


def synchronize_csv_tags(connection, csv_machines: tuple[CsvMachine, ...]) -> tuple[RuntimeMachine, ...]:
    now = db_datetime(utc_now())
    runtime_machines: list[RuntimeMachine] = []
    with transaction(connection):
        for machine in csv_machines:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO machines (machine_name, endpoint_url, created_at_utc, updated_at_utc)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        id=LAST_INSERT_ID(id), endpoint_url=VALUES(endpoint_url),
                        updated_at_utc=VALUES(updated_at_utc)
                    """,
                    (machine.machine_name, machine.endpoint_url, now, now),
                )
                machine_id = int(cursor.lastrowid)
                cursor.execute(
                    "UPDATE tags SET enabled=0, updated_at_utc=%s WHERE machine_id=%s",
                    (now, machine_id),
                )

            execute_many(
                connection,
                """
                INSERT INTO tags (
                    machine_id, node_id, opc_path, display_name, browse_name, data_type,
                    parent_branch, enabled, created_at_utc, updated_at_utc
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s)
                ON DUPLICATE KEY UPDATE
                    opc_path=VALUES(opc_path), display_name=VALUES(display_name),
                    browse_name=VALUES(browse_name), data_type=VALUES(data_type),
                    parent_branch=VALUES(parent_branch), enabled=1,
                    updated_at_utc=VALUES(updated_at_utc)
                """,
                [
                    (
                        machine_id,
                        tag.node_id,
                        tag.opc_path,
                        tag.display_name,
                        tag.browse_name,
                        tag.data_type,
                        tag.parent_branch,
                        now,
                        now,
                    )
                    for tag in machine.tags
                ],
            )

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, machine_id, node_id, display_name
                    FROM tags
                    WHERE machine_id=%s AND enabled=1
                    ORDER BY id
                    """,
                    (machine_id,),
                )
                runtime_tags = tuple(
                    RuntimeTag(
                        id=int(row["id"]),
                        machine_id=int(row["machine_id"]),
                        node_id=str(row["node_id"]),
                        display_name=row["display_name"],
                    )
                    for row in cursor.fetchall()
                )
            runtime_machines.append(
                RuntimeMachine(machine_id, machine.machine_name, machine.endpoint_url, runtime_tags)
            )
    return tuple(runtime_machines)


def status_text(status: Any) -> str | None:
    if status is None:
        return None
    return str(getattr(status, "name", status))


def utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def data_value_result(tag: RuntimeTag, data_value: Any) -> ReadResult:
    variant = getattr(data_value, "Value", None)
    return ReadResult(
        tag=tag,
        value=getattr(variant, "Value", None),
        status_code=status_text(getattr(data_value, "StatusCode", None)),
        source_timestamp=utc_timestamp(getattr(data_value, "SourceTimestamp", None)),
        server_timestamp=utc_timestamp(getattr(data_value, "ServerTimestamp", None)),
    )


def read_one(tag: RuntimeTag, node: Any) -> ReadResult:
    try:
        return data_value_result(tag, node.get_data_value())
    except Exception as exc:
        return ReadResult(tag=tag, status_code="ReadError", error=str(exc))


def get_node(client: Any, node_id: str) -> Any:
    """Build string NodeIds directly so semicolons remain part of the identifier."""
    if node_id.startswith("s="):
        return client.get_node(ua.StringNodeId(node_id[2:], 0))

    namespace_part, separator, identifier = node_id.partition(";s=")
    if separator and namespace_part.startswith("ns="):
        namespace_text = namespace_part[3:]
        if namespace_text.isdigit():
            return client.get_node(ua.StringNodeId(identifier, int(namespace_text)))

    return client.get_node(node_id)


def read_tags(client: Any, tags: tuple[RuntimeTag, ...]) -> list[ReadResult]:
    results: list[ReadResult] = []
    batch_size = max(1, OPCUA_READ_BATCH_SIZE)
    for offset in range(0, len(tags), batch_size):
        chunk = tags[offset : offset + batch_size]
        tag_nodes: list[tuple[RuntimeTag, Any]] = []
        for tag in chunk:
            try:
                tag_nodes.append((tag, get_node(client, tag.node_id)))
            except Exception as exc:
                results.append(ReadResult(tag=tag, status_code="ReadError", error=str(exc)))

        # Do not send an empty ReadRequest when every node ID in this chunk
        # failed local parsing in client.get_node().
        if not tag_nodes:
            continue

        try:
            data_values = client.uaclient.get_attributes(
                [node.nodeid for _, node in tag_nodes], ua.AttributeIds.Value
            )
            if len(data_values) != len(tag_nodes):
                raise RuntimeError("OPC UA batch returned an unexpected number of values")
            results.extend(
                data_value_result(tag, data_value)
                for (tag, _), data_value in zip(tag_nodes, data_values)
            )
        except Exception as exc:
            LOGGER.warning("Batch read failed; reading this batch one tag at a time: %s", exc)
            results.extend(read_one(tag, node) for tag, node in tag_nodes)
    return results


def is_good_status(status_code: str | None) -> bool:
    if status_code is None:
        return True
    return "Bad" not in status_code and "Uncertain" not in status_code and status_code != "ReadError"


def normalize_value(value: Any) -> tuple[str | None, float | None]:
    if value is None:
        return None, None
    text = str(value)
    if isinstance(value, bool):
        return text, 1.0 if value else 0.0
    try:
        return text, float(value)
    except (TypeError, ValueError):
        return text, None


def save_results(connection, machine: RuntimeMachine, sampled_at: datetime, results: list[ReadResult]) -> None:
    created_at = db_datetime(utc_now())
    rows: list[tuple[object, ...]] = []
    for result in results:
        value_text, value_numeric = normalize_value(result.value)
        good = result.error is None and is_good_status(result.status_code)
        error_text = result.error
        if not good and error_text is None:
            error_text = f"OPC UA status {result.status_code or 'unknown'}"
        rows.append(
            (
                result.tag.id,
                machine.id,
                db_datetime(sampled_at),
                db_datetime(result.source_timestamp),
                db_datetime(result.server_timestamp),
                value_numeric,
                value_text,
                "good" if good else "bad",
                result.status_code,
                error_text,
                created_at,
            )
        )
    with transaction(connection):
        execute_many(
            connection,
            """
            INSERT INTO tag_samples (
                tag_id, machine_id, sampled_at_utc, source_timestamp_utc,
                server_timestamp_utc, value_numeric, value_text, quality,
                status_code, error_text, created_at_utc
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )


def poll_machine(connection, machine: RuntimeMachine, sampled_at: datetime) -> None:
    LOGGER.info("Polling %s (%s tags)", machine.machine_name, len(machine.tags))
    client = Client(machine.endpoint_url, timeout=10)
    client.session_timeout = 60000
    try:
        # No security string, username, or password is configured: SecurityPolicy None.
        client.connect()
        results = read_tags(client, machine.tags)
    except Exception as exc:
        LOGGER.error("%s connection/read failed: %s", machine.machine_name, exc)
        results = [
            ReadResult(tag=tag, status_code="ConnectionError", error=str(exc))
            for tag in machine.tags
        ]
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
    save_results(connection, machine, sampled_at, results)
    failed_results = [
        result for result in results if result.error or not is_good_status(result.status_code)
    ]
    for result in failed_results[:5]:
        LOGGER.warning(
            "%s failed tag node_id=%s status=%s error=%s",
            machine.machine_name,
            result.tag.node_id,
            result.status_code,
            result.error or f"OPC UA status {result.status_code or 'unknown'}",
        )
    LOGGER.info(
        "%s complete: saved=%s failed=%s",
        machine.machine_name,
        len(results),
        len(failed_results),
    )


def poll_all(connection, machines: tuple[RuntimeMachine, ...]) -> None:
    sampled_at = utc_now()
    for machine in machines:
        try:
            poll_machine(connection, machine, sampled_at)
        except Exception as exc:
            LOGGER.exception("Could not save %s results: %s", machine.machine_name, exc)


def run_loop(
    connection,
    machines: tuple[RuntimeMachine, ...],
    interval_minutes: float,
    once: bool,
) -> None:
    interval_seconds = interval_minutes * 60.0
    while True:
        cycle_started = time.monotonic()
        poll_all(connection, machines)
        if once:
            return
        delay = max(0.0, interval_seconds - (time.monotonic() - cycle_started))
        LOGGER.info("Next poll in %.1f seconds", delay)
        time.sleep(delay)


def main() -> int:
    args = build_parser().parse_args()
    if args.interval_minutes <= 0:
        raise SystemExit("--interval-minutes must be greater than zero")
    configure_logging()

    if args.init_db:
        connection = connect()
        try:
            create_tables(connection)
            LOGGER.info("Created required collector tables")
            return 0
        finally:
            connection.close()

    csv_machines = read_csv_files()
    connection = connect()
    try:
        runtime_machines = synchronize_csv_tags(connection, csv_machines)
        if args.machine:
            runtime_machines = tuple(
                machine for machine in runtime_machines if machine.machine_name == args.machine
            )

        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            with FileLock(LOCK_PATH).acquire(timeout=0):
                run_loop(connection, runtime_machines, args.interval_minutes, args.once)
        except Timeout:
            LOGGER.error("Another Press collector instance is already running")
            return 1
        return 0
    except KeyboardInterrupt:
        LOGGER.info("Collector stopped")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
