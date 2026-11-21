"""Alembic environment: online, offline and programmatic modes (B1).

The connection-time PRAGMAs come from the single helper in
app.db.connection, so migrations and the future application engine configure
SQLite identically.
"""

from __future__ import annotations

from pathlib import Path

from alembic import context
from app.config import Settings
from app.db.connection import create_engine_for, database_url
from app.db.schema import metadata as target_metadata
from sqlalchemy import Connection

config = context.config


def _database_path() -> Path:
    return Settings().resolved_database_path


def _run_with_connection(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """Emit SQL without connecting or creating any database file."""
    context.configure(
        url=database_url(_database_path()),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    injected = config.attributes.get("connection")
    if injected is not None:
        _run_with_connection(injected)
        return
    engine = create_engine_for(_database_path())
    try:
        with engine.connect() as connection:
            _run_with_connection(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
