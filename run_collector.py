from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler

from filelock import FileLock, Timeout

from collector import build_arg_parser, collector_loop
from config import COLLECTOR_LOCK_PATH, LOGS_DIR, get_all_machine_configs
from db import (
    cleanup_old_data,
    clear_all_samples,
    clear_bad_samples,
    clear_monitoring_data,
    clear_poll_runs,
    get_connection,
    get_db_stats,
    init_database,
)
from tag_loader import load_tag_files

LOGGER = logging.getLogger(__name__)


def configure_logging() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        LOGS_DIR / "collector.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    for logger_name in ("opcua", "opcua.client", "opcua.client.ua_client", "opcua.uaprotocol"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def format_retry_command(argv: list[str]) -> str:
    formatted_args: list[str] = []
    for arg in [*argv, "--yes"]:
        if " " in arg or '"' in arg:
            escaped = arg.replace('"', '\\"')
            formatted_args.append(f'"{escaped}"')
        else:
            formatted_args.append(arg)
    return " ".join(formatted_args)


def requires_confirmation(args) -> bool:
    return any((args.clear_poll_runs, args.clear_bad_samples, args.clear_all_samples, args.clear_monitoring_data))


def show_config() -> int:
    print(json.dumps(get_all_machine_configs(), indent=2, sort_keys=True))
    return 0


def show_db_stats() -> int:
    print(json.dumps(get_db_stats(), indent=2, sort_keys=True))
    return 0


def get_tag_count(conn) -> int:
    with conn.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS count FROM tags")
        row = cursor.fetchone()
    return int(row["count"] or 0)


def main() -> int:
    configure_logging()
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.show_config:
        return show_config()

    conn = get_connection()
    try:
        init_database(conn)

        if args.init_db:
            LOGGER.info("Database schema initialization complete")
            return 0

        if args.db_stats:
            return show_db_stats()

        if args.cleanup_now:
            LOGGER.info("Cleanup finished: %s", cleanup_old_data(conn=conn))
            return 0

        if requires_confirmation(args) and not args.yes:
            print(f"Refusing destructive command without --yes.\nRetry with:\n{format_retry_command(sys.argv)}")
            return 2

        if args.clear_poll_runs:
            LOGGER.warning("Cleared poll_runs deleted=%s", clear_poll_runs(conn=conn))
            return 0

        if args.clear_bad_samples:
            LOGGER.warning("Cleared bad tag_samples deleted=%s", clear_bad_samples(conn=conn))
            return 0

        if args.clear_all_samples:
            LOGGER.warning("Cleared all tag_samples deleted=%s", clear_all_samples(conn=conn))
            return 0

        if args.clear_monitoring_data:
            LOGGER.warning("Cleared monitoring data: %s", clear_monitoring_data(conn=conn))
            return 0

        tag_count = get_tag_count(conn)
        should_import_tags = args.init_only or tag_count == 0
        if should_import_tags:
            load_tag_files(conn)
        else:
            LOGGER.info(
                "Skipping CSV tag import on startup because %s tags already exist in MySQL. Use --init-only when you want to resync tag definitions.",
                tag_count,
            )

        if args.init_only:
            LOGGER.info("Initialization complete; skipping polling due to --init-only")
            return 0

        if not args.allow_multiple:
            lock = FileLock(COLLECTOR_LOCK_PATH)
            try:
                with lock.acquire(timeout=0):
                    collector_loop(
                        conn=conn,
                        once=args.once,
                        interval_seconds=args.interval_seconds,
                        align_sleep=not args.no_sleep_align,
                        machine_name=args.machine,
                    )
            except Timeout:
                LOGGER.error("Another collector instance appears to be running. Use --allow-multiple only if duplicate polling is intentional.")
                return 1
        else:
            collector_loop(
                conn=conn,
                once=args.once,
                interval_seconds=args.interval_seconds,
                align_sleep=not args.no_sleep_align,
                machine_name=args.machine,
            )
        return 0
    except KeyboardInterrupt:
        LOGGER.info("Collector stopped by user")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
