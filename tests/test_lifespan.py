"""ASGI lifespan and audit-engine disposal (Gate 1 hardening)."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient


class _RecordingEngine:
    """Minimal engine stand-in that records disposal."""

    def __init__(self) -> None:
        self.dispose_calls = 0

    def dispose(self) -> None:
        self.dispose_calls += 1


def test_lifespan_disposes_the_app_owned_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    engine = _RecordingEngine()
    monkeypatch.setattr("app.main.create_engine_for", lambda _path: engine)
    app = create_app(Settings(runtime_dir=runtime, database_path=runtime / "servicedesk.db"))

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert app.state.audit_engine is engine
        assert engine.dispose_calls == 0

    assert engine.dispose_calls == 1
