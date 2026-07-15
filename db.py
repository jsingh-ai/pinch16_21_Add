from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

import pymysql
from pymysql.connections import Connection
from pymysql.cursors import DictCursor

from config import mysql_connection_kwargs


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS machines (
        id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        machine_name VARCHAR(100) NOT NULL,
        endpoint_url VARCHAR(255) NOT NULL,
        created_at_utc DATETIME(6) NOT NULL,
        updated_at_utc DATETIME(6) NOT NULL,
        UNIQUE KEY uq_machines_machine_name (machine_name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS tags (
        id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        machine_id BIGINT UNSIGNED NOT NULL,
        node_id VARCHAR(512) NOT NULL,
        opc_path TEXT NULL,
        display_name VARCHAR(255) NULL,
        browse_name VARCHAR(255) NULL,
        data_type VARCHAR(120) NULL,
        parent_branch VARCHAR(120) NULL,
        enabled TINYINT(1) NOT NULL DEFAULT 1,
        created_at_utc DATETIME(6) NOT NULL,
        updated_at_utc DATETIME(6) NOT NULL,
        UNIQUE KEY uq_tags_machine_node (machine_id, node_id),
        KEY idx_tags_machine_enabled (machine_id, enabled),
        CONSTRAINT fk_tags_machine FOREIGN KEY (machine_id) REFERENCES machines(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS tag_samples (
        id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        tag_id BIGINT UNSIGNED NOT NULL,
        machine_id BIGINT UNSIGNED NOT NULL,
        sampled_at_utc DATETIME(6) NOT NULL,
        source_timestamp_utc DATETIME(6) NULL,
        server_timestamp_utc DATETIME(6) NULL,
        value_numeric DOUBLE NULL,
        value_text TEXT NULL,
        quality VARCHAR(40) NOT NULL,
        status_code VARCHAR(120) NULL,
        error_text TEXT NULL,
        created_at_utc DATETIME(6) NOT NULL,
        KEY idx_samples_machine_time (machine_id, sampled_at_utc),
        KEY idx_samples_tag_time (tag_id, sampled_at_utc),
        CONSTRAINT fk_samples_machine FOREIGN KEY (machine_id) REFERENCES machines(id),
        CONSTRAINT fk_samples_tag FOREIGN KEY (tag_id) REFERENCES tags(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def db_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def connect() -> Connection:
    connection = pymysql.connect(cursorclass=DictCursor, **mysql_connection_kwargs())
    with connection.cursor() as cursor:
        cursor.execute("SET time_zone = '+00:00'")
    return connection


def create_tables(connection: Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        with connection.cursor() as cursor:
            cursor.execute(statement)
    connection.commit()


@contextmanager
def transaction(connection: Connection) -> Iterator[Connection]:
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def execute_many(connection: Connection, sql: str, rows: list[tuple[object, ...]]) -> None:
    if rows:
        with connection.cursor() as cursor:
            cursor.executemany(sql, rows)
