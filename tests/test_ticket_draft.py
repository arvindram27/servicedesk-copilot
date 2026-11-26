"""Ticket draft route tests with the fake provider only."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from app.config import Mode, Settings
from app.main import create_app
from app.providers.base import ChatRequest, Message, ProviderCallError
from app.providers.budget import LedgerCorrupt
from app.providers.tokens import bound_prompt_tokens
from fastapi.testclient import TestClient
from scripts.seed_demo import seed_demo

from db_support import run_upgrade

BASE_URL = "http://127.0.0.1:8000"


@dataclass(frozen=True)
class Env:
    client: TestClient
    headers: dict[str, str]
    db: Path


@pytest.fixture
def env(tmp_path: Path) -> Env:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    database = runtime / "drafts.db"
    run_upgrade(database)
    seed_demo(database)
    settings = Settings(mode=Mode.CI_FAKE, runtime_dir=runtime, database_path=database)
    client = TestClient(create_app(settings), base_url=BASE_URL)
    bootstrap = client.post("/api/session")
    assert bootstrap.status_code == 200
    body = bootstrap.json()
    headers = {"x-sdc-session": body["session_id"], "x-sdc-csrf": body["csrf_token"]}
    return Env(client, headers, database)


def _query(db: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(db)
    try:
        rows = connection.execute(sql, params).fetchall()
    finally:
        connection.close()
    return [tuple(row) for row in rows]


def _count(db: Path, table: str) -> int:
    return int(_query(db, f"SELECT COUNT(*) FROM {table}")[0][0])


def _counts(db: Path) -> tuple[int, int, int, int]:
    return (
        _count(db, "tickets"),
        _count(db, "confirmation_tokens"),
        _count(db, "idempotency_keys"),
        _count(db, "audit_events"),
    )


def _post(env: Env, message: str, headers: dict[str, str] | None = None) -> object:
    return env.client.post(
        "/api/ticket-drafts",
        json={"message": message},
        headers=env.headers if headers is None else headers,
    )


def test_draft_returns_a_validated_draft(env: Env) -> None:
    response = _post(env, "my printer is offline")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft"] == {
        "title": "Printer offline",
        "description": "The printer is offline.",
        "category": "hardware",
        "priority": "normal",
    }
    assert body["is_fake"] is True
    assert _query(env.db, "SELECT name FROM categories WHERE active = 1") == [("Hardware",)]


def test_draft_rejects_invalid_model_output(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.drafts.SCENE_ID", "ticket_draft_invalid")
    before = _counts(env.db)

    response = _post(env, "my printer is offline")

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_draft"
    assert _counts(env.db) == before


def test_draft_requires_session_and_csrf(env: Env) -> None:
    missing = _post(env, "my printer is offline", headers={})
    assert missing.status_code == 403
    assert missing.json()["code"] == "bad_session"

    session_only = {"x-sdc-session": env.headers["x-sdc-session"]}
    blocked = _post(env, "my printer is offline", headers=session_only)
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "missing_csrf"
    assert getattr(env.client.app.state, "provider", None) is None


def test_draft_writes_nothing(env: Env) -> None:
    before = _counts(env.db)

    assert _post(env, "my printer is offline").status_code == 200

    assert _counts(env.db) == before


def test_draft_rejects_overlong_message(env: Env) -> None:
    response = _post(env, "x" * 1001)

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"
    assert getattr(env.client.app.state, "provider", None) is None


def test_byte_bound_is_never_below_utf8_length() -> None:
    for message in ("printer offline", "reseau imprimante", "reseau imprime\u0301"):
        request = ChatRequest(
            messages=[
                Message(role="system", content="draft a ticket"),
                Message(role="user", content=message),
            ],
            max_tokens=300,
            temperature=0.0,
            request_id="req-bytes",
        )
        byte_count = sum(len(item.content.encode("utf-8")) for item in request.messages)
        assert bound_prompt_tokens(request) >= byte_count


def test_draft_then_confirm_then_create(env: Env) -> None:
    draft_response = _post(env, "my printer is offline")
    assert draft_response.status_code == 200, draft_response.text
    draft = draft_response.json()["draft"]

    confirm = env.client.post(
        "/api/ticket-confirmations", json={"draft": draft}, headers=env.headers
    )
    assert confirm.status_code == 200, confirm.text
    token = confirm.json()["confirmation_token"]

    key = str(uuid4())
    created = env.client.post(
        "/api/tickets",
        json={"draft": draft, "confirmation_token": token},
        headers={**env.headers, "idempotency-key": key},
    )
    assert created.status_code == 201, created.text
    ticket_id = created.json()["ticket_id"]
    assert _count(env.db, "tickets") == 1
    state = _query(
        env.db,
        "SELECT consumed_at, ticket_id FROM confirmation_tokens WHERE token_id = ?",
        (token,),
    )
    assert state[0][0] is not None
    assert state[0][1] == ticket_id
    assert _query(env.db, "SELECT status, ticket_id FROM idempotency_keys") == [
        ("completed", ticket_id)
    ]


def test_provider_failure_returns_fixed_503(env: Env) -> None:
    class FailingProvider:
        def chat(self, request: ChatRequest) -> object:
            raise ProviderCallError("transport_error")

    env.client.app.state.provider = FailingProvider()
    before = _counts(env.db)

    response = _post(env, "my printer is offline")

    assert response.status_code == 503
    assert response.json()["code"] == "provider_unavailable"
    assert "transport_error" not in response.text
    assert "boom" not in response.text
    assert _counts(env.db) == before


def test_budget_error_returns_fixed_503(env: Env) -> None:
    class FailingLedgerProvider:
        def chat(self, request: ChatRequest) -> object:
            raise LedgerCorrupt("ledger_unreadable")

    env.client.app.state.provider = FailingLedgerProvider()
    before = _counts(env.db)

    response = _post(env, "my printer is offline")

    assert response.status_code == 503
    assert response.json()["code"] == "budget_unavailable"
    assert "ledger_unreadable" not in response.text
    assert _counts(env.db) == before
