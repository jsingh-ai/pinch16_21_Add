from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import DEFAULT_MACHINE_NAMES, EXPECTED_TAG_FILES, TAG_FILES_DIR, get_machine_config
from db import Connection, executemany, to_db_datetime, transaction, utc_now

LOGGER = logging.getLogger(__name__)

PREFERRED_FIELDS = (
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
TAG_METADATA_FIELDS = (
    "opc_path",
    "display_name",
    "browse_name",
    "data_type",
    "parent_branch",
)


@dataclass(frozen=True, slots=True)
class CsvValidationIssue:
    filename: str
    row_number: int
    field: str
    reason: str

    def __str__(self) -> str:
        return (
            f"CSV {self.filename}, row {self.row_number}, field {self.field}: "
            f"{self.reason}"
        )


class CsvValidationError(ValueError):
    def __init__(self, issues: list[CsvValidationIssue]):
        self.issues = issues
        super().__init__("\n".join(str(issue) for issue in issues))


@dataclass(frozen=True, slots=True)
class TagDefinition:
    node_id: str
    opc_path: str | None
    display_name: str | None
    browse_name: str | None
    data_type: str | None
    parent_branch: str | None


@dataclass(frozen=True, slots=True)
class ValidatedMachineCsv:
    filename: str
    machine_name: str
    endpoint_url: str
    tags: tuple[TagDefinition, ...]


@dataclass(slots=True)
class MachineImportSummary:
    machine_name: str
    filename: str
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    re_enabled: int = 0
    disabled: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "machine": self.machine_name,
            "filename": self.filename,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "re-enabled": self.re_enabled,
            "disabled": self.disabled,
            "rejected": self.rejected,
            "errors": self.errors,
        }


@dataclass(frozen=True, slots=True)
class TagSyncPlan:
    inserted: tuple[TagDefinition, ...]
    updated: tuple[TagDefinition, ...]
    unchanged: tuple[TagDefinition, ...]
    re_enabled: tuple[TagDefinition, ...]
    disabled_ids: tuple[int, ...]


def _normalize(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def expected_csv_paths(tag_files_dir: Path = TAG_FILES_DIR) -> dict[str, Path]:
    return {
        machine_name: tag_files_dir / EXPECTED_TAG_FILES[machine_name]
        for machine_name in DEFAULT_MACHINE_NAMES
    }


def discover_csv_files(tag_files_dir: Path = TAG_FILES_DIR) -> list[Path]:
    """Return only the two canonical Press CSV paths that currently exist."""
    return [path for path in expected_csv_paths(tag_files_dir).values() if path.is_file()]


def validate_machine_csv(csv_file: Path, expected_machine_name: str) -> ValidatedMachineCsv:
    issues: list[CsvValidationIssue] = []
    tags: list[TagDefinition] = []
    seen_node_ids: dict[str, int] = {}
    endpoint_url: str | None = None

    with csv_file.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        for required_field in REQUIRED_FIELDS:
            if required_field not in header:
                issues.append(
                    CsvValidationIssue(
                        csv_file.name,
                        1,
                        required_field,
                        "required column is missing from the CSV header",
                    )
                )
        if issues:
            raise CsvValidationError(issues)

        for row_number, row in enumerate(reader, start=2):
            normalized = {field_name: _normalize(row.get(field_name)) for field_name in PREFERRED_FIELDS}
            row_has_error = False
            for required_field in REQUIRED_FIELDS:
                if normalized[required_field] is None:
                    issues.append(
                        CsvValidationIssue(
                            csv_file.name,
                            row_number,
                            required_field,
                            "required value is blank",
                        )
                    )
                    row_has_error = True

            machine_name = normalized["machine_name"]
            if machine_name is not None and machine_name != expected_machine_name:
                issues.append(
                    CsvValidationIssue(
                        csv_file.name,
                        row_number,
                        "machine_name",
                        f"expected {expected_machine_name!r}, found {machine_name!r}",
                    )
                )
                row_has_error = True

            row_endpoint = normalized["endpoint_url"]
            if row_endpoint is not None:
                if endpoint_url is None:
                    endpoint_url = row_endpoint
                elif endpoint_url != row_endpoint:
                    issues.append(
                        CsvValidationIssue(
                            csv_file.name,
                            row_number,
                            "endpoint_url",
                            "does not match the endpoint_url used by earlier rows in this machine CSV",
                        )
                    )
                    row_has_error = True

            node_id = normalized["node_id"]
            if node_id is not None:
                if node_id in seen_node_ids:
                    issues.append(
                        CsvValidationIssue(
                            csv_file.name,
                            row_number,
                            "node_id",
                            f"duplicates row {seen_node_ids[node_id]} within the same machine CSV",
                        )
                    )
                    row_has_error = True
                else:
                    seen_node_ids[node_id] = row_number

            if not row_has_error and node_id is not None:
                tags.append(
                    TagDefinition(
                        node_id=node_id,
                        opc_path=normalized["opc_path"],
                        display_name=normalized["display_name"],
                        browse_name=normalized["browse_name"],
                        data_type=normalized["data_type"],
                        parent_branch=normalized["parent_branch"],
                    )
                )

    if not tags and not issues:
        issues.append(
            CsvValidationIssue(csv_file.name, 2, "row", "CSV contains no importable tag rows")
        )
    if issues:
        raise CsvValidationError(issues)
    assert endpoint_url is not None
    return ValidatedMachineCsv(csv_file.name, expected_machine_name, endpoint_url, tuple(tags))


def _metadata_tuple(tag: TagDefinition | dict[str, Any]) -> tuple[object, ...]:
    if isinstance(tag, TagDefinition):
        return tuple(getattr(tag, field_name) for field_name in TAG_METADATA_FIELDS)
    return tuple(tag.get(field_name) for field_name in TAG_METADATA_FIELDS)


def plan_tag_sync(
    existing_tags: dict[str, dict[str, Any]],
    incoming_tags: tuple[TagDefinition, ...],
) -> TagSyncPlan:
    inserted: list[TagDefinition] = []
    updated: list[TagDefinition] = []
    unchanged: list[TagDefinition] = []
    re_enabled: list[TagDefinition] = []
    incoming_node_ids = {tag.node_id for tag in incoming_tags}

    for tag in incoming_tags:
        existing = existing_tags.get(tag.node_id)
        if existing is None:
            inserted.append(tag)
        elif not bool(existing.get("enabled")):
            re_enabled.append(tag)
        elif _metadata_tuple(existing) != _metadata_tuple(tag):
            updated.append(tag)
        else:
            unchanged.append(tag)

    disabled_ids = tuple(
        int(existing["id"])
        for node_id, existing in existing_tags.items()
        if bool(existing.get("enabled")) and node_id not in incoming_node_ids
    )
    return TagSyncPlan(
        tuple(inserted),
        tuple(updated),
        tuple(unchanged),
        tuple(re_enabled),
        disabled_ids,
    )


def load_tag_files(conn: Connection, tag_files_dir: Path = TAG_FILES_DIR) -> list[MachineImportSummary]:
    """Validate and atomically synchronize each Press CSV independently."""
    summaries: list[MachineImportSummary] = []
    for machine_name, csv_file in expected_csv_paths(tag_files_dir).items():
        if not csv_file.is_file():
            issue = CsvValidationIssue(csv_file.name, 0, "file", "expected machine CSV file does not exist")
            summary = MachineImportSummary(
                machine_name=machine_name,
                filename=csv_file.name,
                rejected=1,
                errors=[str(issue)],
            )
            summaries.append(summary)
            LOGGER.error("%s", issue)
            continue
        try:
            validated = validate_machine_csv(csv_file, machine_name)
        except CsvValidationError as exc:
            summary = MachineImportSummary(
                machine_name=machine_name,
                filename=csv_file.name,
                rejected=len(exc.issues),
                errors=[str(issue) for issue in exc.issues],
            )
            summaries.append(summary)
            for issue in exc.issues:
                LOGGER.error("%s", issue)
            continue

        summary = sync_validated_machine(conn, validated)
        summaries.append(summary)
        LOGGER.info("Tag synchronization summary: %s", summary.as_dict())
    return summaries


def sync_validated_machine(conn: Connection, validated: ValidatedMachineCsv) -> MachineImportSummary:
    with transaction(conn):
        machine_id = _upsert_machine(conn, validated.machine_name, validated.endpoint_url)
        existing_tags = _load_existing_tags(conn, machine_id)
        plan = plan_tag_sync(existing_tags, validated.tags)
        now_dt = to_db_datetime(utc_now())

        executemany(
            conn,
            """
            INSERT INTO tags (
                machine_id, node_id, opc_path, display_name, browse_name, data_type,
                parent_branch, enabled, created_at_utc, updated_at_utc
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s)
            """,
            [
                (machine_id, tag.node_id, *_metadata_tuple(tag), now_dt, now_dt)
                for tag in plan.inserted
            ],
        )

        tags_to_refresh = (*plan.updated, *plan.re_enabled)
        executemany(
            conn,
            """
            UPDATE tags
            SET opc_path=%s, display_name=%s, browse_name=%s, data_type=%s,
                parent_branch=%s, enabled=1, updated_at_utc=%s
            WHERE machine_id=%s AND node_id=%s
            """,
            [(*_metadata_tuple(tag), now_dt, machine_id, tag.node_id) for tag in tags_to_refresh],
        )

        executemany(
            conn,
            "UPDATE tags SET enabled=0, updated_at_utc=%s WHERE id=%s",
            [(now_dt, tag_id) for tag_id in plan.disabled_ids],
        )

    return MachineImportSummary(
        machine_name=validated.machine_name,
        filename=validated.filename,
        inserted=len(plan.inserted),
        updated=len(plan.updated),
        unchanged=len(plan.unchanged),
        re_enabled=len(plan.re_enabled),
        disabled=len(plan.disabled_ids),
    )


def _upsert_machine(conn: Connection, machine_name: str, csv_endpoint_url: str) -> int:
    resolved = get_machine_config(machine_name, csv_endpoint_url)
    endpoint_url = str(resolved.get("endpoint_url") or "")
    auth_mode = str(resolved["auth_mode"])
    now_dt = to_db_datetime(utc_now())
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO machines (
                machine_name, endpoint_url, auth_mode, enabled, created_at_utc, updated_at_utc
            )
            VALUES (%s, %s, %s, 1, %s, %s)
            ON DUPLICATE KEY UPDATE
                id=LAST_INSERT_ID(id), endpoint_url=VALUES(endpoint_url),
                auth_mode=VALUES(auth_mode), enabled=1, updated_at_utc=VALUES(updated_at_utc)
            """,
            (machine_name, endpoint_url, auth_mode, now_dt, now_dt),
        )
        return int(cursor.lastrowid)


def _load_existing_tags(conn: Connection, machine_id: int) -> dict[str, dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, node_id, opc_path, display_name, browse_name, data_type, parent_branch, enabled
            FROM tags
            WHERE machine_id=%s
            """,
            (machine_id,),
        )
        return {str(row["node_id"]): row for row in cursor.fetchall()}
