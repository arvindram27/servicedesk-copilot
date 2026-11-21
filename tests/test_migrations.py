"""Migration round-trip, idempotency and offline SQL (B1 plan tests 6-9)."""

from __future__ import annotations

import io
import re
from contextlib import redirect_stdout
from pathlib import Path

from alembic import command
from app.db.connection import create_engine_for

from db_support import (
    DOMAIN_TABLES,
    PROJECT_ROOT,
    make_config,
    migrated_connection,
    run_downgrade,
    run_upgrade,
    table_names,
)


def test_upgrade_downgrade_upgrade_round_trip(tmp_path: Path) -> None:
    database_path = tmp_path / "roundtrip.db"
    run_upgrade(database_path)
    run_downgrade(database_path)
    run_upgrade(database_path)
    with migrated_connection(database_path) as connection:
        assert table_names(connection) == {*DOMAIN_TABLES, "alembic_version"}


def test_upgrade_twice_is_a_noop(tmp_path: Path) -> None:
    database_path = tmp_path / "idempotent.db"
    run_upgrade(database_path)
    run_upgrade(database_path)
    with migrated_connection(database_path) as connection:
        version = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    assert version == "0001"


def test_offline_sql_names_every_object(tmp_path: Path) -> None:
    del tmp_path  # offline mode writes nothing anywhere
    config = make_config()
    buffer = io.StringIO()
    config.output_buffer = buffer
    with redirect_stdout(buffer):
        command.upgrade(config, "head", sql=True)

    text = buffer.getvalue()
    created = re.findall(r"CREATE TABLE ([a-z_]+) \(", text)
    for table in DOMAIN_TABLES:
        assert created.count(table) == 1, table
    assert created.count("alembic_version") == 1

    triggers = re.findall(r"CREATE TRIGGER (\w+)", text)
    assert sorted(triggers) == ["trg_audit_no_delete", "trg_audit_no_update"]
    assert text.upper().count("STRICT") == len(DOMAIN_TABLES)
    assert not (PROJECT_ROOT / "runtime" / "servicedesk.db").exists()


def test_downgrade_to_base_leaves_only_alembic_version(tmp_path: Path) -> None:
    database_path = tmp_path / "downgraded.db"
    run_upgrade(database_path)
    run_downgrade(database_path)

    engine = create_engine_for(database_path)
    try:
        with engine.connect() as connection:
            remaining = table_names(connection)
    finally:
        engine.dispose()

    assert remaining == {"alembic_version"}
