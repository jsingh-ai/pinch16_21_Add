from __future__ import annotations

import argparse

from db import connect, transaction


def clear_tag_data(connection) -> tuple[int, int]:
    """Delete samples before tag definitions to satisfy the foreign key."""
    with transaction(connection):
        with connection.cursor() as cursor:
            samples_deleted = int(cursor.execute("DELETE FROM tag_samples"))
            tags_deleted = int(cursor.execute("DELETE FROM tags"))
    return samples_deleted, tags_deleted


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete all Press collector tag samples and tag definitions"
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm permanent deletion of every tag_samples and tags row",
    )
    args = parser.parse_args()
    if not args.yes:
        parser.error("refusing to delete data without --yes")

    connection = connect()
    try:
        samples_deleted, tags_deleted = clear_tag_data(connection)
    finally:
        connection.close()
    print(f"Deleted tag_samples={samples_deleted} tags={tags_deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
