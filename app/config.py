"""Environment-based configuration and the single-instance lock (B0).

Local mode binds loopback only. A non-loopback listen host is refused at
startup and there is deliberately no widening flag. The instance lock refuses a
second writer rather than starting one.

Fork contract: never fork while holding the instance lock. Ownership travels
with the open file description, so a forked child would share the claim and
could release it on the parent's behalf. Spawn fresh processes instead.
"""

from __future__ import annotations

import errno
import os
import threading
from enum import StrEnum
from pathlib import Path
from typing import Final

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

try:  # Windows only.
    import msvcrt
except ImportError:  # pragma: no cover - POSIX hosts
    msvcrt = None

try:  # POSIX only.
    import fcntl
except ImportError:  # pragma: no cover - Windows hosts
    fcntl = None

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_RUNTIME_DIR: Final[Path] = PROJECT_ROOT / "runtime"
INSTANCE_LOCK_FILENAME: Final[str] = "instance.lock"
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})

# The lock file is one byte of lock cell plus one fixed-width diagnostic record.
# The cell is never written; the record is always overwritten in place, so the
# file only grows and nothing is ever truncated or deleted.
_DIAG_OFFSET: Final[int] = 1
_DIAG_WIDTH: Final[int] = 96

# Same-process registry. It makes in-process claims deterministic without
# relying on platform per-handle behaviour, which is support, not authority.
_HOLDERS: dict[str, int] = {}
_HOLDERS_GUARD = threading.Lock()

# Errnos meaning "another claim holds it". Anything else fails closed.
_CONTENDED_ERRNOS: Final[frozenset[int]] = frozenset(
    {
        errno.EACCES,
        errno.EAGAIN,
        getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
        getattr(errno, "EDEADLK", 36),
        getattr(errno, "EDEADLOCK", 36),
    }
)


class Mode(StrEnum):
    """Run modes. Hosted mode is opt-in only."""

    CI_FAKE = "ci_fake"
    DEMO_FAKE = "demo_fake"
    HOSTED = "hosted"


class ConfigError(RuntimeError):
    """Raised when configuration would leave the local-only boundary."""


class InstanceLockError(RuntimeError):
    """Raised when the single-instance lock is already held."""


class Settings(BaseSettings):
    """Settings read from ``SDC_*`` environment variables and an optional .env."""

    model_config = SettingsConfigDict(
        env_prefix="SDC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    mode: Mode = Mode.CI_FAKE
    listen_host: str = "127.0.0.1"
    listen_port: int = Field(default=8000, ge=1, le=65535)
    writes_enabled: bool = True
    allow_hosted_calls: bool = False
    runtime_dir: Path = DEFAULT_RUNTIME_DIR
    database_path: Path | None = None
    provider_name: str | None = None
    provider_model: str | None = None
    provider_base_url: str | None = None
    provider_timeout_s: float = 30.0
    hosted_api_key: SecretStr | None = None
    run_id: str | None = None
    kb_dir: Path = PROJECT_ROOT / "data" / "kb"
    kb_manifest_path: Path = PROJECT_ROOT / "data" / "fixtures" / "manifest.json"
    retrieval_top_k: int = Field(default=3, ge=1, le=10)
    retrieval_min_score: float = Field(default=0.5, ge=0, allow_inf_nan=False)

    @field_validator("listen_host")
    @classmethod
    def _require_loopback(cls, value: str) -> str:
        host = value.strip().lower()
        if host not in LOOPBACK_HOSTS:
            msg = (
                f"listen host {value!r} refused: local mode binds loopback only "
                f"({', '.join(sorted(LOOPBACK_HOSTS))})"
            )
            raise ValueError(msg)
        return host

    @model_validator(mode="after")
    def _hosted_requires_opt_in(self) -> Settings:
        if self.mode is Mode.HOSTED and not self.allow_hosted_calls:
            msg = "hosted mode requires SDC_ALLOW_HOSTED_CALLS=1"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _database_path_inside_runtime(self) -> Settings:
        """Refuse a database path that resolves outside the runtime directory."""
        if self.database_path is None:
            return self
        runtime_root = self.runtime_dir.resolve()
        candidate = self.database_path.resolve()
        if candidate == runtime_root or not candidate.is_relative_to(runtime_root):
            msg = (
                f"database path {self.database_path!s} refused: it must resolve inside the "
                f"runtime directory {runtime_root}"
            )
            raise ValueError(msg)
        return self

    @property
    def instance_lock_path(self) -> Path:
        """Location of the single-instance lock file."""
        return self.runtime_dir / INSTANCE_LOCK_FILENAME

    @property
    def resolved_database_path(self) -> Path:
        """Database file location: the explicit override, else runtime/servicedesk.db."""
        if self.database_path is not None:
            return self.database_path
        return self.runtime_dir / "servicedesk.db"


def _lock_key(path: Path) -> str:
    """One key per real file: case-folded, symlinks and aliases resolved."""
    return os.path.normcase(str(path.resolve()))


def _try_lock(descriptor: int) -> bool:
    """Attempt a non-blocking exclusive lock; False only on documented contention."""
    os.lseek(descriptor, 0, os.SEEK_SET)
    if msvcrt is not None:
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in _CONTENDED_ERRNOS:
                return False
            msg = f"cannot acquire the instance lock: {exc}"
            raise ConfigError(msg) from exc
        return True
    if fcntl is not None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _CONTENDED_ERRNOS:
                return False
            msg = f"cannot acquire the instance lock: {exc}"
            raise ConfigError(msg) from exc
        return True
    msg = "no OS file lock primitive is available on this platform"
    raise ConfigError(msg)


def _unlock(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if msvcrt is not None:
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return
    msg = "no OS file lock primitive is available on this platform"
    raise ConfigError(msg)


def _close_descriptor(descriptor: int) -> None:
    os.close(descriptor)


def _diag_record(state: str) -> bytes:
    text = f"state={state} pid={os.getpid()}"
    return text.ljust(_DIAG_WIDTH)[:_DIAG_WIDTH].encode("ascii")


def _write_all(descriptor: int, payload: bytes) -> None:
    """Write the whole buffer; os.write may accept fewer bytes than asked."""
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            msg = "short write while updating the instance lock record"
            raise OSError(msg)
        view = view[written:]


def _write_record(descriptor: int, state: str) -> None:
    os.lseek(descriptor, _DIAG_OFFSET, os.SEEK_SET)
    _write_all(descriptor, _diag_record(state))
    os.fsync(descriptor)
    os.lseek(descriptor, 0, os.SEEK_SET)


class InstanceLock:
    """Single-writer guard whose ownership is the OS lock on a held descriptor.

    The file is opened once, a non-blocking exclusive lock is taken on byte 0,
    and the descriptor stays open for the claim's lifetime. File content is a
    fixed-width diagnostic record only: it is never read to decide ownership, so
    a stale record left by a crashed process cannot block a restart. The kernel
    releases the lock when the owning process dies. The file is never truncated
    and never deleted; byte 0 is never written.

    A live holder is always refused. Call ``release()`` when done, and never
    fork while holding the lock.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._handle: int | None = None
        self._held = False

    @property
    def path(self) -> Path:
        """Path of the lock file."""
        return self._path

    @property
    def held(self) -> bool:
        """True while this object holds the lock."""
        return self._held

    def acquire(self) -> None:
        """Take the lock, or raise if another holder has it."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        key = _lock_key(self._path)
        with _HOLDERS_GUARD:
            if self._held:
                msg = f"this object already holds the instance lock at {self._path}"
                raise InstanceLockError(msg)
            if key in _HOLDERS:
                msg = f"instance lock already held at {self._path}: second worker refused"
                raise InstanceLockError(msg)
            descriptor = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                locked = _try_lock(descriptor)
            except BaseException:
                # No lock was taken; close and propagate the original error.
                _close_descriptor(descriptor)
                raise
            if not locked:
                # Refusal path: close here, never through generic cleanup.
                _close_descriptor(descriptor)
                msg = f"instance lock already held at {self._path}: second worker refused"
                raise InstanceLockError(msg)
            try:
                _write_record(descriptor, "held")
            except BaseException:
                # The lock was taken; release it and close, best effort, so the
                # original error is the one that reaches the caller.
                try:
                    _unlock(descriptor)
                except OSError:
                    pass
                try:
                    _close_descriptor(descriptor)
                except OSError:
                    pass
                raise
            _HOLDERS[key] = id(self)
            self._handle = descriptor
            self._held = True

    def release(self) -> None:
        """Release the lock, keeping the file and overwriting only its record."""
        if not self._held or self._handle is None:
            return
        descriptor = self._handle
        key = _lock_key(self._path)
        # A failure here or in the unlock leaves the object held: safe direction.
        _write_record(descriptor, "released")
        _unlock(descriptor)
        try:
            _close_descriptor(descriptor)
        finally:
            self._handle = None
            self._held = False
            with _HOLDERS_GUARD:
                _HOLDERS.pop(key, None)
