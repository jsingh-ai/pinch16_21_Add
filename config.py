from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # Allows static tooling to run before dependencies are installed.
    load_dotenv = None


BASE_DIR = Path(__file__).resolve().parent
if load_dotenv is not None:
    load_dotenv(BASE_DIR / ".env")

TAG_FILES_DIR = BASE_DIR / "Tag_Files"
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
COLLECTOR_LOCK_PATH = DATA_DIR / "press_opcua_collector.lock"
COLLECTOR_LOG_PATH = LOGS_DIR / "press_opcua_collector.log"
DASHBOARD_LOG_PATH = LOGS_DIR / "press_opcua_collector_dashboard.log"

MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
DEFAULT_MYSQL_DATABASE = "press_opcua_collector"
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", DEFAULT_MYSQL_DATABASE)
MYSQL_CHARSET = "utf8mb4"
MYSQL_CONNECT_TIMEOUT_SECONDS = int(os.getenv("MYSQL_CONNECT_TIMEOUT_SECONDS", "10"))
MYSQL_READ_TIMEOUT_SECONDS = int(os.getenv("MYSQL_READ_TIMEOUT_SECONDS", "30"))
MYSQL_WRITE_TIMEOUT_SECONDS = int(os.getenv("MYSQL_WRITE_TIMEOUT_SECONDS", "30"))
MYSQL_SSL_CA = os.getenv("MYSQL_SSL_CA", "").strip()

DEFAULT_POLL_INTERVAL_SECONDS = 60
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "") or DEFAULT_POLL_INTERVAL_SECONDS)
DEFAULT_OPCUA_READ_BATCH_SIZE = int(os.getenv("OPCUA_READ_BATCH_SIZE", "100") or "100")
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_SESSION_TIMEOUT_MS = 60000
DEFAULT_TAG_FAILURE_LOG_SAMPLE = 5
SAMPLE_RETENTION_DAYS = 14
BAD_SAMPLE_RETENTION_DAYS = 60
POLL_RUN_RETENTION_DAYS = 14
CLEANUP_INTERVAL_MINUTES = 60

DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "5051") or "5051")

AUTH_MODE_ANONYMOUS = "anonymous"
AUTH_MODE_USERNAME_PASSWORD = "username_password"
ACCEPTED_AUTH_MODES = (AUTH_MODE_ANONYMOUS, AUTH_MODE_USERNAME_PASSWORD)

DEFAULT_MACHINE_NAMES = ["Press 14", "Press 15"]
EXPECTED_TAG_FILES = {
    "Press 14": "Press_14_opcua_discovered_tags.csv",
    "Press 15": "Press_15_opcua_discovered_tags.csv",
}


def _machine_env_prefix(machine_name: str) -> str:
    return machine_name.upper().replace(" ", "_")


def _machine_auth_config(machine_name: str) -> dict[str, object]:
    prefix = _machine_env_prefix(machine_name)
    return {
        "auth_mode": (os.getenv(f"{prefix}_AUTH_MODE", "") or AUTH_MODE_ANONYMOUS).strip().lower(),
        "enabled": True,
        "endpoint_url": os.getenv(f"{prefix}_ENDPOINT_URL", "").strip() or None,
        "username": os.getenv(f"{prefix}_USERNAME", ""),
        "password": os.getenv(f"{prefix}_PASSWORD", ""),
    }


MACHINE_AUTH_CONFIG: dict[str, dict[str, object]] = {
    machine_name: _machine_auth_config(machine_name) for machine_name in DEFAULT_MACHINE_NAMES
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
    if machine_name not in MACHINE_AUTH_CONFIG:
        raise ValueError(f"Unknown machine {machine_name!r}; expected one of {DEFAULT_MACHINE_NAMES}")
    machine_config = deepcopy(MACHINE_AUTH_CONFIG[machine_name])
    auth_mode = str(machine_config.get("auth_mode") or AUTH_MODE_ANONYMOUS).strip().lower()
    if auth_mode not in ACCEPTED_AUTH_MODES:
        raise ValueError(
            f"Invalid authentication mode for {machine_name}: {auth_mode!r}; "
            f"accepted values are {', '.join(ACCEPTED_AUTH_MODES)}"
        )
    endpoint_override = _normalize_endpoint(machine_config.get("endpoint_url"))
    machine_config["endpoint_url"] = endpoint_override or _normalize_endpoint(csv_endpoint_url)
    machine_config["auth_mode"] = auth_mode
    return machine_config


def get_all_machine_configs(redact_secrets: bool = False) -> dict[str, dict[str, object]]:
    configs = {
        machine_name: get_machine_config(machine_name)
        for machine_name in DEFAULT_MACHINE_NAMES
    }
    if redact_secrets:
        for machine_config in configs.values():
            machine_config["username_configured"] = bool(machine_config.pop("username", ""))
            machine_config["password_configured"] = bool(machine_config.pop("password", ""))
    return configs
