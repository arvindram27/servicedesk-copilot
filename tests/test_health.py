"""Health and readiness behaviour (B3)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from app.config import Mode, Settings
from app.main import create_app
from fastapi.testclient import TestClient
from pydantic import ValidationError

from db_support import run_upgrade

SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
DATABASE_NAME = "servicedesk.db"


def _settings(tmp_path: Path, mode: Mode) -> Settings:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return Settings(
        mode=mode,
        runtime_dir=runtime,
        allow_hosted_calls=(mode is Mode.HOSTED),
    )


def _client(tmp_path: Path, mode: Mode = Mode.CI_FAKE) -> tuple[TestClient, Path]:
    settings = _settings(tmp_path, mode)
    return TestClient(create_app(settings)), settings.runtime_dir


def _migrated_client(tmp_path: Path, mode: Mode = Mode.CI_FAKE) -> tuple[TestClient, Path]:
    client, runtime = _client(tmp_path, mode)
    run_upgrade(runtime / DATABASE_NAME)
    return client, runtime


def _status_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "format": 1,
        "checked_at": datetime.now(UTC).isoformat(),
        "result": "ok",
        "latency_ms": 5,
    }
    payload.update(overrides)
    return payload


def _write_status(runtime: Path, payload: object) -> None:
    (runtime / "provider_status.json").write_text(json.dumps(payload), encoding="utf-8")


def _upstream(response: object) -> dict[str, object]:
    return response.json()["checks"]["provider_upstream"]  # type: ignore[attr-defined]


def test_healthz_reports_alive(tmp_path: Path) -> None:
    client, runtime = _client(tmp_path)

    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "alive"
    assert body["version"]
    assert not (runtime / DATABASE_NAME).exists()


def test_readyz_green_with_migrated_database(tmp_path: Path) -> None:
    client, _runtime = _migrated_client(tmp_path)

    response = client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["db"] == {
        "status": "ok",
        "detail": "write_probe_rolled_back",
    }
    assert body["checks"]["vector_store"] == {"status": "not_configured"}
    assert body["checks"]["provider"] == {"status": "not_required"}
    assert body["checks"]["provider_upstream"]["status"] == "unverified"


def test_readyz_db_not_initialized_creates_nothing(tmp_path: Path) -> None:
    client, runtime = _client(tmp_path)

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["db"]["status"] == "not_initialized"
    assert not (runtime / DATABASE_NAME).exists()


@pytest.mark.parametrize("preset", [2147483647, -5, 0])
def test_readyz_probe_leaves_zero_state(tmp_path: Path, preset: int) -> None:
    client, runtime = _migrated_client(tmp_path)
    database = runtime / DATABASE_NAME

    connection = sqlite3.connect(database)
    try:
        connection.execute(f"PRAGMA user_version={preset}")
        connection.commit()
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "users",
                "categories",
                "confirmation_tokens",
                "tickets",
                "idempotency_keys",
                "audit_events",
            )
        }
        tables_before = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()

    for _ in range(3):
        assert client.get("/readyz").status_code == 200

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == preset
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
        tables_after = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()

    assert after == before
    assert tables_after == tables_before


def test_readyz_db_locked_reports_write_probe_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.health as health_module

    client, runtime = _migrated_client(tmp_path)
    monkeypatch.setattr(health_module, "DB_BUSY_TIMEOUT_MS", 50)
    database = runtime / DATABASE_NAME

    blocker = sqlite3.connect(database, isolation_level=None, timeout=0.1)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        response = client.get("/readyz")
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert response.status_code == 503
    assert response.json()["checks"]["db"]["detail"] == "write_probe_locked"


def test_readyz_hosted_reports_provider_unavailable(tmp_path: Path) -> None:
    client, _runtime = _migrated_client(tmp_path, Mode.HOSTED)

    response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["provider"] == {
        "status": "unavailable",
        "detail": "provider_not_selected",
    }
    assert "provider" in body["failed"]


@pytest.mark.parametrize("mode", [Mode.CI_FAKE, Mode.DEMO_FAKE])
def test_readyz_fake_modes_need_no_provider(tmp_path: Path, mode: Mode) -> None:
    client, _runtime = _migrated_client(tmp_path, mode)

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["checks"]["provider"] == {"status": "not_required"}


def test_provider_status_absent_is_unverified_and_does_not_gate(tmp_path: Path) -> None:
    client, _runtime = _migrated_client(tmp_path)

    response = client.get("/readyz")

    assert response.status_code == 200
    assert _upstream(response) == {
        "status": "unverified",
        "last_result": None,
        "checked_at": None,
        "age_s": None,
    }


def test_provider_status_stale_reported_does_not_gate(tmp_path: Path) -> None:
    client, runtime = _migrated_client(tmp_path)
    stale = (datetime.now(UTC) - timedelta(seconds=1000)).isoformat()
    _write_status(runtime, _status_payload(checked_at=stale))

    response = client.get("/readyz")

    assert response.status_code == 200
    upstream = _upstream(response)
    assert upstream["status"] == "stale"
    assert int(upstream["age_s"]) >= 1000


def test_provider_status_invalid_json_does_not_crash(tmp_path: Path) -> None:
    client, runtime = _migrated_client(tmp_path)
    (runtime / "provider_status.json").write_text("not json", encoding="utf-8")

    response = client.get("/readyz")

    assert response.status_code == 200
    assert _upstream(response)["status"] == "invalid"


def test_provider_status_oversize_is_invalid(tmp_path: Path) -> None:
    client, runtime = _migrated_client(tmp_path)
    (runtime / "provider_status.json").write_bytes(b"x" * (64 * 1024 + 1))

    response = client.get("/readyz")

    assert response.status_code == 200
    assert _upstream(response)["status"] == "invalid"


def test_provider_status_future_timestamp_is_invalid(tmp_path: Path) -> None:
    client, runtime = _migrated_client(tmp_path)
    future = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()
    _write_status(runtime, _status_payload(checked_at=future))

    assert client.get("/readyz").status_code == 200
    assert _upstream(client.get("/readyz"))["status"] == "invalid"

    near = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
    _write_status(runtime, _status_payload(checked_at=near))
    upstream = _upstream(client.get("/readyz"))
    assert upstream["status"] == "verified"
    assert upstream["age_s"] == 0


def test_provider_status_bool_fields_rejected(tmp_path: Path) -> None:
    client, runtime = _migrated_client(tmp_path)

    _write_status(runtime, _status_payload(format=True))
    assert _upstream(client.get("/readyz"))["status"] == "invalid"

    _write_status(runtime, _status_payload(latency_ms=True))
    assert _upstream(client.get("/readyz"))["status"] == "invalid"


def test_provider_status_error_result_reported(tmp_path: Path) -> None:
    client, runtime = _migrated_client(tmp_path)
    _write_status(runtime, _status_payload(result="error"))

    upstream = _upstream(client.get("/readyz"))

    assert upstream["status"] == "verified"
    assert upstream["last_result"] == "error"


def test_provider_status_canonical_echo(tmp_path: Path) -> None:
    client, runtime = _migrated_client(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    offset_time = now.astimezone(timezone(timedelta(hours=5, minutes=30)))
    raw = offset_time.isoformat()
    _write_status(runtime, _status_payload(checked_at=raw))

    upstream = _upstream(client.get("/readyz"))

    assert upstream["checked_at"] == now.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert str(upstream["checked_at"]).endswith("Z")
    assert raw not in json.dumps(upstream)


def test_provider_status_unhashable_result_is_invalid(tmp_path: Path) -> None:
    client, runtime = _migrated_client(tmp_path)

    _write_status(runtime, _status_payload(result=["ok"]))
    response = client.get("/readyz")
    assert response.status_code == 200
    assert _upstream(response)["status"] == "invalid"

    _write_status(runtime, _status_payload(result={"value": "ok"}))
    response = client.get("/readyz")
    assert response.status_code == 200
    assert _upstream(response)["status"] == "invalid"


def test_provider_status_huge_integer_latency_is_invalid(tmp_path: Path) -> None:
    client, runtime = _migrated_client(tmp_path)
    _write_status(runtime, _status_payload(latency_ms=10**400))

    response = client.get("/readyz")

    assert response.status_code == 200
    assert _upstream(response)["status"] == "invalid"


def test_provider_status_extreme_offset_timestamp_is_invalid(tmp_path: Path) -> None:
    client, runtime = _migrated_client(tmp_path)
    _write_status(runtime, _status_payload(checked_at="0001-01-01T00:00:00+23:59"))

    response = client.get("/readyz")

    assert response.status_code == 200
    assert _upstream(response)["status"] == "invalid"


def test_long_readiness_poll_makes_zero_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _runtime = _migrated_client(tmp_path)
    attempts: list[str] = []

    def _boom(request: object) -> object:
        attempts.append("transport")
        raise AssertionError("readiness attempted an outbound transport call")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _boom)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _boom)

    for _ in range(100):
        assert client.get("/readyz").status_code == 200

    assert attempts == []


def test_readiness_does_not_touch_the_instance_lock(tmp_path: Path) -> None:
    client, runtime = _migrated_client(tmp_path)

    for _ in range(5):
        client.get("/readyz")

    assert not (runtime / "instance.lock").exists()


def test_health_poll_creates_no_non_sidecar_files(tmp_path: Path) -> None:
    """Engine-owned sidecars are excluded; the database's logical state is
    asserted in ``test_readyz_probe_leaves_zero_state`` instead of by bytes."""
    client, runtime = _migrated_client(tmp_path)

    def names() -> set[str]:
        return {path.name for path in runtime.iterdir() if path.is_file()}

    def contents() -> dict[str, bytes]:
        return {
            path.name: path.read_bytes()
            for path in runtime.iterdir()
            if path.is_file()
            and not path.name.endswith(SIDECAR_SUFFIXES)
            and path.name != DATABASE_NAME
        }

    before_names, before_contents = names(), contents()
    for _ in range(5):
        client.get("/healthz")
        client.get("/readyz")
    after_names, after_contents = names(), contents()

    sidecars = {name for name in after_names if name.endswith(SIDECAR_SUFFIXES)}
    assert after_names - before_names <= sidecars
    assert after_contents == before_contents


def test_factory_registers_routes(tmp_path: Path) -> None:
    """Serving the endpoints over HTTP is the stable contract.

    The installed FastAPI/Starlette may represent included routers as lazy
    markers in ``app.routes``, so flat route-list introspection is not a stable
    contract. Exercising the endpoints over HTTP is.
    """
    client, _runtime = _migrated_client(tmp_path)

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "alive"

    ready = client.get("/readyz")
    assert ready.status_code == 200
    ready_body = ready.json()
    assert ready_body["status"] == "ready"
    assert ready_body["checks"]["db"]["status"] == "ok"
    assert ready_body["checks"]["provider"]["status"] == "not_required"

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert metrics.headers["content-type"].startswith("text/plain")


def test_loopback_allowlist_still_refuses_widening(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        create_app(Settings(listen_host="0.0.0.0"))
