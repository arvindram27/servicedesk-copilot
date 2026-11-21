"""Health and readiness endpoints (B3).

Two endpoints, deliberately separate:

- ``/healthz`` is process liveness only. It cannot be 503.
- ``/readyz`` reports whether this process can serve its configured mode from
  LOCAL evidence. It is not upstream health, not production readiness, and not
  restore evidence.

The database probe is a rollback-only write probe. Its bounded claim, and
nothing more, is: the database file opened read-write, the write lock was
acquired, and the engine accepted and reversed a modification. No durability,
flush or physical-write claim is made anywhere, and the probe is not described
as detecting full-disk, read-only-media or any other specific failure mode. The
probe touches no table, no row, no schema object and never ``audit_events``;
it changes no logical database state. SQLite may create, rewrite or remove its
own ``-wal``/``-shm``/``-journal`` sidecar files while the database is open:
that is engine lifecycle, and nothing here asserts about those files.

Provider readiness is fail-closed and honest: fake modes need no provider;
``hosted`` reports ``unavailable: provider_not_selected`` and fails readiness
because no provider has been selected. The provider gate (B5) owns defining the
key source and replacing this check.

The provider status cache is evidence only and never gates readiness. Its
``status`` describes the evidence (fresh, parseable, in-window) while
``last_result`` carries the recorded call outcome, so a fresh cache recording a
failed call reads ``status: verified, last_result: error``.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import Mode, Settings

router = APIRouter()

DB_BUSY_TIMEOUT_MS: Final[int] = 5000
PROVIDER_STATUS_MAX_BYTES: Final[int] = 64 * 1024
PROVIDER_STATUS_MAX_AGE_S: Final[int] = 900
PROVIDER_STATUS_MAX_FUTURE_SKEW_S: Final[int] = 60

_UNVERIFIED: Final[dict[str, Any]] = {
    "status": "unverified",
    "last_result": None,
    "checked_at": None,
    "age_s": None,
}
_INVALID: Final[dict[str, Any]] = {
    "status": "invalid",
    "last_result": None,
    "checked_at": None,
    "age_s": None,
}


@router.get("/healthz")
def healthz(request: Request) -> dict[str, str]:
    """Process liveness only: no dependencies are consulted."""
    version = getattr(request.app, "version", "unknown")
    return {"status": "alive", "version": version}


def _is_busy_error(exc: sqlite3.Error) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _probe_database(database_path: Path) -> tuple[str, str]:
    """Run the rollback-only write probe; return (status, detail)."""
    if not database_path.exists():
        return "not_initialized", "not_initialized"

    try:
        connection = sqlite3.connect(database_path.resolve().as_uri() + "?mode=rw", uri=True)
    except sqlite3.Error:
        return "write_probe_failed", "write_probe_failed"

    try:
        connection.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            if _is_busy_error(exc):
                return "write_probe_failed", "write_probe_locked"
            return "write_probe_failed", "write_probe_failed"
        try:
            try:
                current = int(connection.execute("PRAGMA user_version").fetchone()[0])
                connection.execute(f"PRAGMA user_version={0 if current != 0 else 1}")
            except sqlite3.Error:
                return "write_probe_failed", "write_probe_failed"
            return "ok", "write_probe_rolled_back"
        finally:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
    finally:
        connection.close()


def _parse_checked_at(value: object) -> datetime | None:
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _read_provider_status(runtime_dir: Path) -> dict[str, Any]:
    """Read the provider status cache; report evidence, never gate readiness."""
    path = runtime_dir / "provider_status.json"
    try:
        with path.open("rb") as handle:
            raw = handle.read(PROVIDER_STATUS_MAX_BYTES + 1)
    except FileNotFoundError:
        return dict(_UNVERIFIED)
    except OSError:
        return dict(_INVALID)

    if len(raw) > PROVIDER_STATUS_MAX_BYTES:
        return dict(_INVALID)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return dict(_INVALID)
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        return dict(_INVALID)
    if type(payload) is not dict:
        return dict(_INVALID)

    version = payload.get("format")
    if type(version) is not int or version != 1:
        return dict(_INVALID)

    checked_at = _parse_checked_at(payload.get("checked_at"))
    if checked_at is None:
        return dict(_INVALID)

    result = payload.get("result")
    if type(result) is not str or result not in {"ok", "error"}:
        return dict(_INVALID)

    latency = payload.get("latency_ms")
    if latency is not None:
        if type(latency) not in {int, float}:
            return dict(_INVALID)
        try:
            latency_value = float(latency)
        except OverflowError:
            return dict(_INVALID)
        if not math.isfinite(latency_value):
            return dict(_INVALID)

    now = datetime.now(UTC)
    age_seconds = (now - checked_at).total_seconds()
    if not math.isfinite(age_seconds):
        return dict(_INVALID)
    if age_seconds < -PROVIDER_STATUS_MAX_FUTURE_SKEW_S:
        return dict(_INVALID)
    age = 0 if age_seconds < 0 else int(age_seconds)

    status = "stale" if age > PROVIDER_STATUS_MAX_AGE_S else "verified"
    try:
        checked_at_text = checked_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, ValueError):
        return dict(_INVALID)
    return {
        "status": status,
        "last_result": result,
        "checked_at": checked_at_text,
        "age_s": age,
    }


def _readiness(settings: Settings) -> tuple[dict[str, Any], list[str]]:
    checks: dict[str, Any] = {}
    failed: list[str] = []

    db_status, db_detail = _probe_database(settings.resolved_database_path)
    checks["db"] = {"status": db_status, "detail": db_detail}
    if db_status != "ok":
        failed.append("db")

    checks["vector_store"] = {"status": "not_configured"}

    if settings.mode is Mode.HOSTED:
        checks["provider"] = {"status": "unavailable", "detail": "provider_not_selected"}
        failed.append("provider")
    else:
        checks["provider"] = {"status": "not_required"}

    checks["provider_upstream"] = _read_provider_status(settings.runtime_dir)
    return checks, failed


@router.get("/readyz")
def readyz(request: Request) -> JSONResponse:
    """Local readiness for the configured mode; 503 when a gating check fails."""
    settings: Settings = request.app.state.settings
    checks, failed = _readiness(settings)
    body: dict[str, Any] = {
        "status": "ready" if not failed else "not_ready",
        "mode": settings.mode.value,
        "checks": checks,
    }
    if failed:
        body["failed"] = failed
    return JSONResponse(status_code=200 if not failed else 503, content=body)
