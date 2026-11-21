"""Single-use confirmation token (B1 plan tests 18-20).

The concurrency test uses the DBAPI directly so that ``BEGIN IMMEDIATE`` is
explicit transaction control rather than SQLAlchemy-managed autobegin; it
asserts row counts, not wrapped exceptions. PRAGMAs still come from the single
shared helper.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from app.db.connection import apply_pragmas
from sqlalchemy.exc import IntegrityError

from db_support import (
    CATEGORY_ID,
    REQUESTER_LABEL,
    TIMESTAMP,
    TOKEN_ID,
    USER_ID,
    migrated_connection,
    run_upgrade,
    seed_basics,
)

UPDATE_TOKEN = (
    "UPDATE confirmation_tokens SET consumed_at = ?, ticket_id = ?"
    " WHERE token_id = ? AND consumed_at IS NULL"
)
INSERT_TICKET = (
    "INSERT INTO tickets (ticket_id, requester_label, title, description,"
    " category_id, priority, status, idempotency_key, confirmation_token_id, created_at)"
    " VALUES (?, ?, 'Printer', 'Broken', ?, 'normal', 'open', ?, ?, ?)"
)


def test_sequential_consumption_is_single_use(tmp_path: Path) -> None:
    with migrated_connection(tmp_path / "sequential.db") as connection:
        seed_basics(connection)
        connection.exec_driver_sql(
            INSERT_TICKET,
            ("ticket-1", REQUESTER_LABEL, CATEGORY_ID, "key-1", TOKEN_ID, TIMESTAMP),
        )
        first = connection.exec_driver_sql(UPDATE_TOKEN, (TIMESTAMP, "ticket-1", TOKEN_ID))
        assert first.rowcount == 1
        connection.commit()

        second = connection.exec_driver_sql(UPDATE_TOKEN, (TIMESTAMP, "ticket-2", TOKEN_ID))
        assert second.rowcount == 0
        connection.rollback()

        consumed = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM confirmation_tokens WHERE consumed_at IS NOT NULL"
        ).scalar()
        tickets = connection.exec_driver_sql("SELECT COUNT(*) FROM tickets").scalar()
    assert (consumed, tickets) == (1, 1)


def test_concurrent_consumption_has_exactly_one_winner(tmp_path: Path) -> None:
    database_path = tmp_path / "race.db"
    run_upgrade(database_path)

    setup = sqlite3.connect(database_path, isolation_level=None, timeout=5.0)
    try:
        apply_pragmas(setup, database_path)
        setup.execute(
            "INSERT INTO users (user_id, requester_label, display_name, created_at)"
            " VALUES (?, ?, 'Local User', ?)",
            (USER_ID, REQUESTER_LABEL, TIMESTAMP),
        )
        setup.execute(
            "INSERT INTO categories (category_id, name, description, active)"
            " VALUES (?, 'Hardware', 'Hardware requests', 1)",
            (CATEGORY_ID,),
        )
        setup.execute(
            "INSERT INTO confirmation_tokens (token_id, requester_label, session_id,"
            " draft_hash, issued_at) VALUES (?, ?, 'session-1', 'draft-hash-1', ?)",
            (TOKEN_ID, REQUESTER_LABEL, TIMESTAMP),
        )
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    results: dict[str, int] = {}
    errors: list[BaseException] = []
    guard = threading.Lock()

    def worker(name: str) -> None:
        ticket_id = f"ticket-{name}"
        connection = sqlite3.connect(database_path, isolation_level=None, timeout=5.0)
        try:
            apply_pragmas(connection, database_path)
            barrier.wait(timeout=10)  # fires BEFORE BEGIN IMMEDIATE
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(UPDATE_TOKEN, (TIMESTAMP, ticket_id, TOKEN_ID))
                rowcount = cursor.rowcount
                if rowcount == 1:
                    connection.execute(
                        INSERT_TICKET,
                        (
                            ticket_id,
                            REQUESTER_LABEL,
                            CATEGORY_ID,
                            f"key-{name}",
                            TOKEN_ID,
                            TIMESTAMP,
                        ),
                    )
                connection.execute("COMMIT")
                with guard:
                    results[name] = rowcount
            except BaseException as exc:  # recorded, then surfaced by the assertions
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                with guard:
                    errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(results.values()) == [0, 1]

    check = sqlite3.connect(database_path, isolation_level=None, timeout=5.0)
    try:
        apply_pragmas(check, database_path)
        consumed = check.execute(
            "SELECT COUNT(*) FROM confirmation_tokens WHERE consumed_at IS NOT NULL"
        ).fetchone()[0]
        tickets = check.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    finally:
        check.close()
    assert (consumed, tickets) == (1, 1)


def test_consumed_token_cannot_back_a_new_ticket(tmp_path: Path) -> None:
    with migrated_connection(tmp_path / "reuse.db") as connection:
        seed_basics(connection)
        connection.exec_driver_sql(
            INSERT_TICKET,
            ("ticket-1", REQUESTER_LABEL, CATEGORY_ID, "key-1", TOKEN_ID, TIMESTAMP),
        )
        consumed = connection.exec_driver_sql(UPDATE_TOKEN, (TIMESTAMP, "ticket-1", TOKEN_ID))
        assert consumed.rowcount == 1
        connection.commit()

        with pytest.raises(IntegrityError) as excinfo:
            connection.exec_driver_sql(
                INSERT_TICKET,
                ("ticket-2", REQUESTER_LABEL, CATEGORY_ID, "key-2", TOKEN_ID, TIMESTAMP),
            )
        assert isinstance(excinfo.value.orig, sqlite3.IntegrityError)
        connection.rollback()
