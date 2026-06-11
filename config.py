from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TAG_FILES_DIR = BASE_DIR / "Tag_Files"
DATA_DIR = BASE_DIR / "data"
SQLITE_DB_PATH = DATA_DIR / "opcua_history.sqlite3"

DEFAULT_POLL_INTERVAL_SECONDS = 60
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_SESSION_TIMEOUT_MS = 60000
DEFAULT_TAG_FAILURE_LOG_SAMPLE = 5

AUTH_MODE_ANONYMOUS = "anonymous"
AUTH_MODE_USERNAME_BLANK_BASIC256_TOKEN = "username_blank_basic256_token"

DEFAULT_MACHINE_NAMES = [
    "Pinch 16",
    "Pinch 17",
    "Pinch 18",
    "Pinch 19",
    "Pinch 20",
    "Pinch 21",
]

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
        "auth_mode": AUTH_MODE_ANONYMOUS,
        "enabled": True,
        "endpoint_url": None,
    },
    "Pinch 21": {
        "auth_mode": AUTH_MODE_ANONYMOUS,
        "enabled": True,
        "endpoint_url": None,
    },
}
