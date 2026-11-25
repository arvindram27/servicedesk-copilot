"""In-process ticket creation slice (Gate 1).

The confirmation token is server-issued and bound to the exact draft hash, the
local requester label and the app session. The local ITSM adapter is a narrow
in-process stand-in for the later real ITSM client.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Final, Protocol

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import OperationalError

from app.audit.schema import AuditEvent
from app.audit.writer import write_event
from app.security import problem
from app.security.csrf import require_csrf
from app.security.origin import classify_request
from app.security.writes import require_writes_enabled

LOCAL_REQUESTER: Final[str] = "local-user"
PRIORITIES: Final[frozenset[str]] = frozenset({"low", "normal", "high", "urgent"})
TITLE_MAX: Final[int] = 200
DESCRIPTION_MAX: Final[int] = 2000
CATEGORY_MAX: Final[int] = 64

router = APIRouter()


class TicketDraftError(RuntimeError):
    """The draft is missing or malformed."""


class TicketAuditError(RuntimeError):
    """An audit write failed; the caller must roll back."""


@dataclass(frozen=True)
class TicketDraft:
    """A deterministic ticket draft built from explicit fields."""

    title: str
    category: str
    priority: str
    description: str

    def canonical(self) -> str:
        """Return the canonical UTF-8 JSON form used for the draft hash."""
        payload = {
            "title": self.title,
            "category": self.category,
            "priority": self.priority,
            "description": self.description,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def draft_hash(self) -> str:
        """Return the SHA-256 hex digest of the canonical draft."""
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()

    @classmethod
    def from_mapping(cls, raw: object) -> TicketDraft:
        """Validate and normalize a JSON draft object."""
        if not isinstance(raw, dict):
            raise TicketDraftError("invalid_draft")
        title = _bounded_text(raw.get("title"), TITLE_MAX)
        category = _bounded_text(raw.get("category"), CATEGORY_MAX).lower()
        priority = _bounded_text(raw.get("priority"), 16).lower()
        if priority not in PRIORITIES:
            raise TicketDraftError("invalid_draft")
        description = _bounded_text(raw.get("description"), DESCRIPTION_MAX)
        return cls(
            title=title,
            category=category,
            priority=priority,
            description=description,
        )


@dataclass(frozen=True)
class TicketResult:
    """The adapter result, mapped to a response by the route."""

    status: int
    code: str | None
    body: dict[str, object]


class TicketWriter(Protocol):
    """Narrow seam a later real ITSM client can implement."""

    def create(
        self,
        connection: Connection,
        *,
        draft: TicketDraft,
        confirmation_token: str,
        idempotency_key: str,
        requester_label: str,
        session_id: str,
    ) -> TicketResult: ...


@contextmanager
def immediate_transaction(engine: Engine) -> Iterator[Connection]:
    """Open one immediate SQLite write transaction and commit or roll back."""
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()


class LocalItsmAdapter:
    """Deterministic in-process ticket writer for the slice."""

    def create(
        self,
        connection: Connection,
        *,
        draft: TicketDraft,
        confirmation_token: str,
        idempotency_key: str,
        requester_label: str,
        session_id: str,
    ) -> TicketResult:
        if not _is_uuid(idempotency_key):
            return _reject(
                connection,
                reason="idempotency_key_required",
                status=400,
                code="idempotency_key_required",
                requester_label=requester_label,
                session_id=session_id,
            )

        key_row = (
            connection.execute(
                text(
                    "SELECT status, payload_fingerprint, response_json, ticket_id"
                    " FROM idempotency_keys"
                    " WHERE requester_label = :requester_label"
                    " AND idempotency_key = :idempotency_key"
                ),
                {"requester_label": requester_label, "idempotency_key": idempotency_key},
            )
            .mappings()
            .fetchone()
        )
        if key_row is not None:
            if key_row["status"] != "completed":
                return TicketResult(500, "internal_error", {})
            if key_row["payload_fingerprint"] != draft.draft_hash():
                return _reject(
                    connection,
                    reason="idempotency_key_reused",
                    status=422,
                    code="idempotency_key_reused",
                    requester_label=requester_label,
                    session_id=session_id,
                )
            _audit(
                connection,
                "ticket.replay",
                {"ticket_id": key_row["ticket_id"], "draft_hash": draft.draft_hash()},
                node="itsm",
                outcome="ok",
                requester_label=requester_label,
                session_id=session_id,
            )
            return TicketResult(200, None, json.loads(key_row["response_json"]))

        token_row = (
            connection.execute(
                text(
                    "SELECT requester_label, session_id, draft_hash, consumed_at"
                    " FROM confirmation_tokens"
                    " WHERE token_id = :token_id"
                ),
                {"token_id": confirmation_token},
            )
            .mappings()
            .fetchone()
        )
        if token_row is None:
            return _reject(
                connection,
                reason="confirmation_required",
                status=403,
                code="confirmation_required",
                requester_label=requester_label,
                session_id=session_id,
            )
        if token_row["requester_label"] != requester_label:
            return _reject(
                connection,
                reason="confirmation_required",
                status=403,
                code="confirmation_required",
                requester_label=requester_label,
                session_id=session_id,
            )
        if token_row["session_id"] != session_id:
            return _reject(
                connection,
                reason="confirmation_required",
                status=403,
                code="confirmation_required",
                requester_label=requester_label,
                session_id=session_id,
            )
        if token_row["draft_hash"] != draft.draft_hash():
            return _reject(
                connection,
                reason="draft_mismatch",
                status=403,
                code="confirmation_required",
                requester_label=requester_label,
                session_id=session_id,
            )
        if token_row["consumed_at"] is not None:
            return _reject(
                connection,
                reason="token_consumed",
                status=403,
                code="confirmation_required",
                requester_label=requester_label,
                session_id=session_id,
            )

        category_id = _category_id(connection, draft.category)
        ticket_id = _ticket_id()
        now = _now()
        consumed = connection.execute(
            text(
                "UPDATE confirmation_tokens"
                " SET consumed_at = :now, ticket_id = :ticket_id"
                " WHERE token_id = :token_id AND consumed_at IS NULL"
            ),
            {"now": now, "ticket_id": ticket_id, "token_id": confirmation_token},
        )
        if consumed.rowcount != 1:
            return _reject(
                connection,
                reason="token_consumed",
                status=403,
                code="confirmation_required",
                requester_label=requester_label,
                session_id=session_id,
            )

        ticket_body = _insert_ticket(
            connection,
            ticket_id=ticket_id,
            draft=draft,
            category_id=category_id,
            idempotency_key=idempotency_key,
            confirmation_token=confirmation_token,
            requester_label=requester_label,
            created_at=now,
        )
        _complete_key(
            connection,
            requester_label=requester_label,
            idempotency_key=idempotency_key,
            draft_hash=draft.draft_hash(),
            confirmation_token=confirmation_token,
            now=now,
            ticket_id=ticket_id,
            response=ticket_body,
        )
        _audit(
            connection,
            "ticket.create",
            {"ticket_id": ticket_id, "draft_hash": draft.draft_hash()},
            node="itsm",
            outcome="ok",
            requester_label=requester_label,
            session_id=session_id,
        )
        return TicketResult(201, None, ticket_body)


def _bounded_text(value: object, limit: int) -> str:
    if type(value) is not str:
        raise TicketDraftError("invalid_draft")
    text_value = value.strip()
    if not text_value or len(text_value) > limit:
        raise TicketDraftError("invalid_draft")
    return text_value


def _category_id(connection: Connection, category: str) -> str:
    row = connection.execute(
        text("SELECT category_id FROM categories WHERE active = 1 AND name = :name COLLATE NOCASE"),
        {"name": category},
    ).fetchone()
    if row is None:
        raise TicketDraftError("invalid_draft")
    return str(row[0])


def _audit(
    connection: Connection,
    event_type: str,
    payload: dict[str, object],
    *,
    node: str,
    outcome: str,
    requester_label: str,
    session_id: str,
) -> None:
    try:
        event = AuditEvent(
            event_type,
            payload,
            session_id=session_id,
            requester_label=requester_label,
            node=node,
            outcome=outcome,
        )
        write_event(connection, event)
    except Exception:
        raise TicketAuditError from None


def _reject(
    connection: Connection,
    *,
    reason: str,
    status: int,
    code: str,
    requester_label: str,
    session_id: str,
) -> TicketResult:
    _audit(
        connection,
        "ticket.reject",
        {"reason": reason},
        node="itsm",
        outcome="refused",
        requester_label=requester_label,
        session_id=session_id,
    )
    return TicketResult(status, code, {})


def _insert_ticket(
    connection: Connection,
    *,
    ticket_id: str,
    draft: TicketDraft,
    category_id: str,
    idempotency_key: str,
    confirmation_token: str,
    requester_label: str,
    created_at: str,
) -> dict[str, object]:
    connection.execute(
        text(
            "INSERT INTO tickets"
            " (ticket_id, requester_label, title, description, category_id, priority,"
            " status, idempotency_key, confirmation_token_id, created_at)"
            " VALUES (:ticket_id, :requester_label, :title, :description, :category_id,"
            " :priority, 'open', :idempotency_key, :confirmation_token, :created_at)"
        ),
        {
            "ticket_id": ticket_id,
            "requester_label": requester_label,
            "title": draft.title,
            "description": draft.description,
            "category_id": category_id,
            "priority": draft.priority,
            "idempotency_key": idempotency_key,
            "confirmation_token": confirmation_token,
            "created_at": created_at,
        },
    )
    return {
        "ticket_id": ticket_id,
        "idempotency_key": idempotency_key,
        "title": draft.title,
        "category": draft.category,
        "priority": draft.priority,
        "description": draft.description,
        "status": "open",
    }


def _complete_key(
    connection: Connection,
    *,
    requester_label: str,
    idempotency_key: str,
    draft_hash: str,
    confirmation_token: str,
    now: str,
    ticket_id: str,
    response: dict[str, object],
) -> None:
    response_json = json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    connection.execute(
        text(
            "INSERT INTO idempotency_keys"
            " (requester_label, idempotency_key, payload_fingerprint,"
            " confirmation_token_id, status, claimed_at, completed_at, owner_run_id,"
            " owner_generation, response_status, response_json, ticket_id)"
            " VALUES (:requester_label, :idempotency_key, :draft_hash,"
            " :confirmation_token, 'completed', :now, :now, 'local', 1, 201,"
            " :response_json, :ticket_id)"
        ),
        {
            "requester_label": requester_label,
            "idempotency_key": idempotency_key,
            "draft_hash": draft_hash,
            "confirmation_token": confirmation_token,
            "now": now,
            "response_json": response_json,
            "ticket_id": ticket_id,
        },
    )


def _issue_token(
    connection: Connection,
    *,
    draft: TicketDraft,
    requester_label: str,
    session_id: str,
) -> str:
    _category_id(connection, draft.category)
    token = secrets.token_urlsafe(32)
    now = _now()
    connection.execute(
        text(
            "INSERT INTO confirmation_tokens"
            " (token_id, requester_label, session_id, draft_hash, issued_at)"
            " VALUES (:token_id, :requester_label, :session_id, :draft_hash, :issued_at)"
        ),
        {
            "token_id": token,
            "requester_label": requester_label,
            "session_id": session_id,
            "draft_hash": draft.draft_hash(),
            "issued_at": now,
        },
    )
    _audit(
        connection,
        "ticket.confirm",
        {"draft_hash": draft.draft_hash(), "token_issued": True},
        node="ticket",
        outcome="ok",
        requester_label=requester_label,
        session_id=session_id,
    )
    return token


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


def _is_locked(exc: OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _ticket_id() -> str:
    return f"INC{uuid.uuid4().hex[:8].upper()}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _guard(request: Request) -> JSONResponse | None:
    classification = classify_request(request)
    if isinstance(classification, JSONResponse):
        return classification
    request.state.client_kind = classification.client_kind
    blocked = require_writes_enabled(request)
    if blocked is not None:
        return blocked
    return require_csrf(request)


@router.post("/api/ticket-confirmations", response_model=None)
def confirm_ticket(request: Request, payload: Annotated[object, Body()]) -> JSONResponse:
    blocked = _guard(request)
    if blocked is not None:
        return blocked
    if not isinstance(payload, dict):
        return problem(400, "invalid_draft")
    session_id = request.headers["x-sdc-session"]
    try:
        draft = TicketDraft.from_mapping(payload.get("draft"))
    except TicketDraftError:
        return problem(400, "invalid_draft")
    engine: Engine = request.app.state.audit_engine
    try:
        with engine.begin() as connection:
            token = _issue_token(
                connection,
                draft=draft,
                requester_label=LOCAL_REQUESTER,
                session_id=session_id,
            )
    except TicketDraftError:
        return problem(400, "invalid_draft")
    except TicketAuditError:
        return problem(503, "audit_unavailable")
    except OperationalError as exc:
        if _is_locked(exc):
            return problem(503, "store_busy")
        return problem(500, "internal_error")
    return JSONResponse(
        status_code=200,
        content={"confirmation_token": token, "draft_hash": draft.draft_hash()},
    )


@router.post("/api/tickets", response_model=None)
def create_ticket(request: Request, payload: Annotated[object, Body()]) -> JSONResponse:
    blocked = _guard(request)
    if blocked is not None:
        return blocked
    if not isinstance(payload, dict):
        return problem(400, "invalid_draft")
    session_id = request.headers["x-sdc-session"]
    try:
        draft = TicketDraft.from_mapping(payload.get("draft"))
    except TicketDraftError:
        return problem(400, "invalid_draft")
    token = payload.get("confirmation_token")
    confirmation_token = token if type(token) is str else ""
    key_values = request.headers.getlist("idempotency-key")
    idempotency_key = key_values[0] if len(key_values) == 1 else ""
    engine: Engine = request.app.state.audit_engine
    writer = LocalItsmAdapter()
    try:
        with immediate_transaction(engine) as connection:
            result = writer.create(
                connection,
                draft=draft,
                confirmation_token=confirmation_token,
                idempotency_key=idempotency_key,
                requester_label=LOCAL_REQUESTER,
                session_id=session_id,
            )
    except TicketDraftError:
        return problem(400, "invalid_draft")
    except TicketAuditError:
        return problem(503, "audit_unavailable")
    except OperationalError as exc:
        if _is_locked(exc):
            return problem(503, "store_busy")
        return problem(500, "internal_error")
    except Exception:
        return problem(500, "internal_error")
    if result.status in {200, 201}:
        return JSONResponse(status_code=result.status, content=result.body)
    return problem(result.status, result.code or "confirmation_required")
