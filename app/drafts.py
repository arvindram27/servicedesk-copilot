"""Ticket draft route: one provider call, fake by default (Gate 1)."""

from __future__ import annotations

import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.providers.base import ChatRequest, Message, Provider, ProviderError
from app.providers.budget import BudgetError, BudgetExceeded
from app.providers.registry import build
from app.providers.tokens import bound_prompt_tokens
from app.security import problem
from app.security.csrf import require_csrf
from app.security.origin import classify_request
from app.tickets import TicketDraft, TicketDraftError, _category_id

router = APIRouter()

MESSAGE_MAX: int = 1000
MAX_TOKENS: int = 300
SCENE_ID: str = "ticket_draft"
SYSTEM_PROMPT: str = (
    "Draft a support ticket from the user message. Return one JSON object with "
    "exactly title, description and category. The category must be one of the "
    "active category names. Do not create a ticket."
)


def _guard(request: Request) -> JSONResponse | None:
    classification = classify_request(request)
    if isinstance(classification, JSONResponse):
        return classification
    request.state.client_kind = classification.client_kind
    return require_csrf(request)


def _active_categories(request: Request) -> list[str]:
    engine = request.app.state.audit_engine
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT name FROM categories WHERE active = 1 ORDER BY name")
        ).fetchall()
    return [str(row[0]) for row in rows]


def _get_provider(request: Request) -> Provider:
    existing = getattr(request.app.state, "provider", None)
    if existing is not None:
        return existing
    provider = build(request.app.state.settings, token_counter=bound_prompt_tokens)
    request.app.state.provider = provider
    return provider


def _draft_from_text(model_text: str) -> TicketDraft:
    payload = json.loads(model_text)
    if not isinstance(payload, dict):
        raise TicketDraftError("invalid_draft")
    return TicketDraft.from_mapping(
        {
            "title": payload.get("title"),
            "description": payload.get("description"),
            "category": payload.get("category"),
            "priority": "normal",
        }
    )


@router.post("/api/ticket-drafts", response_model=None)
def draft_ticket(request: Request, payload: Annotated[object, Body()]) -> JSONResponse:
    """Draft a ticket with one provider call; fake mode returns a fixed script."""
    blocked = _guard(request)
    if blocked is not None:
        return blocked
    if not isinstance(payload, dict):
        return problem(422, "invalid_request")
    message = payload.get("message")
    if type(message) is not str:
        return problem(422, "invalid_request")
    text_message = message.strip()
    if not text_message or len(text_message) > MESSAGE_MAX:
        return problem(422, "invalid_request")
    try:
        categories = _active_categories(request)
    except OperationalError:
        return problem(503, "store_busy")
    prompt = SYSTEM_PROMPT + " Allowed categories: " + ", ".join(categories) + "."
    chat_request = ChatRequest(
        messages=[
            Message(role="system", content=prompt),
            Message(role="user", content=text_message),
        ],
        max_tokens=MAX_TOKENS,
        temperature=0.0,
        request_id=uuid.uuid4().hex,
        scene_id=SCENE_ID,
    )
    try:
        result = _get_provider(request).chat(chat_request)
    except BudgetExceeded:
        return problem(503, "budget_exceeded")
    except (BudgetError, OSError):
        return problem(503, "budget_unavailable")
    except ProviderError:
        return problem(503, "provider_unavailable")
    try:
        draft = _draft_from_text(result.text)
        engine = request.app.state.audit_engine
        with engine.connect() as connection:
            _category_id(connection, draft.category)
    except (ValueError, TicketDraftError):
        return problem(422, "invalid_draft")
    except OperationalError:
        return problem(503, "store_busy")
    return JSONResponse(
        status_code=200,
        content={
            "draft": {
                "title": draft.title,
                "description": draft.description,
                "category": draft.category,
                "priority": draft.priority,
            },
            "is_fake": result.is_fake,
        },
    )
