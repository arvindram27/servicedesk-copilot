"""Tests for the manual migration wrapper (B1b).

These cover cooperating wrappers only. The cooperative lock cannot exclude
arbitrary SQLite writers, direct Alembic runs or an application that does not
honour it.
"""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import scripts.migrate as migrate_module
from app.config import Settings
from sqlalchemy.exc import SQLAlchemyError

from db_support import PROJECT_ROOT, run_upgrade

LOCK_WORKER = """
import sys
import time
from pathlib import Path

from scripts.migrate import MigrationLock

lock = MigrationLock(Path(sys.argv[1]))
lock.acquire()
Path(sys.argv[2]).write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 15
stop = Path(sys.argv[3])
while time.monotonic() < deadline and not stop.exists():
    time.sleep(0.02)
lock.release()
"""


def _events(database_path: Path) -> list[dict[str, object]]:
    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute("SELECT payload_json FROM audit_events ORDER BY seq").fetchall()
    finally:
        connection.close()
    return [json.loads(row[0]) for row in rows]


def _version(database_path: Path) -> str | None:
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        connection.close()
    return None if row is None else str(row[0])


def _runtime(tmp_path: Path) -> tuple[Path, Path]:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return runtime, runtime / "copilot.db"


def _make_unversioned_database(path: Path) -> None:
    """A valid SQLite database with no alembic_version table."""
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE notes (body TEXT NOT NULL) STRICT")
        connection.execute("INSERT INTO notes (body) VALUES ('keep me')")
        connection.commit()
    finally:
        connection.close()


def test_first_run_creates_database_and_records_skip_reason(tmp_path: Path) -> None:
    runtime, database = _runtime(tmp_path)
    buffer = io.StringIO()

    code = migrate_module.run_wrapper(database, runtime_dir=runtime, stream=buffer)

    assert code == 0
    assert database.is_file()
    assert _version(database) == "0001"
    assert "skipped (source_missing_first_run)" in buffer.getvalue()
    payload = _events(database)[0]
    assert payload["skipped_reason"] == "source_missing_first_run"
    assert payload["from_revision"] is None
    assert payload["backup_path"] is None


def test_existing_source_takes_backup_and_records_revisions(tmp_path: Path) -> None:
    runtime, database = _runtime(tmp_path)
    run_upgrade(database)
    buffer = io.StringIO()

    code = migrate_module.run_wrapper(database, runtime_dir=runtime, stream=buffer)

    assert code == 0
    artifacts = list((runtime / "backups").glob("*.db"))
    assert len(artifacts) == 1
    payload = _events(database)[-1]
    assert payload["from_revision"] == "0001"
    assert payload["to_revision"] == "0001"
    assert payload["skipped_reason"] is None
    assert payload["backup_path"] is not None
    assert Path(str(payload["backup_path"])).is_file()


def test_repeat_at_head_is_a_schema_no_change_run_with_backup_and_audit(
    tmp_path: Path,
) -> None:
    runtime, database = _runtime(tmp_path)
    run_upgrade(database)
    migrate_module.run_wrapper(database, runtime_dir=runtime, stream=io.StringIO())
    migrate_module.run_wrapper(database, runtime_dir=runtime, stream=io.StringIO())

    assert len(list((runtime / "backups").glob("*.db"))) == 2
    events = _events(database)
    assert len(events) == 2
    assert events[-1]["from_revision"] == events[-1]["to_revision"] == "0001"
    assert events[-1]["schema_no_change"] is True


def test_failed_backup_aborts_before_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, database = _runtime(tmp_path)
    run_upgrade(database)
    calls: list[int] = []
    monkeypatch.setattr(migrate_module.command, "upgrade", lambda *a, **k: calls.append(1))

    def _fail(*args: object, **kwargs: object) -> object:
        raise migrate_module.BackupError("simulated backup failure")

    monkeypatch.setattr(migrate_module, "create_backup", _fail)

    code = migrate_module.run_wrapper(database, runtime_dir=runtime, stream=io.StringIO())

    assert code == 1
    assert calls == []
    assert _version(database) == "0001"


def test_revision_mismatch_aborts_before_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, database = _runtime(tmp_path)
    run_upgrade(database)
    calls: list[int] = []
    monkeypatch.setattr(migrate_module.command, "upgrade", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(
        migrate_module,
        "create_backup",
        lambda *a, **k: SimpleNamespace(backup_path=tmp_path / "unused.db", schema_version="0000"),
    )

    code = migrate_module.run_wrapper(database, runtime_dir=runtime, stream=io.StringIO())

    assert code == 1
    assert calls == []


def test_upgrade_failure_writes_no_audit_and_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, database = _runtime(tmp_path)
    migrate_module.run_wrapper(database, runtime_dir=runtime, stream=io.StringIO())
    audit_rows_before = len(_events(database))

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated upgrade failure")

    monkeypatch.setattr(migrate_module.command, "upgrade", _boom)

    code = migrate_module.run_wrapper(database, runtime_dir=runtime, stream=io.StringIO())

    assert code == 1
    assert len(_events(database)) == audit_rows_before
    lock = migrate_module.MigrationLock(migrate_module.lock_path_for(database))
    lock.acquire()
    lock.release()


def test_audit_failure_after_upgrade_reports_distinctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, database = _runtime(tmp_path)
    _make_unversioned_database(database)

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(migrate_module, "_insert_audit_event", _boom)
    buffer = io.StringIO()

    code = migrate_module.run_wrapper(database, runtime_dir=runtime, stream=buffer)

    assert code == 1
    assert "upgrade applied, audit event failed" in buffer.getvalue()
    assert _version(database) == "0001"
    assert _events(database) == []
    lock = migrate_module.MigrationLock(migrate_module.lock_path_for(database))
    lock.acquire()
    lock.release()


def test_lock_contention_refuses_without_backup_or_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, database = _runtime(tmp_path)
    lock_path = migrate_module.lock_path_for(database)
    ready = tmp_path / "ready.flag"
    stop = tmp_path / "stop.flag"
    calls: list[int] = []
    monkeypatch.setattr(migrate_module.command, "upgrade", lambda *a, **k: calls.append(1))

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            LOCK_WORKER,
            str(lock_path),
            str(ready),
            str(stop),
        ],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "lock holder never became ready"

        code = migrate_module.run_wrapper(database, runtime_dir=runtime, stream=io.StringIO())

        assert code == 2
        assert calls == []
        assert not database.exists()
        assert not (runtime / "backups").exists()
    finally:
        stop.write_text("stop", encoding="utf-8")
        try:
            process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)

    assert process.returncode == 0
    monkeypatch.undo()

    code = migrate_module.run_wrapper(database, runtime_dir=runtime, stream=io.StringIO())

    assert code == 0
    assert database.is_file()
    assert lock_path.exists()


def test_directory_source_refuses_before_database_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    database = runtime / "a-directory"
    database.mkdir()
    calls: list[int] = []
    monkeypatch.setattr(migrate_module.command, "upgrade", lambda *a, **k: calls.append(1))

    code = migrate_module.run_wrapper(database, runtime_dir=runtime, stream=io.StringIO())

    assert code == 2
    assert calls == []
    assert not (runtime / "backups").exists()


def test_classification_error_is_not_treated_as_first_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, database = _runtime(tmp_path)

    def _fail(path: Path) -> bool:
        raise migrate_module.BackupError("simulated stat failure")

    monkeypatch.setattr(migrate_module, "classify_source", _fail)
    buffer = io.StringIO()

    code = migrate_module.run_wrapper(database, runtime_dir=runtime, stream=buffer)

    assert code == 1
    assert "migration failed" in buffer.getvalue()
    assert not database.exists()


def test_unsupported_platform_refuses_before_database_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, database = _runtime(tmp_path)
    monkeypatch.setattr(migrate_module, "msvcrt", None)
    monkeypatch.setattr(migrate_module, "fcntl", None)

    code = migrate_module.run_wrapper(database, runtime_dir=runtime, stream=io.StringIO())

    assert code == 2
    assert not database.exists()


def test_unexpected_lock_error_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, database = _runtime(tmp_path)

    def _fail(descriptor: int) -> None:
        raise migrate_module.MigrationLockError("simulated unexpected lock error")

    monkeypatch.setattr(migrate_module, "_try_lock", _fail)

    buffer = io.StringIO()
    code = migrate_module.run_wrapper(database, runtime_dir=runtime, stream=buffer)

    assert code == 1
    assert "migration failed" in buffer.getvalue()
    assert not database.exists()


def test_out_of_runtime_database_path_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside.db"

    code = migrate_module.main(["--database", str(outside)])

    assert code == 2
    assert not outside.exists()


def test_help_performs_no_database_work(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        migrate_module.main(["--help"])

    assert excinfo.value.code == 0
    assert "migrate" in capsys.readouterr().out
    assert not Settings().resolved_database_path.exists()


@pytest.mark.parametrize("timeout_s", [float("nan"), float("inf"), -1.0])
def test_invalid_wrapper_timeout_refused_before_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout_s: float
) -> None:
    runtime = tmp_path / "runtime-not-created"
    database = runtime / "copilot.db"
    calls: list[int] = []
    monkeypatch.setattr(migrate_module.command, "upgrade", lambda *a, **k: calls.append(1))
    buffer = io.StringIO()

    code = migrate_module.run_wrapper(
        database, runtime_dir=runtime, timeout_s=timeout_s, stream=buffer
    )

    assert code == 2
    assert "refused" in buffer.getvalue()
    assert not runtime.exists()
    assert not database.exists()
    assert calls == []


def test_invalid_timeout_cli_returns_json_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = tmp_path / "runtime-not-created"
    database = runtime / "copilot.db"

    code = migrate_module.main(
        [
            "--database",
            str(database),
            "--runtime-dir",
            str(runtime),
            "--timeout-seconds",
            "nan",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["exit_code"] == 2
    assert any("refused" in line for line in payload["lines"])
    assert not runtime.exists()


def test_engine_setup_failure_is_stage_specific(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, database = _runtime(tmp_path)
    run_upgrade(database)
    calls: list[int] = []
    monkeypatch.setattr(migrate_module.command, "upgrade", lambda *a, **k: calls.append(1))

    def _boom(path: Path) -> object:
        raise SQLAlchemyError("simulated engine failure")

    monkeypatch.setattr(migrate_module, "create_engine_for", _boom)
    buffer = io.StringIO()

    code = migrate_module.run_wrapper(database, runtime_dir=runtime, stream=buffer)

    assert code == 1
    assert "database setup stage" in buffer.getvalue()
    assert calls == []
    lock = migrate_module.MigrationLock(migrate_module.lock_path_for(database))
    lock.acquire()
    lock.release()


def test_revision_read_failure_is_stage_specific(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, database = _runtime(tmp_path)
    run_upgrade(database)
    calls: list[int] = []
    monkeypatch.setattr(migrate_module.command, "upgrade", lambda *a, **k: calls.append(1))

    def _boom(connection: object) -> str | None:
        raise sqlite3.OperationalError("simulated revision read failure")

    monkeypatch.setattr(migrate_module, "_read_revision", _boom)
    buffer = io.StringIO()

    code = migrate_module.run_wrapper(database, runtime_dir=runtime, stream=buffer)

    assert code == 1
    assert "revision read stage" in buffer.getvalue()
    assert calls == []


def test_invalid_sqlite_source_wrapper_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, database = _runtime(tmp_path)
    database.write_bytes(b"this is not a sqlite database")
    calls: list[int] = []
    monkeypatch.setattr(migrate_module.command, "upgrade", lambda *a, **k: calls.append(1))
    buffer = io.StringIO()

    code = migrate_module.run_wrapper(database, runtime_dir=runtime, stream=buffer)

    assert code == 1
    assert "migration aborted: backup failed or unverified" in buffer.getvalue()
    assert calls == []
    assert list((runtime / "backups").glob("*.db"))
    assert list((runtime / "backups").glob("*.manifest.json")) == []


def test_existing_unversioned_source_upgrades_with_real_backup_and_audit(
    tmp_path: Path,
) -> None:
    runtime, database = _runtime(tmp_path)
    _make_unversioned_database(database)
    buffer = io.StringIO()

    code = migrate_module.run_wrapper(database, runtime_dir=runtime, stream=buffer)

    assert code == 0
    assert _version(database) == "0001"
    payload = _events(database)[-1]
    assert payload["from_revision"] is None
    assert payload["to_revision"] == "0001"
    assert payload["skipped_reason"] is None
    assert payload["backup_path"] is not None

    backup_path = Path(str(payload["backup_path"]))
    assert backup_path.is_file()
    manifest = json.loads(
        backup_path.with_name(backup_path.name + ".manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["schema_version"] is None

    connection = sqlite3.connect(database)
    try:
        notes = connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    finally:
        connection.close()
    assert notes == 1
