"""Demo seed tests: rows, idempotency and refusal."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from scripts.seed_demo import SeedRefused, seed_demo

from db_support import run_upgrade


def _count(database: Path, table: str) -> int:
    connection = sqlite3.connect(database)
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def test_seed_inserts_one_user_and_one_category(tmp_path: Path) -> None:
    database = tmp_path / "seed.db"
    run_upgrade(database)

    users, categories = seed_demo(database)

    assert (users, categories) == (1, 1)
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT user_id, requester_label, display_name FROM users"
        ).fetchall() == [("user-1", "local-user", "Local User")]
        assert connection.execute(
            "SELECT category_id, name, active FROM categories"
        ).fetchall() == [("cat-hardware", "Hardware", 1)]
    finally:
        connection.close()
    for table in ("tickets", "confirmation_tokens", "idempotency_keys"):
        assert _count(database, table) == 0


def test_seed_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "seed.db"
    run_upgrade(database)
    assert seed_demo(database) == (1, 1)

    assert seed_demo(database) == (0, 0)
    assert _count(database, "users") == 1
    assert _count(database, "categories") == 1


def test_seed_refuses_missing_or_unmigrated_database(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    with pytest.raises(SeedRefused) as missing_error:
        seed_demo(missing)
    assert str(missing) in str(missing_error.value)
    assert not missing.exists()

    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with pytest.raises(SeedRefused) as empty_error:
        seed_demo(empty)
    assert str(empty) in str(empty_error.value)
    connection = sqlite3.connect(empty)
    try:
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        connection.close()
    assert names == set()
