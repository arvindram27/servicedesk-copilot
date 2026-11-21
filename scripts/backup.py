"""Online SQLite backup command (B1b).

Backups use the SQLite online backup API only: no file copy, no VACUUM INTO.
The deadline is cooperative, not a wall-clock guarantee; see ``create_backup``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import stat
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from app.config import Settings
from pydantic import ValidationError

FORMAT_VERSION = "1"
BACKUP_DIRNAME = "backups"
PAGES_PER_CHUNK = 64
BACKUP_SLEEP_S = 0.25
DEFAULT_TIMEOUT_S = 60.0
SOURCE_BUSY_TIMEOUT_MS = 5000
DIGEST_CHUNK_BYTES = 1024 * 1024


class BackupError(RuntimeError):
    """A backup failed or could not be verified."""


class BackupRefused(BackupError):
    """A guard refused the request before any artifact was created."""


class BackupDeadlineExceeded(BackupError):
    """The cooperative backup deadline expired."""


@dataclass(frozen=True)
class BackupResult:
    """Evidence for one verified backup."""

    backup_path: Path
    manifest_path: Path
    sha256: str
    source_size_bytes: int
    backup_size_bytes: int
    schema_version: str | None
    integrity_check: str
    journal_mode: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


@contextmanager
def _closing(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Close a connection without masking an in-flight failure.

    A close failure is reported only when nothing else is propagating; when a
    primary error is in flight it survives and the close error cannot replace
    it. No file is ever removed.
    """
    try:
        yield connection
    finally:
        primary_in_flight = sys.exc_info()[0] is not None
        try:
            connection.close()
        except (OSError, sqlite3.Error) as exc:
            if not primary_in_flight:
                msg = f"cleanup stage: cannot close the SQLite connection: {exc}"
                raise BackupError(msg) from exc


def validate_timeout(timeout_s: float) -> None:
    """Refuse non-numeric, non-finite or negative cooperative timeouts."""
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        msg = f"timeout must be a number, got {type(timeout_s).__name__}"
        raise BackupRefused(msg)
    if not math.isfinite(float(timeout_s)):
        msg = f"timeout must be finite, got {timeout_s!r}"
        raise BackupRefused(msg)
    if float(timeout_s) < 0:
        msg = f"timeout must not be negative, got {timeout_s!r}"
        raise BackupRefused(msg)


def classify_source(source_path: Path) -> bool:
    """True for an existing regular file, False if missing; refuse anything else.

    A directory, device or other non-regular path refuses, and a stat error is
    reported rather than being treated as a missing first-run source.
    """
    try:
        info = source_path.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        msg = f"cannot stat {source_path}: {exc}"
        raise BackupError(msg) from exc
    if not stat.S_ISREG(info.st_mode):
        msg = f"source {source_path} is not a regular file"
        raise BackupRefused(msg)
    return True


def source_uri(source_path: Path) -> str:
    """Read-write SQLite URI that never creates a missing file."""
    return source_path.resolve().as_uri() + "?mode=rw"


def read_only_uri(path: Path) -> str:
    """Read-only SQLite URI for verification."""
    return path.resolve().as_uri() + "?mode=ro"


def read_schema_revision(connection: sqlite3.Connection) -> str | None:
    """Return the alembic revision, or None for base.

    Genuinely absent or empty ``alembic_version`` means base. Malformed or
    unreadable metadata raises instead of being reported as base.
    """
    table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
    ).fetchone()
    if table is None:
        return None
    try:
        rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    except sqlite3.Error as exc:
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


def _reserve_exclusive(path: Path) -> None:
    """Create ``path`` exclusively; never overwrite an existing file."""
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        msg = f"refusing to overwrite existing file: {path}"
        raise BackupRefused(msg) from None
    except OSError as exc:
        msg = f"cannot create {path}: {exc}"
        raise BackupError(msg) from exc
    os.close(handle)


def _set_delete_journal_mode(connection: sqlite3.Connection) -> str:
    """Switch the artifact to DELETE journal mode and return the actual mode."""
    connection.commit()
    row = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
    return "" if row is None else str(row[0])


def _read_artifact(path: Path) -> tuple[str, str | None, str]:
    """Verify a finalized artifact read-only: integrity, revision, journal mode."""
    connection = sqlite3.connect(read_only_uri(path), uri=True)
    try:
        integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
        integrity = "" if integrity_row is None else str(integrity_row[0])
        revision: str | None = None
        if integrity == "ok":
            revision = read_schema_revision(connection)
        mode_row = connection.execute("PRAGMA journal_mode").fetchone()
        journal_mode = "" if mode_row is None else str(mode_row[0])
    finally:
        connection.close()
    return integrity, revision, journal_mode


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(DIGEST_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest_exclusive(path: Path, manifest: dict[str, Any]) -> None:
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        msg = f"refusing to overwrite existing manifest: {path}"
        raise BackupRefused(msg) from None
    except OSError as exc:
        msg = f"cannot write manifest {path}: {exc}"
        raise BackupError(msg) from exc


def _artifact_path(backup_root: Path, source_path: Path) -> Path:
    stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    return backup_root / f"{source_path.stem}-{stamp}-{uuid.uuid4().hex[:8]}.db"


def _deadline_hook(clock: Callable[[], float], deadline: float) -> Callable[..., None]:
    """Progress callback that aborts the copy by raising when the deadline passes.

    The callback's return value is ignored by CPython, so expiry is an
    exception, never a status return.
    """

    def check(_status: int, _remaining: int, _total: int) -> None:
        if clock() >= deadline:
            msg = "backup deadline exceeded while copying pages"
            raise BackupDeadlineExceeded(msg)

    return check


def create_backup(
    source_path: Path,
    backup_root: Path,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    destination: Path | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> BackupResult:
    """Back up an existing database to a finalized, verified artifact."""
    validate_timeout(timeout_s)
    source_path = Path(source_path)
    backup_root = Path(backup_root)

    if not classify_source(source_path):
        msg = f"source database does not exist: {source_path}"
        raise BackupRefused(msg)

    deadline = clock() + float(timeout_s)
    if clock() >= deadline:
        msg = f"backup deadline expired before copying (timeout {timeout_s})"
        raise BackupDeadlineExceeded(msg)

    try:
        backup_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        msg = f"directory stage: cannot create {backup_root}: {exc}"
        raise BackupError(msg) from exc

    artifact = (
        Path(destination) if destination is not None else _artifact_path(backup_root, source_path)
    )
    if artifact.exists():
        msg = f"refusing to overwrite existing artifact: {artifact}"
        raise BackupRefused(msg)
    _reserve_exclusive(artifact)

    try:
        source_size = source_path.stat().st_size
    except OSError as exc:
        msg = f"source stat stage: {source_path}: {exc}"
        raise BackupError(msg) from exc

    try:
        source_connection = sqlite3.connect(source_uri(source_path), uri=True)
    except (OSError, sqlite3.Error) as exc:
        msg = f"source connect stage: {source_path}: {exc}"
        raise BackupError(msg) from exc

    with _closing(source_connection):
        try:
            source_connection.execute(f"PRAGMA busy_timeout={SOURCE_BUSY_TIMEOUT_MS}")
        except (OSError, sqlite3.Error) as exc:
            msg = f"source read stage: {source_path}: {exc}"
            raise BackupError(msg) from exc
        try:
            destination_connection = sqlite3.connect(str(artifact))
        except (OSError, sqlite3.Error) as exc:
            msg = f"destination stage: {artifact}: {exc}"
            raise BackupError(msg) from exc
        with _closing(destination_connection):
            try:
                source_connection.backup(
                    destination_connection,
                    pages=PAGES_PER_CHUNK,
                    progress=_deadline_hook(clock, deadline),
                    sleep=BACKUP_SLEEP_S,
                )
            except BackupDeadlineExceeded:
                raise
            except (OSError, sqlite3.Error) as exc:
                msg = f"copy stage: {artifact}: {exc}"
                raise BackupError(msg) from exc
            try:
                mode = _set_delete_journal_mode(destination_connection)
            except (OSError, sqlite3.Error) as exc:
                msg = f"normalization stage: {artifact}: {exc}"
                raise BackupError(msg) from exc
            if mode.lower() != "delete":
                msg = f"artifact journal mode is {mode!r}, expected 'delete'"
                raise BackupError(msg)

    try:
        integrity, schema_version, journal_mode = _read_artifact(artifact)
    except BackupError:
        raise
    except (OSError, sqlite3.Error) as exc:
        msg = f"artifact verification stage: {artifact}: {exc}"
        raise BackupError(msg) from exc
    if integrity != "ok":
        msg = f"integrity_check on artifact failed: {integrity or 'no result'}"
        raise BackupError(msg)
    try:
        backup_size = artifact.stat().st_size
        digest = _sha256(artifact)
    except OSError as exc:
        msg = f"digest stage: {artifact}: {exc}"
        raise BackupError(msg) from exc

    manifest_path = artifact.with_name(artifact.name + ".manifest.json")
    manifest: dict[str, Any] = {
        "format": FORMAT_VERSION,
        "source_path": str(source_path),
        "backup_path": str(artifact),
        "created_at": _utc_now().isoformat(),
        "source_size_bytes": source_size,
        "backup_size_bytes": backup_size,
        "sha256": digest,
        "integrity_check": integrity,
        "schema_version": schema_version,
        "journal_mode": journal_mode,
        "timeout_seconds": float(timeout_s),
    }
    _write_manifest_exclusive(manifest_path, manifest)

    return BackupResult(
        backup_path=artifact,
        manifest_path=manifest_path,
        sha256=digest,
        source_size_bytes=source_size,
        backup_size_bytes=backup_size,
        schema_version=schema_version,
        integrity_check=integrity,
        journal_mode=journal_mode,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.backup",
        description="Create a verified online backup of the ServiceDesk Copilot database.",
    )
    parser.add_argument("--database", help="database path; must resolve inside the runtime dir")
    parser.add_argument("--runtime-dir", help="runtime directory; backups go to <runtime>/backups")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help="cooperative copy deadline in seconds (default 60)",
    )
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    return parser


def main(argv: list[str] | None = None, stream: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = stream if stream is not None else sys.stdout
    overrides: dict[str, Any] = {}
    if args.database:
        overrides["database_path"] = Path(args.database)
    if args.runtime_dir:
        overrides["runtime_dir"] = Path(args.runtime_dir)
    try:
        settings = Settings(**overrides)
    except ValidationError as exc:
        print(f"refused: {exc}", file=out)
        return 2
    source = settings.resolved_database_path
    root = settings.runtime_dir / BACKUP_DIRNAME
    try:
        result = create_backup(source, root, timeout_s=args.timeout_seconds)
    except BackupRefused as exc:
        print(f"refused: {exc}", file=out)
        return 2
    except BackupError as exc:
        print(f"backup failed: {exc}", file=out)
        return 1
    except (OSError, sqlite3.Error) as exc:
        print(f"backup failed: operational error: {exc}", file=out)
        return 1
    if args.json:
        print(json.dumps({key: str(value) for key, value in asdict(result).items()}), file=out)
    else:
        print(f"artifact: {result.backup_path}", file=out)
        print(f"manifest: {result.manifest_path}", file=out)
        print(f"sha256: {result.sha256}", file=out)
        print(f"schema_version: {result.schema_version}", file=out)
        print(f"integrity_check: {result.integrity_check}", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
