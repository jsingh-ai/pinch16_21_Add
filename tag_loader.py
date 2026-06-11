from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import (
    AUTH_MODE_ANONYMOUS,
    DEFAULT_MACHINE_NAMES,
    TAG_FILES_DIR,
    get_machine_config,
)
from db import Connection, to_db_datetime, transaction, utc_now

LOGGER = logging.getLogger(__name__)

CSV_GLOB = "*_opcua_discovered_tags.csv"


@dataclass(slots=True)
class MachineImportSummary:
    machine_name: str
    endpoint_url: str | None
    tags_seen: int


def _normalize(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _infer_machine_name(row: dict[str, str], file_path: Path) -> str:
    csv_machine_name = _normalize(row.get("machine_name"))
    if csv_machine_name:
        return csv_machine_name
    stem = file_path.stem
    return stem.replace("_opcua_discovered_tags", "").replace("_", " ")


def discover_csv_files(tag_files_dir: Path = TAG_FILES_DIR) -> list[Path]:
    return sorted(tag_files_dir.glob(CSV_GLOB))


def load_tag_files(conn: Connection, tag_files_dir: Path = TAG_FILES_DIR) -> list[MachineImportSummary]:
    csv_files = discover_csv_files(tag_files_dir)
    if not csv_files:
        raise FileNotFoundError(f"No CSV files matching {CSV_GLOB} found in {tag_files_dir}")

    summaries: list[MachineImportSummary] = []
    with transaction(conn):
        _ensure_default_machines(conn)
        for csv_file in csv_files:
            summaries.extend(_import_csv_file(conn, csv_file))
    LOGGER.info("Imported %s CSV files from %s", len(csv_files), tag_files_dir)
    return summaries


def _ensure_default_machines(conn: Connection) -> None:
    now_dt = to_db_datetime(utc_now())
    for machine_name in DEFAULT_MACHINE_NAMES:
        resolved = get_machine_config(machine_name)
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO machines (
                    machine_name,
                    endpoint_url,
                    auth_mode,
                    enabled,
                    created_at_utc,
                    updated_at_utc
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    id = LAST_INSERT_ID(id),
                    endpoint_url = COALESCE(VALUES(endpoint_url), endpoint_url),
                    auth_mode = VALUES(auth_mode),
                    enabled = VALUES(enabled),
                    updated_at_utc = VALUES(updated_at_utc)
                """,
                (
                    machine_name,
                    resolved.get("endpoint_url") or "",
                    resolved.get("auth_mode", AUTH_MODE_ANONYMOUS),
                    1 if resolved.get("enabled", True) else 0,
                    now_dt,
                    now_dt,
                ),
            )


def _import_csv_file(conn: Connection, csv_file: Path) -> list[MachineImportSummary]:
    summaries: dict[str, MachineImportSummary] = {}
    with csv_file.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            machine_name = _infer_machine_name(row, csv_file)
            endpoint_url = _normalize(row.get("endpoint_url"))
            machine_id = _upsert_machine(conn, machine_name, endpoint_url)
            _upsert_tag(conn, machine_id, row)

            summary = summaries.get(machine_name)
            if summary is None:
                summaries[machine_name] = MachineImportSummary(machine_name, endpoint_url, 1)
            else:
                summary.tags_seen += 1
                if not summary.endpoint_url and endpoint_url:
                    summary.endpoint_url = endpoint_url

    for summary in summaries.values():
        LOGGER.info(
            "Imported %s tags for %s from %s",
            summary.tags_seen,
            summary.machine_name,
            csv_file.name,
        )
    return list(summaries.values())


def _upsert_machine(conn: Connection, machine_name: str, endpoint_url: str | None) -> int:
    resolved = get_machine_config(machine_name, endpoint_url)
    final_endpoint = resolved.get("endpoint_url") or ""
    auth_mode = str(resolved.get("auth_mode", AUTH_MODE_ANONYMOUS))
    enabled = 1 if resolved.get("enabled", True) else 0
    now_dt = to_db_datetime(utc_now())

    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO machines (
                machine_name,
                endpoint_url,
                auth_mode,
                enabled,
                created_at_utc,
                updated_at_utc
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                id = LAST_INSERT_ID(id),
                endpoint_url = COALESCE(NULLIF(VALUES(endpoint_url), ''), endpoint_url),
                auth_mode = VALUES(auth_mode),
                enabled = VALUES(enabled),
                updated_at_utc = VALUES(updated_at_utc)
            """,
            (
                machine_name,
                final_endpoint,
                auth_mode,
                enabled,
                now_dt,
                now_dt,
            ),
        )
        return int(cursor.lastrowid)


def _upsert_tag(conn: Connection, machine_id: int, row: dict[str, str]) -> None:
    now_dt = to_db_datetime(utc_now())
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO tags (
                machine_id,
                node_id,
                opc_path,
                display_name,
                browse_name,
                data_type,
                parent_branch,
                enabled,
                created_at_utc,
                updated_at_utc
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s)
            ON DUPLICATE KEY UPDATE
                id = LAST_INSERT_ID(id),
                opc_path = VALUES(opc_path),
                display_name = VALUES(display_name),
                browse_name = VALUES(browse_name),
                data_type = VALUES(data_type),
                parent_branch = VALUES(parent_branch),
                updated_at_utc = VALUES(updated_at_utc)
            """,
            (
                machine_id,
                _normalize(row.get("node_id")),
                _normalize(row.get("opc_path")),
                _normalize(row.get("display_name")),
                _normalize(row.get("browse_name")),
                _normalize(row.get("data_type")),
                _normalize(row.get("parent_branch")),
                now_dt,
                now_dt,
            ),
        )
