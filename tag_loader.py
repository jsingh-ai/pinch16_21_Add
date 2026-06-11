from __future__ import annotations

import csv
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from config import (
    AUTH_MODE_ANONYMOUS,
    DEFAULT_MACHINE_NAMES,
    MACHINE_AUTH_CONFIG,
    TAG_FILES_DIR,
)
from db import transaction

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


def load_tag_files(conn: sqlite3.Connection, tag_files_dir: Path = TAG_FILES_DIR) -> list[MachineImportSummary]:
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


def _ensure_default_machines(conn: sqlite3.Connection) -> None:
    for machine_name in DEFAULT_MACHINE_NAMES:
        overrides = MACHINE_AUTH_CONFIG.get(machine_name, {})
        conn.execute(
            """
            INSERT INTO machines (machine_name, endpoint_url, auth_mode, enabled, created_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(machine_name) DO UPDATE SET
                auth_mode = excluded.auth_mode,
                enabled = excluded.enabled,
                endpoint_url = COALESCE(machines.endpoint_url, excluded.endpoint_url)
            """,
            (
                machine_name,
                overrides.get("endpoint_url"),
                overrides.get("auth_mode", AUTH_MODE_ANONYMOUS),
                1 if overrides.get("enabled", True) else 0,
            ),
        )


def _import_csv_file(conn: sqlite3.Connection, csv_file: Path) -> list[MachineImportSummary]:
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


def _upsert_machine(conn: sqlite3.Connection, machine_name: str, endpoint_url: str | None) -> int:
    overrides = MACHINE_AUTH_CONFIG.get(machine_name, {})
    final_endpoint = overrides.get("endpoint_url") or endpoint_url
    auth_mode = str(overrides.get("auth_mode", AUTH_MODE_ANONYMOUS))
    enabled = 1 if overrides.get("enabled", True) else 0

    conn.execute(
        """
        INSERT INTO machines (machine_name, endpoint_url, auth_mode, enabled, created_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(machine_name) DO UPDATE SET
            endpoint_url = COALESCE(excluded.endpoint_url, machines.endpoint_url),
            auth_mode = excluded.auth_mode,
            enabled = excluded.enabled
        """,
        (machine_name, final_endpoint, auth_mode, enabled),
    )

    row = conn.execute(
        "SELECT id FROM machines WHERE machine_name = ?",
        (machine_name,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Failed to load machine row for {machine_name}")
    return int(row["id"])


def _upsert_tag(conn: sqlite3.Connection, machine_id: int, row: dict[str, str]) -> None:
    conn.execute(
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
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, datetime('now'))
        ON CONFLICT(machine_id, node_id) DO UPDATE SET
            opc_path = excluded.opc_path,
            display_name = excluded.display_name,
            browse_name = excluded.browse_name,
            data_type = excluded.data_type,
            parent_branch = excluded.parent_branch
        """,
        (
            machine_id,
            _normalize(row.get("node_id")),
            _normalize(row.get("opc_path")),
            _normalize(row.get("display_name")),
            _normalize(row.get("browse_name")),
            _normalize(row.get("data_type")),
            _normalize(row.get("parent_branch")),
        ),
    )
