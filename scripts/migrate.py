"""Manual migration wrapper (B1b): lock, writability, backup, upgrade, audit.

This wrapper protects cooperating manual invocations on one local filesystem.
It does not stop arbitrary SQLite writers, direct Alembic runs or a future
application that does not honour it, and it is never called at app startup.
"""

from __future__ import annotations

import argparse
import errno
import io
import json
import os
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from alembic import command
from alembic.config import Config
from app.config import PROJECT_ROOT, Settings
from app.db.connection import create_engine_for
from pydantic import ValidationError
from scripts.backup import (
    BACKUP_DIRNAME,
    DEFAULT_TIMEOUT_S,
    BackupError,
    BackupRefused,
    classify_source,
    create_backup,
    validate_timeout,
)
from sqlalchemy import Connection
from sqlalchemy.exc import DatabaseError, SQLAlchemyError

try:  # Windows only.
    import msvcrt
except ImportError:  # pragma: no cover - POSIX hosts
    msvcrt = None

try:  # POSIX only.
    import fcntl
except ImportError:  # pragma: no cover - Windows hosts
    fcntl = None

LOCK_SUFFIX = ".migrate.lock"
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
ALEMBIC_DIR = PROJECT_ROOT / "alembic"

_CONTENDED_ERRNOS = frozenset(
    {
        errno.EACCES,
        errno.EAGAIN,
        getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
        getattr(errno, "EDEADLK", 36),
        getattr(errno, "EDEADLOCK", 36),
    }
)


class MigrationLockError(RuntimeError):
    """The cooperative migration lock could not be used."""


class MigrationLockBusy(MigrationLockError):
    """Another cooperating wrapper holds the lock."""


class MigrationLockUnsupported(MigrationLockError):
    """No OS file lock primitive exists on this platform."""


def lock_path_for(database_path: Path) -> Path:
    """Deterministic lock path beside the database, independent of its existence."""
    return database_path.with_name(database_path.name + LOCK_SUFFIX)


def _try_lock(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if msvcrt is not None:
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in _CONTENDED_ERRNOS:
                msg = f"migration lock is already held: {exc}"
                raise MigrationLockBusy(msg) from exc
            msg = f"cannot acquire the migration lock: {exc}"
            raise MigrationLockError(msg) from exc
        return
    if fcntl is not None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _CONTENDED_ERRNOS:
                msg = f"migration lock is already held: {exc}"
                raise MigrationLockBusy(msg) from exc
            msg = f"cannot acquire the migration lock: {exc}"
            raise MigrationLockError(msg) from exc
        return
    msg = "no OS file lock primitive is available on this platform"
    raise MigrationLockUnsupported(msg)


def _unlock(descriptor: int) -> None:
    if msvcrt is not None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


class MigrationLock:
    """Persistent lock file with a non-blocking OS lock on its descriptor."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._handle: int | None = None

    @property
    def held(self) -> bool:
        """True while this object holds the OS lock."""
        return self._handle is not None

    def acquire(self) -> None:
        """Take the lock or raise; the file is created if absent and never truncated."""
        try:
            descriptor = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            msg = f"cannot open the migration lock file {self._path}: {exc}"
            raise MigrationLockError(msg) from exc
        try:
            _try_lock(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        self._handle = descriptor

    def release(self) -> None:
        """Release the OS lock and close the descriptor; the file is kept."""
        if self._handle is None:
            return
        descriptor = self._handle
        self._handle = None
        try:
            _unlock(descriptor)
        finally:
            os.close(descriptor)


def _alembic_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    return config


def _read_revision(connection: Connection) -> str | None:
    """Alembic revision of a live connection; None means base."""
    table = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
    ).fetchone()
    if table is None:
        return None
    try:
        rows = connection.exec_driver_sql("SELECT version_num FROM alembic_version").fetchall()
    except DatabaseError as exc:
        msg = f"malformed alembic_version metadata: {exc}"
        raise BackupError(msg) from exc
    if not rows:
        return None
    values = {row[0] for row in rows}
    if len(values) != 1:
        msg = f"multiple alembic heads in a linear schema: {sorted(map(str, values))}"
        raise BackupError(msg)
    value = values.pop()
    if not isinstance(value, str) or not value.strip():
        msg = f"invalid alembic revision value: {value!r}"
        raise BackupError(msg)
    return value


def _insert_audit_event(
    connection: Connection,
    *,
    from_revision: str | None,
    to_revision: str | None,
    backup_path: Path | None,
    skipped_reason: str | None,
) -> None:
    payload = {
        "from_revision": from_revision,
        "to_revision": to_revision,
        "backup_path": None if backup_path is None else str(backup_path),
        "skipped_reason": skipped_reason,
        "schema_no_change": from_revision == to_revision,
    }
    connection.exec_driver_sql(
        "INSERT INTO audit_events (event_id, occurred_at, event_type, node, outcome, payload_json)"
        " VALUES (?, ?, 'migration.applied', 'migrate', 'ok', ?)",
        (
            str(uuid.uuid4()),
            datetime.now(UTC).isoformat(),
            json.dumps(payload, sort_keys=True),
        ),
    )


def run_wrapper(
    database_path: Path,
    *,
    runtime_dir: Path | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    stream: TextIO | None = None,
) -> int:
    """Run the guarded migration sequence; return 0 success, 1 failure, 2 refusal."""
    out = stream if stream is not None else sys.stdout
    database_path = Path(database_path)
    try:
        validate_timeout(timeout_s)
    except BackupRefused as exc:
        print(f"refused: {exc}", file=out)
        return 2
    parent = database_path.parent
    if parent.exists() and not parent.is_dir():
        print(f"refused: parent path is not a directory: {parent}", file=out)
        return 2
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"refused: cannot create directory {parent}: {exc}", file=out)
        return 2

    lock = MigrationLock(lock_path_for(database_path))
    try:
        lock.acquire()
    except MigrationLockUnsupported as exc:
        print(f"refused: {exc}", file=out)
        return 2
    except MigrationLockBusy as exc:
        print(f"refused: {exc}", file=out)
        return 2
    except MigrationLockError as exc:
        print(f"migration failed: {exc}", file=out)
        return 1

    engine = None
    connection = None
    try:
        try:
            exists = classify_source(database_path)
        except BackupRefused as exc:
            print(f"refused: {exc}", file=out)
            return 2
        except BackupError as exc:
            print(f"migration failed: {exc}", file=out)
            return 1

        backup_path: Path | None = None
        backup_revision: str | None = None
        skipped_reason: str | None = None

        if exists:
            try:
                probe = os.open(database_path, os.O_RDWR)
            except OSError as exc:
                print(f"refused: cannot open {database_path} for writing: {exc}", file=out)
                return 2
            os.close(probe)
            root = (
                Path(runtime_dir) if runtime_dir is not None else database_path.parent
            ) / BACKUP_DIRNAME
            try:
                result = create_backup(database_path, root, timeout_s=timeout_s)
            except BackupError as exc:
                print(f"migration aborted: backup failed or unverified: {exc}", file=out)
                return 1
            backup_path = result.backup_path
            backup_revision = result.schema_version
        else:
            skipped_reason = "source_missing_first_run"

        try:
            engine = create_engine_for(database_path)
            connection = engine.connect()
        except (OSError, sqlite3.Error, SQLAlchemyError) as exc:
            print(f"migration failed: database setup stage: {exc}", file=out)
            return 1

        try:
            from_revision = _read_revision(connection)
            connection.rollback()
        except (OSError, sqlite3.Error, SQLAlchemyError, BackupError) as exc:
            print(f"migration failed: revision read stage: {exc}", file=out)
            return 1

        if exists and from_revision != backup_revision:
            msg = (
                "migration aborted: revision mismatch "
                f"(database {from_revision!r}, verified backup {backup_revision!r})"
            )
            print(msg, file=out)
            return 1

        config = _alembic_config()
        config.attributes["connection"] = connection
        try:
            command.upgrade(config, "head")
        except Exception as exc:  # noqa: BLE001 - the error text is the report
            try:
                connection.rollback()
            except Exception:  # noqa: BLE001 - best effort before reporting
                pass
            print(f"migration failed: upgrade did not complete: {exc}", file=out)
            return 1

        try:
            connection.commit()
        except Exception as exc:  # noqa: BLE001 - committed state is unknown here
            print(f"migration failed: commit after upgrade failed: {exc}", file=out)
            return 1

        try:
            with connection.begin():
                to_revision = _read_revision(connection)
                _insert_audit_event(
                    connection,
                    from_revision=from_revision,
                    to_revision=to_revision,
                    backup_path=backup_path,
                    skipped_reason=skipped_reason,
                )
        except Exception as exc:  # noqa: BLE001 - upgrade already applied
            print(f"upgrade applied, audit event failed: {exc}", file=out)
            return 1

        print(f"source: {database_path}", file=out)
        if backup_path is None:
            print(f"backup: skipped ({skipped_reason})", file=out)
        else:
            print(f"backup: {backup_path}", file=out)
        print(f"revision: {from_revision} -> {to_revision}", file=out)
        return 0
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - teardown must not mask the result
                pass
        if engine is not None:
            try:
                engine.dispose()
            except Exception:  # noqa: BLE001 - teardown must not mask the result
                pass
        lock.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.migrate",
        description="Run the guarded manual schema migration for ServiceDesk Copilot.",
    )
    parser.add_argument("--database", help="database path; must resolve inside the runtime dir")
    parser.add_argument("--runtime-dir", help="runtime directory; backups go to <runtime>/backups")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help="cooperative backup deadline in seconds (default 60)",
    )
    parser.add_argument("--json", action="store_true", help="print a JSON summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    overrides: dict[str, Any] = {}
    if args.database:
        overrides["database_path"] = Path(args.database)
    if args.runtime_dir:
        overrides["runtime_dir"] = Path(args.runtime_dir)
    try:
        settings = Settings(**overrides)
    except ValidationError as exc:
        print(f"refused: {exc}")
        return 2

    capture = io.StringIO()
    code = run_wrapper(
        settings.resolved_database_path,
        runtime_dir=settings.runtime_dir,
        timeout_s=args.timeout_seconds,
        stream=capture,
    )
    lines = [line for line in capture.getvalue().splitlines() if line]
    if args.json:
        print(json.dumps({"exit_code": code, "lines": lines}))
    else:
        for line in lines:
            print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
