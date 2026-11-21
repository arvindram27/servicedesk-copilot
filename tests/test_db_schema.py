"""Schema-shape tests for migration 0001 (B1 plan tests 1-5)."""

from __future__ import annotations

from pathlib import Path

from db_support import DOMAIN_TABLES, migrated_connection


def test_tables_are_the_expected_seven(tmp_path: Path) -> None:
    with migrated_connection(tmp_path / "schema.db") as connection:
        names = set(
            connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).scalars()
        )
    assert names == {*DOMAIN_TABLES, "alembic_version"}


def test_indexes_and_triggers_exist(tmp_path: Path) -> None:
    with migrated_connection(tmp_path / "objects.db") as connection:
        indexes = set(
            connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).scalars()
        )
        triggers = set(
            connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).scalars()
        )
    assert {
        "ux_tickets_token",
        "ux_tickets_key",
        "ix_keys_status_age",
        "ix_audit_session_seq",
    } <= indexes
    assert triggers == {"trg_audit_no_update", "trg_audit_no_delete"}


def test_connection_pragmas(tmp_path: Path) -> None:
    with migrated_connection(tmp_path / "pragmas.db") as connection:
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
    assert foreign_keys == 1
    assert str(journal_mode).lower() == "wal"


def _not_null_columns(connection: object, table: str) -> dict[str, int]:
    rows = connection.exec_driver_sql(f"PRAGMA table_info('{table}')").all()  # type: ignore[attr-defined]
    return {str(row[1]): int(row[3]) for row in rows}


def test_primary_keys_are_declared_not_null(tmp_path: Path) -> None:
    with migrated_connection(tmp_path / "nullability.db") as connection:
        assert _not_null_columns(connection, "users")["user_id"] == 1
        assert _not_null_columns(connection, "categories")["category_id"] == 1
        assert _not_null_columns(connection, "confirmation_tokens")["token_id"] == 1
        assert _not_null_columns(connection, "tickets")["ticket_id"] == 1
        key_columns = _not_null_columns(connection, "idempotency_keys")
        assert key_columns["owner_run_id"] == 1
        assert key_columns["owner_generation"] == 1


def test_tables_are_strict_and_version_is_0001(tmp_path: Path) -> None:
    with migrated_connection(tmp_path / "strict.db") as connection:
        for table in DOMAIN_TABLES:
            sql = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).scalar()
            assert sql is not None
            assert "STRICT" in str(sql).upper()
        version = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    assert version == "0001"
