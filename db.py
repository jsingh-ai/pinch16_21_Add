from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from config import DATA_DIR, SQLITE_DB_PATH


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS machines (
        id INTEGER PRIMARY KEY,
        machine_name TEXT UNIQUE,
        endpoint_url TEXT,
        auth_mode TEXT,
        enabled INTEGER,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY,
        machine_id INTEGER,
        node_id TEXT,
        opc_path TEXT,
        display_name TEXT,
        browse_name TEXT,
        data_type TEXT,
        parent_branch TEXT,
        enabled INTEGER,
        created_at TEXT,
        UNIQUE(machine_id, node_id),
        FOREIGN KEY(machine_id) REFERENCES machines(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tag_samples (
        id INTEGER PRIMARY KEY,
        tag_id INTEGER,
        machine_id INTEGER,
        ts_utc TEXT,
        value_text TEXT,
        value_numeric REAL NULL,
        quality TEXT,
        error_text TEXT NULL,
        FOREIGN KEY(tag_id) REFERENCES tags(id),
        FOREIGN KEY(machine_id) REFERENCES machines(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS poll_runs (
        id INTEGER PRIMARY KEY,
        started_at_utc TEXT,
        finished_at_utc TEXT,
        duration_seconds REAL,
        machines_attempted INTEGER,
        machines_ok INTEGER,
        machines_failed INTEGER,
        tags_attempted INTEGER,
        tags_ok INTEGER,
        tags_failed INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tag_samples_ts_utc ON tag_samples(ts_utc)",
    "CREATE INDEX IF NOT EXISTS idx_tag_samples_tag_id_ts_utc ON tag_samples(tag_id, ts_utc)",
    "CREATE INDEX IF NOT EXISTS idx_tag_samples_machine_id_ts_utc ON tag_samples(machine_id, ts_utc)",
    "CREATE INDEX IF NOT EXISTS idx_tags_machine_id ON tags(machine_id)",
    "CREATE INDEX IF NOT EXISTS idx_tags_machine_id_node_id ON tags(machine_id, node_id)",
]


def get_connection(db_path: Path | str = SQLITE_DB_PATH) -> sqlite3.Connection:
    db_file = Path(db_path)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize_database(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        conn.execute("BEGIN")
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def executemany(conn: sqlite3.Connection, sql: str, rows: Iterable[Sequence[object]]) -> None:
    conn.executemany(sql, list(rows))
