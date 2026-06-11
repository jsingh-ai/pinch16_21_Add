from __future__ import annotations

import logging
import sys

from collector import build_arg_parser, collector_loop
from db import (
    cleanup_old_data,
    clear_all_samples,
    clear_bad_samples,
    clear_monitoring_data,
    clear_poll_runs,
    get_connection,
    initialize_database,
)
from tag_loader import load_tag_files


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for logger_name in (
        "opcua",
        "opcua.client",
        "opcua.client.ua_client",
        "opcua.uaprotocol",
    ):
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
    return any(
        (
            args.clear_poll_runs,
            args.clear_bad_samples,
            args.clear_all_samples,
            args.clear_monitoring_data,
        )
    )


def main() -> int:
    configure_logging()
    parser = build_arg_parser()
    args = parser.parse_args()

    conn = get_connection()
    try:
        initialize_database(conn)
        load_tag_files(conn)

        if args.cleanup_now:
            results = cleanup_old_data(conn=conn)
            logging.getLogger(__name__).info("Cleanup finished: %s", results)
            return 0

        if requires_confirmation(args) and not args.yes:
            retry_command = format_retry_command(sys.argv)
            print(f"Refusing destructive command without --yes.\nRetry with:\n{retry_command}")
            return 2

        if args.clear_poll_runs:
            deleted = clear_poll_runs(conn=conn)
            logging.getLogger(__name__).warning("Cleared poll_runs deleted=%s", deleted)
            return 0

        if args.clear_bad_samples:
            deleted = clear_bad_samples(conn=conn)
            logging.getLogger(__name__).warning("Cleared bad tag_samples deleted=%s", deleted)
            return 0

        if args.clear_all_samples:
            deleted = clear_all_samples(conn=conn)
            logging.getLogger(__name__).warning("Cleared all tag_samples deleted=%s", deleted)
            return 0

        if args.clear_monitoring_data:
            deleted = clear_monitoring_data(conn=conn)
            logging.getLogger(__name__).warning("Cleared monitoring data: %s", deleted)
            return 0

        if args.init_only:
            logging.getLogger(__name__).info("Initialization complete; skipping polling due to --init-only")
            return 0

        collector_loop(
            conn=conn,
            once=args.once,
            interval_seconds=args.interval_seconds,
            align_sleep=not args.no_sleep_align,
            machine_name=args.machine,
        )
        return 0
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Collector stopped by user")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
