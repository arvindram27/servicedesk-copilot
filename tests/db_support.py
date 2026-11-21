"""Database test helpers (not collected as tests).

Every database lives under a pytest ``tmp_path`` directory inside the
per-invocation basetemp. Nothing here touches ``runtime/servicedesk.db`` and
nothing deletes a file.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from app.db.connection import create_engine_for

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
ALEMBIC_DIR = PROJECT_ROOT / "alembic"

USER_ID = "user-1"
REQUESTER_LABEL = "local-user"
CATEGORY_ID = "cat-hardware"
TOKEN_ID = "token-1"
DRAFT_HASH = "draft-hash-1"
SESSION_ID = "session-1"
TIMESTAMP = "2026-11-20T00:00:00Z"

DOMAIN_TABLES = (
    "users",
    "categories",
    "confirmation_tokens",
    "tickets",
    "idempotency_keys",
    "audit_events",
)


def make_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    return config


def run_upgrade(database_path: Path, revision: str = "head") -> None:
    """Run an upgrade through the programmatic connection path."""
    engine = create_engine_for(database_path)
    try:
        with engine.connect() as connection:
            config = make_config()
            config.attributes["connection"] = connection
            command.upgrade(config, revision)
    finally:
        engine.dispose()


def run_downgrade(database_path: Path, revision: str = "base") -> None:
    """Run a downgrade through the programmatic connection path."""
    engine = create_engine_for(database_path)
    try:
        with engine.connect() as connection:
            config = make_config()
            config.attributes["connection"] = connection
            command.downgrade(config, revision)
    finally:
        engine.dispose()


@contextmanager
def migrated_connection(database_path: Path) -> Iterator[sa.Connection]:
    """Yield a connection to a freshly migrated database file."""
    run_upgrade(database_path)
    engine = create_engine_for(database_path)
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def table_names(connection: sa.Connection) -> set[str]:
    """Domain tables plus alembic_version, excluding sqlite internals."""
    rows = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).scalars()
    return set(rows)


def seed_basics(connection: sa.Connection) -> None:
    """Insert one user, one category and one unconsumed token."""
    connection.exec_driver_sql(
        "INSERT INTO users (user_id, requester_label, display_name, created_at)"
        " VALUES (?, ?, ?, ?)",
        (USER_ID, REQUESTER_LABEL, "Local User", TIMESTAMP),
    )
    connection.exec_driver_sql(
        "INSERT INTO categories (category_id, name, description, active) VALUES (?, ?, ?, 1)",
        (CATEGORY_ID, "Hardware", "Hardware requests"),
    )
    connection.exec_driver_sql(
        "INSERT INTO confirmation_tokens"
        " (token_id, requester_label, session_id, draft_hash, issued_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (TOKEN_ID, REQUESTER_LABEL, SESSION_ID, DRAFT_HASH, TIMESTAMP),
    )
    connection.commit()
