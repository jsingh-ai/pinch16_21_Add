from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TAG_FILES_DIR = BASE_DIR / "Tag_Files"
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
COLLECTOR_LOCK_PATH = DATA_DIR / "collector.lock"

MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "opcua_collector")
MYSQL_CHARSET = "utf8mb4"
MYSQL_CONNECT_TIMEOUT_SECONDS = int(os.getenv("MYSQL_CONNECT_TIMEOUT_SECONDS", "10"))
MYSQL_READ_TIMEOUT_SECONDS = int(os.getenv("MYSQL_READ_TIMEOUT_SECONDS", "30"))
MYSQL_WRITE_TIMEOUT_SECONDS = int(os.getenv("MYSQL_WRITE_TIMEOUT_SECONDS", "30"))
MYSQL_SSL_CA = os.getenv("MYSQL_SSL_CA", "").strip()

DEFAULT_POLL_INTERVAL_SECONDS = 60
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_SESSION_TIMEOUT_MS = 60000
DEFAULT_TAG_FAILURE_LOG_SAMPLE = 5
SAMPLE_RETENTION_DAYS = 14
BAD_SAMPLE_RETENTION_DAYS = 60
POLL_RUN_RETENTION_DAYS = 14
CLEANUP_INTERVAL_MINUTES = 60

AUTH_MODE_ANONYMOUS = "anonymous"
AUTH_MODE_USERNAME_BLANK_BASIC256_TOKEN = "username_blank_basic256_token"

PINCH20_USERNAME_TOKEN_POLICY_URI = "http://opcfoundation.org/UA/SecurityPolicy#Basic256"

DEFAULT_MACHINE_NAMES = [
    "Pinch 16",
    "Pinch 17",
    "Pinch 18",
    "Pinch 19",
    "Pinch 20",
    "Pinch 21",
]

# Pinch 20 is a B&R server that uses Channel SecurityPolicy None with a blank
# security_string, plus an encrypted Basic256 username token carrying a blank
# password. No client certificate files are required for this flow.
MACHINE_AUTH_CONFIG: dict[str, dict[str, object]] = {
    "Pinch 16": {
        "auth_mode": AUTH_MODE_ANONYMOUS,
        "enabled": True,
        "endpoint_url": None,
    },
    "Pinch 17": {
        "auth_mode": AUTH_MODE_ANONYMOUS,
        "enabled": True,
        "endpoint_url": None,
    },
    "Pinch 18": {
        "auth_mode": AUTH_MODE_ANONYMOUS,
        "enabled": True,
        "endpoint_url": None,
    },
    "Pinch 19": {
        "auth_mode": AUTH_MODE_ANONYMOUS,
        "enabled": True,
        "endpoint_url": None,
    },
    "Pinch 20": {
        "auth_mode": AUTH_MODE_USERNAME_BLANK_BASIC256_TOKEN,
        "enabled": True,
        "endpoint_url": "opc.tcp://192.168.11.26:4840",
        "security_string": "",
        "application_uri": "urn:example.org:FreeOpcUa:opcua-asyncio",
        "username": "OpcUaViewer",
        "password": "",
        "username_token_policy_id": "3",
        "username_token_policy_uri": PINCH20_USERNAME_TOKEN_POLICY_URI,
    },
    "Pinch 21": {
        "auth_mode": AUTH_MODE_ANONYMOUS,
        "enabled": True,
        "endpoint_url": None,
    },
}


def get_mysql_connection_kwargs(database: str | None = None) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "host": MYSQL_HOST,
        "port": MYSQL_PORT,
        "user": MYSQL_USER,
        "password": MYSQL_PASSWORD,
        "database": database or MYSQL_DATABASE,
        "charset": MYSQL_CHARSET,
        "connect_timeout": MYSQL_CONNECT_TIMEOUT_SECONDS,
        "read_timeout": MYSQL_READ_TIMEOUT_SECONDS,
        "write_timeout": MYSQL_WRITE_TIMEOUT_SECONDS,
        "autocommit": False,
    }
    if MYSQL_SSL_CA:
        kwargs["ssl"] = {"ca": MYSQL_SSL_CA}
    return kwargs


def _normalize_endpoint(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def get_machine_config(machine_name: str, csv_endpoint_url: str | None = None) -> dict[str, object]:
    config = deepcopy(MACHINE_AUTH_CONFIG.get(machine_name, {}))
    endpoint_override = _normalize_endpoint(config.get("endpoint_url"))
    config["endpoint_url"] = endpoint_override or _normalize_endpoint(csv_endpoint_url)
    config.setdefault("auth_mode", AUTH_MODE_ANONYMOUS)
    config.setdefault("enabled", True)
    return config


def get_all_machine_configs() -> dict[str, dict[str, object]]:
    return {
        machine_name: get_machine_config(machine_name)
        for machine_name in DEFAULT_MACHINE_NAMES
    }
