from __future__ import annotations

import logging
import sys

from collector import build_arg_parser, collector_loop
from db import get_connection, initialize_database
from tag_loader import load_tag_files


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> int:
    configure_logging()
    parser = build_arg_parser()
    args = parser.parse_args()

    conn = get_connection()
    try:
        initialize_database(conn)
        load_tag_files(conn)

        if args.init_only:
            logging.getLogger(__name__).info("Initialization complete; skipping polling due to --init-only")
            return 0

        collector_loop(
            conn=conn,
            once=args.once,
            interval_seconds=args.interval_seconds,
            align_sleep=not args.no_sleep_align,
        )
        return 0
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Collector stopped by user")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
