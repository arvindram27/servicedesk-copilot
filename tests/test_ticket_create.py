"""Ticket creation slice (Gate 1): draft, confirmation token, immediate create.

Every test uses a temporary migrated SQLite database, an in-process TestClient
and the seeded local-user and Hardware category. No port, no network, no model.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from app.audit.schema import AuditEvent
from app.audit.writer import write_event
from app.config import Mode, Settings
from app.db.connection import create_engine_for
from app.main import create_app
from app.tickets import TicketDraft, TicketDraftError, immediate_transaction
from fastapi.testclient import TestClient
from scripts.seed_demo import seed_demo
from sqlalchemy.exc import OperationalError

from db_support import TOKEN_ID, run_upgrade, seed_basics

DRAFT: dict[str, object] = {
    "title": "Printer offline",
    "category": "hardware",
    "priority": "normal",
    "description": "The office printer shows offline.",
}
BASE_URL = "http://127.0.0.1:8000"


@dataclass(frozen=True)
class Env:
    settings: Settings
    client: TestClient
    headers: dict[str, str]
    db: Path


@pytest.fixture
def env(tmp_path: Path) -> Env:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    database = runtime / "servicedesk.db"
    run_upgrade(database)
    engine = create_engine_for(database)
    try:
        with engine.connect() as connection:
            seed_basics(connection)
    finally:
        engine.dispose()
    settings = Settings(mode=Mode.CI_FAKE, runtime_dir=runtime, database_path=database)
    client = TestClient(create_app(settings), base_url=BASE_URL)
    bootstrap = client.post("/api/session")
    assert bootstrap.status_code == 200
    body = bootstrap.json()
    headers = {"x-sdc-session": body["session_id"], "x-sdc-csrf": body["csrf_token"]}
    return Env(settings, client, headers, database)


def _query(db: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(db)
    try:
        rows = connection.execute(sql, params).fetchall()
    finally:
        connection.close()
    return [tuple(row) for row in rows]


def _count(db: Path, table: str) -> int:
    return int(_query(db, f"SELECT COUNT(*) FROM {table}")[0][0])


def _token_state(db: Path, token: str) -> tuple[object, object] | None:
    rows = _query(
        db,
        "SELECT consumed_at, ticket_id FROM confirmation_tokens WHERE token_id = ?",
        (token,),
    )
    return rows[0] if rows else None


def _audit(db: Path, event_type: str) -> list[dict[str, object]]:
    rows = _query(
        db,
        "SELECT payload_json FROM audit_events WHERE event_type = ? ORDER BY seq",
        (event_type,),
    )
    return [json.loads(str(row[0])) for row in rows]


def _issue(env: Env, draft: dict[str, object] = DRAFT) -> str:
    response = env.client.post(
        "/api/ticket-confirmations", json={"draft": draft}, headers=env.headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft_hash"] == TicketDraft.from_mapping(draft).draft_hash()
    return str(body["confirmation_token"])


def _create(
    env: Env,
    token: str,
    key: str,
    draft: dict[str, object] = DRAFT,
) -> object:
    return env.client.post(
        "/api/tickets",
        json={"draft": draft, "confirmation_token": token},
        headers={**env.headers, "idempotency-key": key},
    )


def _seed_token(env: Env, draft: dict[str, object]) -> str:
    token = f"seeded-{uuid4().hex}"
    connection = sqlite3.connect(env.db)
    try:
        connection.execute(
            "INSERT INTO confirmation_tokens"
            " (token_id, requester_label, session_id, draft_hash, issued_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                token,
                "local-user",
                env.headers["x-sdc-session"],
                TicketDraft.from_mapping(draft).draft_hash(),
                "2026-11-25T00:00:00+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return token


def test_same_key_retry_returns_stored_ticket(env: Env) -> None:
    """T-1: a same-key retry returns the stored ticket and writes one replay row."""
    key = str(uuid4())
    token = _issue(env)
    first = _create(env, token, key)
    assert first.status_code == 201, first.text
    ticket = first.json()
    second = _create(env, token, key)
    assert second.status_code == 200, second.text
    assert second.json()["ticket_id"] == ticket["ticket_id"]
    assert _count(env.db, "tickets") == 1
    assert _query(env.db, "SELECT status, ticket_id FROM idempotency_keys") == [
        ("completed", ticket["ticket_id"])
    ]
    assert len(_audit(env.db, "ticket.create")) == 1
    assert len(_audit(env.db, "ticket.replay")) == 1


def test_fresh_app_replays_and_missing_token_creates_nothing(env: Env) -> None:
    """T-2 (DB persistence only): a fresh app replays the stored ticket."""
    key = str(uuid4())
    token = _issue(env)
    first = _create(env, token, key)
    assert first.status_code == 201
    ticket_id = first.json()["ticket_id"]
    second_client = TestClient(create_app(env.settings), base_url=BASE_URL)
    bootstrap = second_client.post("/api/session")
    body = bootstrap.json()
    headers = {"x-sdc-session": body["session_id"], "x-sdc-csrf": body["csrf_token"]}
    replay = second_client.post(
        "/api/tickets",
        json={"draft": DRAFT, "confirmation_token": token},
        headers={**headers, "idempotency-key": key},
    )
    assert replay.status_code == 200
    assert replay.json()["ticket_id"] == ticket_id
    assert _count(env.db, "tickets") == 1
    missing = second_client.post(
        "/api/tickets",
        json={"draft": DRAFT},
        headers={**headers, "idempotency-key": str(uuid4())},
    )
    assert missing.status_code == 403
    assert _count(env.db, "tickets") == 1


def test_same_key_different_draft_is_rejected(env: Env) -> None:
    """T-6: the same key with a different draft is refused and changes nothing."""
    key = str(uuid4())
    token = _issue(env)
    first = _create(env, token, key)
    assert first.status_code == 201
    state = _token_state(env.db, token)
    changed = {**DRAFT, "title": "Printer on fire"}
    response = _create(env, token, key, draft=changed)
    assert response.status_code == 422
    assert response.json()["code"] == "idempotency_key_reused"
    assert _count(env.db, "tickets") == 1
    assert _token_state(env.db, token) == state
    assert _query(env.db, "SELECT status, ticket_id FROM idempotency_keys") == [
        ("completed", first.json()["ticket_id"])
    ]
    assert _audit(env.db, "ticket.reject")[-1]["reason"] == "idempotency_key_reused"


@pytest.mark.parametrize("extra", [{}, {"confirmation_token": "not-a-token"}])
def test_missing_or_text_token_is_rejected(env: Env, extra: dict[str, object]) -> None:
    """T-8: a missing or text-only token is refused and leaves the token unconsumed."""
    response = env.client.post(
        "/api/tickets",
        json={"draft": DRAFT, **extra},
        headers={**env.headers, "idempotency-key": str(uuid4())},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "confirmation_required"
    assert _count(env.db, "tickets") == 0
    assert _token_state(env.db, TOKEN_ID) == (None, None)
    assert _audit(env.db, "ticket.reject")[-1]["reason"] == "confirmation_required"


def test_changed_draft_is_rejected(env: Env) -> None:
    """T-9: a valid token used with a changed draft is refused and stays unconsumed."""
    token = _issue(env)
    changed = {**DRAFT, "description": "Changed after confirmation."}
    response = _create(env, token, str(uuid4()), draft=changed)
    assert response.status_code == 403
    assert response.json()["code"] == "confirmation_required"
    assert _count(env.db, "tickets") == 0
    assert _token_state(env.db, token) == (None, None)
    assert _audit(env.db, "ticket.reject")[-1]["reason"] == "draft_mismatch"


def test_consumed_token_with_fresh_key_is_rejected(env: Env) -> None:
    """T-10: a consumed token with a fresh key is refused; the original key replays."""
    key = str(uuid4())
    token = _issue(env)
    first = _create(env, token, key)
    assert first.status_code == 201
    response = _create(env, token, str(uuid4()))
    assert response.status_code == 403
    assert _count(env.db, "tickets") == 1
    assert _audit(env.db, "ticket.reject")[-1]["reason"] == "token_consumed"
    retry = _create(env, token, key)
    assert retry.status_code == 200
    assert retry.json()["ticket_id"] == first.json()["ticket_id"]


def test_token_bound_to_another_session_is_rejected(env: Env) -> None:
    """Token binding: a token from another session is refused and stays unconsumed."""
    token = _issue(env)
    bootstrap = env.client.post("/api/session")
    body = bootstrap.json()
    other = {"x-sdc-session": body["session_id"], "x-sdc-csrf": body["csrf_token"]}
    response = env.client.post(
        "/api/tickets",
        json={"draft": DRAFT, "confirmation_token": token},
        headers={**other, "idempotency-key": str(uuid4())},
    )
    assert response.status_code == 403
    assert _token_state(env.db, token) == (None, None)


@pytest.mark.parametrize("mode", ["missing", "not-uuid", "duplicate"])
def test_bad_idempotency_key_is_rejected(env: Env, mode: str) -> None:
    """Idempotency header: missing, non-UUID or duplicated is refused with a reject row."""
    headers: object = dict(env.headers)
    if mode == "not-uuid":
        headers["idempotency-key"] = "not-a-uuid"
    elif mode == "duplicate":
        key = str(uuid4())
        headers = list(env.headers.items()) + [("idempotency-key", key)] * 2
    response = env.client.post("/api/tickets", json={"draft": DRAFT}, headers=headers)
    assert response.status_code == 400
    assert response.json()["code"] == "idempotency_key_required"
    assert _count(env.db, "tickets") == 0
    assert _count(env.db, "idempotency_keys") == 0
    assert _audit(env.db, "ticket.reject")[-1]["reason"] == "idempotency_key_required"


@pytest.mark.parametrize(
    "bad",
    [{"priority": "critical"}, {"title": ""}, {"description": "   "}],
)
def test_bad_draft_fields_are_rejected_at_both_routes(env: Env, bad: dict[str, object]) -> None:
    """Draft validation: bad fields are refused at both routes with nothing created."""
    draft = {**DRAFT, **bad}
    before = _count(env.db, "confirmation_tokens")
    confirm = env.client.post(
        "/api/ticket-confirmations", json={"draft": draft}, headers=env.headers
    )
    assert confirm.status_code == 400
    assert confirm.json()["code"] == "invalid_draft"
    assert _count(env.db, "confirmation_tokens") == before
    response = env.client.post(
        "/api/tickets",
        json={"draft": draft},
        headers={**env.headers, "idempotency-key": str(uuid4())},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_draft"
    assert _count(env.db, "tickets") == 0


def test_unknown_category_is_rejected_without_consuming(env: Env) -> None:
    """Draft validation: an unknown category is refused at both routes."""
    draft = {**DRAFT, "category": "network"}
    before = _count(env.db, "confirmation_tokens")
    confirm = env.client.post(
        "/api/ticket-confirmations", json={"draft": draft}, headers=env.headers
    )
    assert confirm.status_code == 400
    assert confirm.json()["code"] == "invalid_draft"
    assert _count(env.db, "confirmation_tokens") == before
    token = _seed_token(env, draft)
    response = _create(env, token, str(uuid4()), draft=draft)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_draft"
    assert _count(env.db, "tickets") == 0
    assert _token_state(env.db, token) == (None, None)


def test_draft_canonicalization_and_validation() -> None:
    """Draft: hashing is stable across spacing and case, and invalid shapes raise."""
    canonical = TicketDraft.from_mapping(DRAFT)
    spaced = TicketDraft.from_mapping(
        {
            **DRAFT,
            "title": f"  {DRAFT['title']}  ",
            "category": "HARDWARE",
            "priority": "NORMAL",
        }
    )
    assert spaced == canonical
    assert spaced.draft_hash() == canonical.draft_hash()
    for field, value in [
        ("title", "Other"),
        ("category", "software"),
        ("priority", "low"),
        ("description", "Other"),
    ]:
        assert TicketDraft.from_mapping({**DRAFT, field: value}).draft_hash() != (
            canonical.draft_hash()
        )
    for bad in (None, "", {"title": ""}, {"title": 5}, {"priority": "critical"}):
        with pytest.raises(TicketDraftError):
            TicketDraft.from_mapping(bad)


def test_no_token_or_text_in_audit_or_logs(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    """No leak: a full create keeps token, title and description out of rows and logs."""
    caplog.set_level(logging.DEBUG)
    title = "Leak canary title"
    description = "Leak canary description body"
    draft = {**DRAFT, "title": title, "description": description}
    token = _issue(env, draft)
    response = _create(env, token, str(uuid4()), draft=draft)
    assert response.status_code == 201
    payloads = json.dumps(
        [json.loads(str(row[0])) for row in _query(env.db, "SELECT payload_json FROM audit_events")]
    )
    for needle in (token, title, description):
        assert needle not in payloads
        assert needle not in caplog.text


def test_immediate_transaction_helper(tmp_path: Path) -> None:
    """Immediate helper: in_transaction holds, audit writes, a second BEGIN is locked."""
    database = tmp_path / "proof.db"
    run_upgrade(database)
    engine = create_engine_for(database)
    try:
        with immediate_transaction(engine) as connection:
            assert connection.in_transaction()
            write_event(
                connection,
                AuditEvent(
                    "chat.answer",
                    {
                        "article_id": "PRN-001",
                        "article_version": 1,
                        "section": "Check the connection",
                        "citation_ok": True,
                        "top_k": 3,
                    },
                ),
            )
            holder = engine.connect()
            try:
                holder.exec_driver_sql("PRAGMA busy_timeout=1")
                with pytest.raises(OperationalError) as caught:
                    holder.exec_driver_sql("BEGIN IMMEDIATE")
                assert "locked" in str(caught.value).lower()
            finally:
                holder.close()
    finally:
        engine.dispose()


@pytest.mark.parametrize("route", ["confirm", "create"])
def test_locked_database_returns_store_busy(env: Env, route: str) -> None:
    """Locked database: both routes return the fixed 503 store_busy problem."""
    locker = sqlite3.connect(env.db, timeout=1, isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")
    try:
        if route == "confirm":
            response = env.client.post(
                "/api/ticket-confirmations", json={"draft": DRAFT}, headers=env.headers
            )
        else:
            response = env.client.post(
                "/api/tickets",
                json={"draft": DRAFT, "confirmation_token": "x"},
                headers={**env.headers, "idempotency-key": str(uuid4())},
            )
        assert response.status_code == 503
        assert response.json()["code"] == "store_busy"
    finally:
        locker.rollback()
        locker.close()


def test_audit_failure_rolls_back(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit failure: confirm and create return 503 and roll back every row."""

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit down")

    tokens_before = _count(env.db, "confirmation_tokens")
    monkeypatch.setattr("app.tickets.write_event", boom)
    confirm = env.client.post(
        "/api/ticket-confirmations", json={"draft": DRAFT}, headers=env.headers
    )
    assert confirm.status_code == 503
    assert confirm.json()["code"] == "audit_unavailable"
    assert _count(env.db, "confirmation_tokens") == tokens_before
    monkeypatch.undo()
    token = _issue(env)
    monkeypatch.setattr("app.tickets.write_event", boom)
    response = _create(env, token, str(uuid4()))
    assert response.status_code == 503
    assert response.json()["code"] == "audit_unavailable"
    assert _count(env.db, "tickets") == 0
    assert _token_state(env.db, token) == (None, None)
    assert _count(env.db, "idempotency_keys") == 0


def test_late_failure_rolls_back_after_consume(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rollback: a failure after the token consume leaves no ticket, key or consume."""
    token = _issue(env)

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("late failure")

    monkeypatch.setattr("app.tickets._insert_ticket", boom)
    response = _create(env, token, str(uuid4()))
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert _count(env.db, "tickets") == 0
    assert _token_state(env.db, token) == (None, None)
    assert _count(env.db, "idempotency_keys") == 0


def test_malformed_bodies_use_fixed_problem(env: Env) -> None:
    """Malformed bodies: non-object, missing draft and non-string token never echo input."""
    marker = "ECHO-MARKER-12345"
    for path in ("/api/ticket-confirmations", "/api/tickets"):
        response = env.client.post(path, json=[marker], headers=env.headers)
        assert response.status_code in {400, 422}
        assert response.headers["content-type"].startswith("application/problem+json")
        assert marker not in response.text
    for path in ("/api/ticket-confirmations", "/api/tickets"):
        missing = env.client.post(path, json={}, headers=env.headers)
        assert missing.status_code == 400
        assert missing.json()["code"] == "invalid_draft"
    nonstring = env.client.post(
        "/api/tickets",
        json={"draft": DRAFT, "confirmation_token": 123},
        headers={**env.headers, "idempotency-key": str(uuid4())},
    )
    assert nonstring.status_code == 403
    assert nonstring.json()["code"] == "confirmation_required"


def test_replay_from_second_session_returns_stored_ticket(env: Env) -> None:
    """Documented behavior: a same-key replay is requester-scoped and works from any session."""
    key = str(uuid4())
    token = _issue(env)
    first = _create(env, token, key)
    assert first.status_code == 201
    ticket_id = first.json()["ticket_id"]
    second_client = TestClient(create_app(env.settings), base_url=BASE_URL)
    bootstrap = second_client.post("/api/session")
    body = bootstrap.json()
    headers = {"x-sdc-session": body["session_id"], "x-sdc-csrf": body["csrf_token"]}
    confirm = second_client.post(
        "/api/ticket-confirmations", json={"draft": DRAFT}, headers=headers
    )
    assert confirm.status_code == 200
    new_token = confirm.json()["confirmation_token"]
    replay = second_client.post(
        "/api/tickets",
        json={"draft": DRAFT, "confirmation_token": new_token},
        headers={**headers, "idempotency-key": key},
    )
    assert replay.status_code == 200
    assert replay.json()["ticket_id"] == ticket_id
    assert _count(env.db, "tickets") == 1
    assert len(_audit(env.db, "ticket.replay")) == 1


@pytest.mark.parametrize("category", ["HARDWARE", "  hardware  "])
def test_category_name_case_and_outer_spaces_are_accepted(env: Env, category: str) -> None:
    """Category matching: case and outer spaces are ignored, and the id resolves."""
    draft = {**DRAFT, "category": category}
    token = _issue(env, draft)
    response = _create(env, token, str(uuid4()), draft=draft)
    assert response.status_code == 201
    assert _query(env.db, "SELECT category_id FROM tickets") == [("cat-hardware",)]


def test_inactive_category_is_rejected_at_confirm(env: Env) -> None:
    """Category matching: an inactive category is refused and mints no token."""
    connection = sqlite3.connect(env.db)
    try:
        connection.execute(
            "UPDATE categories SET active = 0 WHERE category_id = ?", ("cat-hardware",)
        )
        connection.commit()
    finally:
        connection.close()
    before = _count(env.db, "confirmation_tokens")
    response = env.client.post(
        "/api/ticket-confirmations", json={"draft": DRAFT}, headers=env.headers
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_draft"
    assert "confirmation_token" not in response.json()
    assert _count(env.db, "confirmation_tokens") == before


def test_category_deactivated_between_confirm_and_create_leaves_token_unused(env: Env) -> None:
    """Category matching: deactivation after confirm refuses create and keeps the token."""
    key = str(uuid4())
    token = _issue(env)
    connection = sqlite3.connect(env.db)
    try:
        connection.execute(
            "UPDATE categories SET active = 0 WHERE category_id = ?", ("cat-hardware",)
        )
        connection.commit()
    finally:
        connection.close()
    response = _create(env, token, key)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_draft"
    assert _token_state(env.db, token) == (None, None)
    assert _count(env.db, "tickets") == 0
    assert _count(env.db, "idempotency_keys") == 0


def test_seeded_database_runs_confirm_and_create(tmp_path: Path) -> None:
    """Seed path: a migrated, seeded database confirms and creates through the API."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    database = runtime / "seeded.db"
    run_upgrade(database)
    assert seed_demo(database) == (1, 1)
    settings = Settings(mode=Mode.CI_FAKE, runtime_dir=runtime, database_path=database)
    client = TestClient(create_app(settings), base_url=BASE_URL)
    bootstrap = client.post("/api/session")
    assert bootstrap.status_code == 200
    body = bootstrap.json()
    headers = {"x-sdc-session": body["session_id"], "x-sdc-csrf": body["csrf_token"]}
    key = str(uuid4())
    confirm = client.post("/api/ticket-confirmations", json={"draft": DRAFT}, headers=headers)
    assert confirm.status_code == 200, confirm.text
    token = confirm.json()["confirmation_token"]
    response = client.post(
        "/api/tickets",
        json={"draft": DRAFT, "confirmation_token": token},
        headers={**headers, "idempotency-key": key},
    )
    assert response.status_code == 201, response.text
    ticket_id = response.json()["ticket_id"]
    assert _count(database, "tickets") == 1
    state = _token_state(database, token)
    assert state is not None
    assert state[0] is not None
    assert state[1] == ticket_id
    assert _query(database, "SELECT status, ticket_id FROM idempotency_keys") == [
        ("completed", ticket_id)
    ]
