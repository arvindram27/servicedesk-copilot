"""Base metrics behaviour (B3)."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from app.config import Mode, Settings
from app.main import create_app
from fastapi.testclient import TestClient

from db_support import run_upgrade

SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
DATABASE_NAME = "servicedesk.db"


def _client(tmp_path: Path, mode: Mode = Mode.CI_FAKE) -> tuple[TestClient, Path]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    settings = Settings(mode=mode, runtime_dir=runtime)
    return TestClient(create_app(settings)), runtime


def _migrated_client(tmp_path: Path) -> tuple[TestClient, Path]:
    client, runtime = _client(tmp_path)
    run_upgrade(runtime / DATABASE_NAME)
    return client, runtime


def _text(client: TestClient) -> str:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    return response.text


def test_metrics_endpoint_exposes_base_series(tmp_path: Path) -> None:
    client, _runtime = _client(tmp_path)

    text = _text(client)

    assert re.search(r"(?m)^sdc_budget_configured 0(\.0)?$", text)
    for name in (
        "sdc_budget_refusals_total",
        "sdc_refusals_total",
        "sdc_escalations_total",
        "sdc_idempotent_retries_total",
        "sdc_duplicate_prevented_total",
    ):
        assert re.search(rf"(?m)^{name} 0(\.0)?$", text), name
    assert "sdc_http_requests_total{" not in text


def test_spend_series_absent_until_budget_configured(tmp_path: Path) -> None:
    client, _runtime = _client(tmp_path)

    text = _text(client)

    assert "sdc_provider_spend_usd_estimate_total" not in text
    assert re.search(r"(?m)^sdc_budget_configured 0(\.0)?$", text)


def test_request_metrics_increment_per_route(tmp_path: Path) -> None:
    client, _runtime = _client(tmp_path)

    client.get("/healthz")
    text = _text(client)

    assert (
        'sdc_http_requests_total{method="GET",route="/healthz",status="200"} 1.0'
        in text
    )
    assert 'route="/healthz"' in text


def test_unmatched_route_uses_fixed_label(tmp_path: Path) -> None:
    client, _runtime = _client(tmp_path)

    response = client.get("/definitely-not-a-route")
    text = _text(client)

    assert response.status_code == 404
    assert 'route="unmatched"' in text
    assert "definitely-not-a-route" not in text


def test_arbitrary_method_labeled_other(tmp_path: Path) -> None:
    client, _runtime = _client(tmp_path)

    client.request("BREW", "/healthz")
    text = _text(client)

    assert 'method="OTHER"' in text


def test_exception_records_500_and_reraises(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    app = create_app(Settings(runtime_dir=runtime))

    @app.get("/boom")
    def _boom() -> dict[str, str]:
        raise RuntimeError("boom")

    client = TestClient(app)

    with pytest.raises(RuntimeError):
        client.get("/boom")

    text = _text(client)
    assert 'route="/boom"' in text
    assert 'status="500"' in text


def test_repeated_factories_have_independent_registries(tmp_path: Path) -> None:
    first, _runtime = _client(tmp_path / "first")
    second, _second_runtime = _client(tmp_path / "second")

    first.get("/healthz")

    first_text = _text(first)
    second_text = _text(second)

    assert 'route="/healthz"' in first_text
    assert 'route="/healthz"' not in second_text


def test_metrics_do_not_write_audit_rows(tmp_path: Path) -> None:
    client, runtime = _migrated_client(tmp_path)

    for _ in range(3):
        client.get("/metrics")

    connection = sqlite3.connect(runtime / DATABASE_NAME)
    try:
        audit_rows = connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        tickets = connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    finally:
        connection.close()

    assert audit_rows == 0
    assert tickets == 0


def test_metrics_do_not_create_non_sidecar_files(tmp_path: Path) -> None:
    """Engine-owned sidecars are excluded; logical state is asserted elsewhere."""
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
    for _ in range(3):
        client.get("/metrics")
    after_names, after_contents = names(), contents()

    sidecars = {name for name in after_names if name.endswith(SIDECAR_SUFFIXES)}
    assert after_names - before_names <= sidecars
    assert after_contents == before_contents
