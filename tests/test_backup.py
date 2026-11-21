"""Tests for the online backup command (B1b).

These are tool tests, not a restore drill: seeded rows use direct SQL and only
prove the artifact is readable, not that it is equivalent to a changing source.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
import scripts.backup as backup_module
from app.config import Settings

from db_support import (
    CATEGORY_ID,
    REQUESTER_LABEL,
    TIMESTAMP,
    TOKEN_ID,
    migrated_connection,
    seed_basics,
)

INSERT_TICKET = (
    "INSERT INTO tickets (ticket_id, requester_label, title, description,"
    " category_id, priority, status, idempotency_key, confirmation_token_id, created_at)"
    " VALUES ('ticket-1', ?, 'Printer', 'Broken', ?, 'normal', 'open', 'key-1', ?, ?)"
)


def _seed(database_path: Path) -> None:
    """Seed a migrated database with direct SQL."""
    with migrated_connection(database_path) as connection:
        seed_basics(connection)
        connection.exec_driver_sql(
            INSERT_TICKET, (REQUESTER_LABEL, CATEGORY_ID, TOKEN_ID, TIMESTAMP)
        )
        connection.exec_driver_sql(
            "INSERT INTO audit_events (event_id, occurred_at, event_type, payload_json)"
            " VALUES ('evt-seed', ?, 'seed', '{}')",
            (TIMESTAMP,),
        )
        connection.commit()


def _rows(database_path: Path) -> dict[str, int]:
    connection = sqlite3.connect(database_path)
    try:
        counts: dict[str, int] = {}
        for table in ("users", "categories", "tickets", "confirmation_tokens", "audit_events"):
            counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return counts
    finally:
        connection.close()


def _manifest(result: backup_module.BackupResult) -> dict[str, object]:
    return json.loads(result.manifest_path.read_text(encoding="utf-8"))


def test_creates_finalized_artifact_and_manifest(tmp_path: Path) -> None:
    source = tmp_path / "service.db"
    _seed(source)
    result = backup_module.create_backup(source, tmp_path / "backups")

    assert result.backup_path.is_file()
    assert result.manifest_path.is_file()
    assert result.backup_path.name.endswith(".db")
    assert result.manifest_path.name.endswith(".db.manifest.json")
    assert result.integrity_check == "ok"
    assert result.schema_version == "0001"
    assert result.journal_mode == "delete"


def test_artifact_is_delete_journal_mode_without_wal_sidecar(tmp_path: Path) -> None:
    source = tmp_path / "service.db"
    _seed(source)
    result = backup_module.create_backup(source, tmp_path / "backups")

    connection = sqlite3.connect(result.backup_path)
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()

    assert str(mode).lower() == "delete"
    assert not result.backup_path.with_name(result.backup_path.name + "-wal").exists()


def test_artifact_integrity_and_seeded_rows_present(tmp_path: Path) -> None:
    source = tmp_path / "service.db"
    _seed(source)
    result = backup_module.create_backup(source, tmp_path / "backups")

    connection = sqlite3.connect(backup_module.read_only_uri(result.backup_path), uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tickets = connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        tokens = connection.execute("SELECT COUNT(*) FROM confirmation_tokens").fetchone()[0]
    finally:
        connection.close()

    assert integrity == "ok"
    assert (tickets, tokens) == (1, 1)


def test_manifest_digest_matches_final_artifact(tmp_path: Path) -> None:
    source = tmp_path / "service.db"
    _seed(source)
    result = backup_module.create_backup(source, tmp_path / "backups")

    manifest = _manifest(result)
    assert manifest["sha256"] == backup_module._sha256(result.backup_path)
    assert manifest["backup_size_bytes"] == result.backup_path.stat().st_size
    assert manifest["schema_version"] == "0001"
    assert manifest["journal_mode"] == "delete"
    assert manifest["integrity_check"] == "ok"
    assert manifest["timeout_seconds"] == backup_module.DEFAULT_TIMEOUT_S


def test_reopening_quiescent_artifact_read_only_keeps_digest(tmp_path: Path) -> None:
    source = tmp_path / "service.db"
    _seed(source)
    result = backup_module.create_backup(source, tmp_path / "backups")

    before = backup_module._sha256(result.backup_path)
    connection = sqlite3.connect(backup_module.read_only_uri(result.backup_path), uri=True)
    try:
        connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    after = backup_module._sha256(result.backup_path)

    assert before == after == result.sha256


def test_seeded_source_logically_preserved(tmp_path: Path) -> None:
    source = tmp_path / "service.db"
    _seed(source)
    before = _rows(source)
    backup_module.create_backup(source, tmp_path / "backups")

    connection = sqlite3.connect(source)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        connection.close()

    assert _rows(source) == before
    assert integrity == "ok"
    assert journal_mode == "wal"


def test_missing_source_is_refused_without_creating_database(tmp_path: Path) -> None:
    source = tmp_path / "missing.db"
    with pytest.raises(backup_module.BackupRefused):
        backup_module.create_backup(source, tmp_path / "backups")

    assert not source.exists()
    assert not (tmp_path / "backups").exists()


def test_directory_source_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "not-a-database"
    source.mkdir()
    with pytest.raises(backup_module.BackupRefused):
        backup_module.create_backup(source, tmp_path / "backups")

    assert not (tmp_path / "backups").exists()


def test_existing_artifact_is_never_overwritten(tmp_path: Path) -> None:
    source = tmp_path / "service.db"
    _seed(source)
    existing = tmp_path / "existing.db"
    existing.write_bytes(b"not a database")
    before = existing.read_bytes()

    with pytest.raises(backup_module.BackupRefused):
        backup_module.create_backup(source, tmp_path / "backups", destination=existing)

    assert existing.read_bytes() == before


def test_source_filename_with_spaces_hash_and_percent(tmp_path: Path) -> None:
    source = tmp_path / "service db #1 100%.db"
    _seed(source)
    result = backup_module.create_backup(source, tmp_path / "backups")

    assert result.backup_path.is_file()
    assert result.integrity_check == "ok"


def test_source_filename_with_question_mark_where_permitted(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("'?' is not permitted in Windows file names")
    source = tmp_path / "service db ?1.db"
    _seed(source)
    result = backup_module.create_backup(source, tmp_path / "backups")

    assert result.integrity_check == "ok"


class _ExpiringClock:
    """Fake monotonic clock that expires on a chosen call number."""

    def __init__(self, expire_at_call: int) -> None:
        self._expire_at_call = expire_at_call
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return 1000.0 if self.calls >= self._expire_at_call else 0.0


def test_deadline_expiry_raises_and_retains_artifact(tmp_path: Path) -> None:
    source = tmp_path / "service.db"
    _seed(source)
    backups = tmp_path / "backups"
    clock = _ExpiringClock(expire_at_call=3)

    with pytest.raises(backup_module.BackupDeadlineExceeded):
        backup_module.create_backup(source, backups, clock=clock)

    assert list(backups.glob("*.db"))
    assert list(backups.glob("*.manifest.json")) == []


def test_pre_expired_deadline_creates_no_artifact(tmp_path: Path) -> None:
    source = tmp_path / "service.db"
    _seed(source)
    backups = tmp_path / "backups"

    with pytest.raises(backup_module.BackupDeadlineExceeded):
        backup_module.create_backup(source, backups, timeout_s=0)

    assert not backups.exists()


def test_progress_callback_raises_on_expiry() -> None:
    hook = backup_module._deadline_hook(lambda: 1000.0, 10.0)
    with pytest.raises(backup_module.BackupDeadlineExceeded):
        hook(0, 10, 10)


@pytest.mark.parametrize("timeout_s", [float("nan"), float("inf"), -1.0])
def test_invalid_timeouts_are_rejected_before_artifact(tmp_path: Path, timeout_s: float) -> None:
    source = tmp_path / "service.db"
    _seed(source)
    backups = tmp_path / "backups"

    with pytest.raises(backup_module.BackupRefused):
        backup_module.create_backup(source, backups, timeout_s=timeout_s)

    assert not backups.exists()


def test_journal_mode_failure_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "service.db"
    _seed(source)
    backups = tmp_path / "backups"
    monkeypatch.setattr(backup_module, "_set_delete_journal_mode", lambda connection: "wal")

    with pytest.raises(backup_module.BackupError):
        backup_module.create_backup(source, backups)

    assert list(backups.glob("*.db"))
    assert list(backups.glob("*.manifest.json")) == []


def test_integrity_failure_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "service.db"
    _seed(source)
    backups = tmp_path / "backups"
    monkeypatch.setattr(backup_module, "_read_artifact", lambda path: ("corrupt", None, "delete"))

    with pytest.raises(backup_module.BackupError):
        backup_module.create_backup(source, backups)

    assert list(backups.glob("*.db"))
    assert list(backups.glob("*.manifest.json")) == []


def test_manifest_creation_failure_aborts_and_keeps_existing_manifest(
    tmp_path: Path,
) -> None:
    source = tmp_path / "service.db"
    _seed(source)
    artifact = tmp_path / "artifact.db"
    manifest_path = artifact.with_name(artifact.name + ".manifest.json")
    manifest_path.write_text("existing manifest bytes\n", encoding="utf-8")
    before = manifest_path.read_bytes()

    with pytest.raises(backup_module.BackupRefused):
        backup_module.create_backup(source, tmp_path / "backups", destination=artifact)

    assert artifact.is_file()
    assert manifest_path.read_bytes() == before


def test_unversioned_source_backs_up_with_null_revision(tmp_path: Path) -> None:
    source = tmp_path / "unversioned.db"
    connection = sqlite3.connect(source)
    try:
        connection.execute("CREATE TABLE notes (body TEXT NOT NULL) STRICT")
        connection.execute("INSERT INTO notes (body) VALUES ('hello')")
        connection.commit()
    finally:
        connection.close()

    result = backup_module.create_backup(source, tmp_path / "backups")

    assert result.schema_version is None
    assert _manifest(result)["schema_version"] is None
    assert result.integrity_check == "ok"


def test_empty_version_table_is_base(tmp_path: Path) -> None:
    source = tmp_path / "empty-version.db"
    connection = sqlite3.connect(source)
    try:
        connection.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL"
            " CONSTRAINT alembic_version_pkc PRIMARY KEY)"
        )
        connection.commit()
    finally:
        connection.close()

    result = backup_module.create_backup(source, tmp_path / "backups")

    assert result.schema_version is None


def test_malformed_metadata_fails_visibly(tmp_path: Path) -> None:
    source = tmp_path / "malformed.db"
    connection = sqlite3.connect(source)
    try:
        connection.execute("CREATE TABLE alembic_version (not_the_revision TEXT)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(backup_module.BackupError):
        backup_module.create_backup(source, tmp_path / "backups")


def test_stat_error_is_not_treated_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "service.db"

    def _denied(self: Path, *args: object, **kwargs: object) -> object:
        raise PermissionError("simulated permission failure")

    monkeypatch.setattr(Path, "stat", _denied)
    with pytest.raises(backup_module.BackupError):
        backup_module.classify_source(source)


def test_invalid_sqlite_source_raises_managed_error(tmp_path: Path) -> None:
    source = tmp_path / "garbage.db"
    source.write_bytes(b"this is not a sqlite database")
    backups = tmp_path / "backups"

    with pytest.raises(backup_module.BackupError) as excinfo:
        backup_module.create_backup(source, backups)

    assert "stage" in str(excinfo.value)
    assert list(backups.glob("*.manifest.json")) == []


def test_invalid_sqlite_source_cli_reports_handled_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    source = runtime / "garbage.db"
    source.write_bytes(b"this is not a sqlite database")

    code = backup_module.main(["--database", str(source), "--runtime-dir", str(runtime)])

    captured = capsys.readouterr()
    assert code == 1
    assert "backup failed" in captured.out
    assert not list((runtime / "backups").glob("*.manifest.json"))


def test_directory_stage_failure_is_managed(tmp_path: Path) -> None:
    source = tmp_path / "service.db"
    _seed(source)
    blocked = tmp_path / "backups"
    blocked.write_text("not a directory", encoding="utf-8")

    with pytest.raises(backup_module.BackupError) as excinfo:
        backup_module.create_backup(source, blocked)

    assert "directory stage" in str(excinfo.value)


def test_normalization_stage_failure_is_managed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "service.db"
    _seed(source)
    backups = tmp_path / "backups"

    def _boom(connection: object) -> str:
        raise sqlite3.OperationalError("simulated normalization failure")

    monkeypatch.setattr(backup_module, "_set_delete_journal_mode", _boom)
    with pytest.raises(backup_module.BackupError) as excinfo:
        backup_module.create_backup(source, backups)

    assert "normalization stage" in str(excinfo.value)
    assert list(backups.glob("*.db"))
    assert list(backups.glob("*.manifest.json")) == []


def test_verification_stage_failure_is_managed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "service.db"
    _seed(source)
    backups = tmp_path / "backups"

    def _boom(path: Path) -> object:
        raise sqlite3.OperationalError("simulated verification failure")

    monkeypatch.setattr(backup_module, "_read_artifact", _boom)
    with pytest.raises(backup_module.BackupError) as excinfo:
        backup_module.create_backup(source, backups)

    assert "artifact verification stage" in str(excinfo.value)
    assert list(backups.glob("*.db"))
    assert list(backups.glob("*.manifest.json")) == []


def test_digest_stage_failure_is_managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "service.db"
    _seed(source)
    backups = tmp_path / "backups"

    def _boom(path: Path) -> str:
        raise OSError("simulated digest failure")

    monkeypatch.setattr(backup_module, "_sha256", _boom)
    with pytest.raises(backup_module.BackupError) as excinfo:
        backup_module.create_backup(source, backups)

    assert "digest stage" in str(excinfo.value)
    assert list(backups.glob("*.db"))
    assert list(backups.glob("*.manifest.json")) == []


def test_help_performs_no_database_work(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        backup_module.main(["--help"])

    assert excinfo.value.code == 0
    assert "backup" in capsys.readouterr().out
    assert not Settings().resolved_database_path.exists()
