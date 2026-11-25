"""Insert the local demo user and the Hardware category.

The seed uses fixed keys and INSERT OR IGNORE, so a second run changes nothing.
It never creates a schema, deletes a row, or writes a token, ticket, idempotency
key or audit event. An existing Hardware row that is inactive is left as it is.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings

USER_ID = "user-1"
REQUESTER_LABEL = "local-user"
DISPLAY_NAME = "Local User"
CATEGORY_ID = "cat-hardware"
CATEGORY_NAME = "Hardware"
CATEGORY_DESCRIPTION = "Hardware requests"
REQUIRED_TABLES = (
    "users",
    "categories",
    "confirmation_tokens",
    "tickets",
    "idempotency_keys",
    "audit_events",
)


class SeedRefused(RuntimeError):
    """The database is missing or has not been migrated."""


def seed_demo(database_path: Path) -> tuple[int, int]:
    """Insert the demo rows; return the inserted user and category counts."""
    path = Path(database_path)
    if not path.is_file():
        raise SeedRefused(f"database file not found: {path}; run scripts.migrate first")
    connection = sqlite3.connect(path)
    try:
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        missing = [name for name in REQUIRED_TABLES if name not in names]
        if missing:
            joined = ", ".join(missing)
            raise SeedRefused(
                f"database {path} is not migrated; missing tables: {joined}; "
                "run scripts.migrate first"
            )
        now = datetime.now(UTC).isoformat()
        users = connection.execute(
            "INSERT OR IGNORE INTO users (user_id, requester_label, display_name, created_at)"
            " VALUES (?, ?, ?, ?)",
            (USER_ID, REQUESTER_LABEL, DISPLAY_NAME, now),
        ).rowcount
        categories = connection.execute(
            "INSERT OR IGNORE INTO categories (category_id, name, description, active)"
            " VALUES (?, ?, ?, 1)",
            (CATEGORY_ID, CATEGORY_NAME, CATEGORY_DESCRIPTION),
        ).rowcount
        connection.commit()
    finally:
        connection.close()
    return users, categories


def main() -> int:
    """Resolve the configured database path, seed it, and print a summary."""
    database_path = Settings().resolved_database_path
    print(f"database: {database_path}")
    try:
        users, categories = seed_demo(database_path)
    except SeedRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        return 1
    print(f"inserted users={users} categories={categories}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
