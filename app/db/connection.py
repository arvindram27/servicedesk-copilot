"""SQLite engine factory and the single connect-time PRAGMA helper (B1).

Every connection path must go through ``create_engine_for`` or apply
``apply_pragmas`` itself, because ``PRAGMA foreign_keys`` is per connection and
is not stored in the database file. Nothing here connects at import time.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

BUSY_TIMEOUT_MS = 5000


def database_url(database_path: Path) -> URL:
    """Build the SQLite URL without string concatenation (Windows-safe)."""
    return URL.create("sqlite", database=str(database_path))


def apply_pragmas(
    dbapi_connection: sqlite3.Connection,
    database_path: Path | None,
) -> None:
    """Apply the connect-time PRAGMAs; ``None`` skips the file-only WAL pragma."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        if database_path is not None:
            cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


def create_engine_for(database_path: Path) -> Engine:
    """Create a SQLite engine whose every connection carries the PRAGMAs."""
    engine = create_engine(database_url(database_path), poolclass=NullPool)

    def _on_connect(dbapi_connection: Any, _connection_record: Any) -> None:
        apply_pragmas(dbapi_connection, database_path)

    event.listen(engine, "connect", _on_connect)
    return engine
