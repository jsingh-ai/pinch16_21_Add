from __future__ import annotations

import json

from config import MYSQL_DATABASE, MYSQL_HOST, MYSQL_PORT, MYSQL_USER
from db import get_connection


def main() -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT VERSION() AS version, DATABASE() AS database_name, @@session.time_zone AS session_time_zone")
            row = cursor.fetchone()
        print(
            json.dumps(
                {
                    "host": MYSQL_HOST,
                    "port": MYSQL_PORT,
                    "user": MYSQL_USER,
                    "database": MYSQL_DATABASE,
                    "server_version": row["version"],
                    "database_name": row["database_name"],
                    "session_time_zone": row["session_time_zone"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
