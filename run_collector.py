from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler

from filelock import FileLock, Timeout

from collector import build_arg_parser, collector_loop
from config import (
    COLLECTOR_LOCK_PATH,
    COLLECTOR_LOG_PATH,
    DASHBOARD_HOST,
    DASHBOARD_PORT,
    DEFAULT_OPCUA_READ_BATCH_SIZE,
    MYSQL_DATABASE,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    MYSQL_SSL_CA,
    MYSQL_USER,
    POLL_INTERVAL_SECONDS,
    get_all_machine_configs,
)
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
    COLLECTOR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        COLLECTOR_LOG_PATH,
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
    resolved = {
        "application": "Press 14–15 OPC UA Collector",
        "mysql": {
            "host": MYSQL_HOST,
            "port": MYSQL_PORT,
            "user": MYSQL_USER,
            "database": MYSQL_DATABASE,
            "password_configured": bool(MYSQL_PASSWORD),
            "ssl_ca_configured": bool(MYSQL_SSL_CA),
        },
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        "opcua_read_batch_size": DEFAULT_OPCUA_READ_BATCH_SIZE,
        "collector_lock_path": str(COLLECTOR_LOCK_PATH),
        "collector_log_path": str(COLLECTOR_LOG_PATH),
        "dashboard_host": DASHBOARD_HOST,
        "dashboard_port": DASHBOARD_PORT,
        "machines": get_all_machine_configs(redact_secrets=True),
    }
    print(json.dumps(resolved, indent=2, sort_keys=True))
    return 0


def _print_sync_summaries(summaries) -> None:
    for summary in summaries:
        print(json.dumps(summary.as_dict(), sort_keys=True))


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    configure_logging()

    if args.show_config:
        return show_config()

    if requires_confirmation(args) and not args.yes:
        print(f"Refusing destructive command without --yes.\nRetry with:\n{format_retry_command(sys.argv)}")
        return 2

    conn = get_connection()
    try:
        if args.init_db:
            init_database(conn)
            LOGGER.info("Database table initialization complete in %s", MYSQL_DATABASE)
            return 0

        if args.sync_tags or args.init_only:
            if args.init_only:
                LOGGER.warning("--init-only is deprecated; use --sync-tags. It does not initialize tables.")
            summaries = load_tag_files(conn)
            _print_sync_summaries(summaries)
            return 1 if any(summary.rejected for summary in summaries) else 0

        if args.db_stats:
            print(json.dumps(get_db_stats(conn=conn), indent=2, sort_keys=True))
            return 0

        if args.cleanup_now:
            LOGGER.info("Cleanup finished: %s", cleanup_old_data(conn=conn))
            return 0

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

        COLLECTOR_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
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
            LOGGER.error("Another Press collector instance is already running; polling was not started.")
            return 1
        return 0
    except KeyboardInterrupt:
        LOGGER.info("Press collector stopped by user")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
