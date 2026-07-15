from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


BASE_DIR = Path(__file__).resolve().parent
if load_dotenv is not None:
    load_dotenv(BASE_DIR / ".env")

TAG_FILES_DIR = BASE_DIR / "Tag_Files"
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
LOCK_PATH = DATA_DIR / "press_opcua_collector.lock"
LOG_PATH = LOGS_DIR / "press_opcua_collector.log"

DEFAULT_MYSQL_DATABASE = "press_opcua_collector"
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", DEFAULT_MYSQL_DATABASE)
MYSQL_SSL_CA = os.getenv("MYSQL_SSL_CA", "").strip()

DEFAULT_POLL_INTERVAL_MINUTES = 1.0
POLL_INTERVAL_MINUTES = float(
    os.getenv("POLL_INTERVAL_MINUTES", "") or DEFAULT_POLL_INTERVAL_MINUTES
)
OPCUA_READ_BATCH_SIZE = int(os.getenv("OPCUA_READ_BATCH_SIZE", "") or 100)
MYSQL_INSERT_BATCH_SIZE = int(os.getenv("MYSQL_INSERT_BATCH_SIZE", "") or 2000)

PRESS_NAMES = ("Press 14", "Press 15")
CSV_FILENAMES = {
    "Press 14": "Press_14_opcua_discovered_tags.csv",
    "Press 15": "Press_15_opcua_discovered_tags.csv",
}
ENDPOINT_ENV_NAMES = {
    "Press 14": "PRESS_14_ENDPOINT_URL",
    "Press 15": "PRESS_15_ENDPOINT_URL",
}


def endpoint_override(machine_name: str) -> str | None:
    value = os.getenv(ENDPOINT_ENV_NAMES[machine_name], "").strip()
    return value or None


def mysql_connection_kwargs() -> dict[str, object]:
    kwargs: dict[str, object] = {
        "host": MYSQL_HOST,
        "port": MYSQL_PORT,
        "user": MYSQL_USER,
        "password": MYSQL_PASSWORD,
        "database": MYSQL_DATABASE,
        "charset": "utf8mb4",
        "connect_timeout": 10,
        "read_timeout": 30,
        "write_timeout": 30,
        "autocommit": False,
    }
    if MYSQL_SSL_CA:
        kwargs["ssl"] = {"ca": MYSQL_SSL_CA}
    return kwargs
