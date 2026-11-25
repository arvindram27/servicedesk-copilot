"""Deterministic cited-answer read path (Gate 1).

No model, no embeddings and no ticket write. Retrieval is lexical over a
verified synthetic snapshot; the reply is a fixed frame around a verbatim
excerpt, and every citation is checked against the same snapshot before it is
returned. Audit events use the existing closed registry and writer transaction
contract.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.audit.schema import AuditEvent
from app.audit.writer import write_event
from app.config import PROJECT_ROOT, Settings
from app.db.connection import create_engine_for
from app.kb.citation import (
    REFUSAL_REPLY,
    Citation,
    CitationError,
    build_citation,
    render_answer,
    validate_citation,
)
from app.kb.manifest import KbError, KbSnapshot, load_snapshot
from app.kb.retrieval import Chunk, RetrievalResult, index_articles, retrieve, select_supported
from app.security import problem
from app.security.origin import classify_request

MAX_MESSAGE_LENGTH: Final[int] = 1000
_NO_SUPPORT_REASON: Final[str] = "no_supporting_article"
_CITATION_REASON: Final[str] = "citation_invalid"

router = APIRouter()

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[str, str, str], tuple[KbSnapshot, tuple[Chunk, ...]]] = {}


class KbUnavailable(RuntimeError):
    """The verified knowledge base could not be loaded."""


class AuditUnavailable(RuntimeError):
    """The audit sink could not accept the event."""


@dataclass(frozen=True)
class AnswerOutcome:
    """One answered or refused turn."""

    state: Literal["answered", "refused"]
    reply: str
    citations: tuple[Citation, ...]
    audit_event_type: str
    audit_payload: dict[str, object]


def _snapshot_key(settings: Settings) -> tuple[str, str, str]:
    return (str(settings.kb_manifest_path), str(PROJECT_ROOT), str(settings.kb_dir))


def load_cached(settings: Settings) -> tuple[KbSnapshot, tuple[Chunk, ...]]:
    """Return the cached immutable snapshot, loading and verifying it once."""
    key = _snapshot_key(settings)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
        try:
            snapshot = load_snapshot(
                settings.kb_manifest_path,
                root=settings.kb_dir.resolve().parent.parent,
                kb_root=settings.kb_dir.resolve(),
            )
        except KbError:
            raise KbUnavailable from None
        cached = (snapshot, index_articles(snapshot))
        _CACHE[key] = cached
        return cached


def _score_milli(results: tuple[RetrievalResult, ...]) -> int:
    if not results:
        return 0
    return int(round(results[0].score * 1000))


def answer_question(
    message: str,
    *,
    chunks: tuple[Chunk, ...],
    top_k: int,
    min_score: float,
) -> AnswerOutcome:
    """Retrieve, cite and render one deterministic answer or refusal."""
    results = retrieve(chunks, message, top_k=top_k)
    supported = select_supported(results, min_score)
    if supported is None:
        return AnswerOutcome(
            state="refused",
            reply=REFUSAL_REPLY,
            citations=(),
            audit_event_type="chat.refusal",
            audit_payload={
                "reason": _NO_SUPPORT_REASON,
                "top_k": top_k,
                "best_score_milli": _score_milli(results),
            },
        )
    citation = build_citation(supported.chunk)
    try:
        validate_citation(citation, chunks)
    except CitationError:
        return AnswerOutcome(
            state="refused",
            reply=REFUSAL_REPLY,
            citations=(),
            audit_event_type="chat.refusal",
            audit_payload={
                "reason": _CITATION_REASON,
                "top_k": top_k,
                "best_score_milli": _score_milli(results),
            },
        )
    return AnswerOutcome(
        state="answered",
        reply=render_answer(citation),
        citations=(citation,),
        audit_event_type="chat.answer",
        audit_payload={
            "article_id": citation.article_id,
            "article_version": citation.version,
            "section": citation.section,
            "citation_ok": True,
            "top_k": top_k,
        },
    )


def _audit_engine(app: Any) -> Any:
    settings: Settings = app.state.settings
    path = settings.resolved_database_path
    if not path.exists():
        raise AuditUnavailable
    engine = getattr(app.state, "audit_engine", None)
    if engine is None:
        engine = create_engine_for(path)
        app.state.audit_engine = engine
    return engine


def _write_audit(app: Any, outcome: AnswerOutcome, correlation_id: str) -> None:
    engine = _audit_engine(app)
    event = AuditEvent(
        event_type=outcome.audit_event_type,
        payload=outcome.audit_payload,
        session_id=correlation_id,
        node="chat",
        outcome="ok" if outcome.state == "answered" else "refused",
    )
    try:
        with engine.begin() as connection:
            write_event(connection, event)
    except Exception:
        raise AuditUnavailable from None


@router.post("/chat")
async def chat(request: Request) -> JSONResponse:
    """Answer one synthetic support question with a checked citation or refuse."""
    classification = classify_request(request)
    if isinstance(classification, JSONResponse):
        return classification
    try:
        body = await request.json()
    except Exception:
        return problem(422, "invalid_request")
    if type(body) is not dict:
        return problem(422, "invalid_request")
    message = body.get("message")
    if type(message) is not str:
        return problem(422, "invalid_request")
    text = message.strip()
    if not text or len(text) > MAX_MESSAGE_LENGTH:
        return problem(422, "invalid_request")
    correlation_id = str(uuid.uuid4())
    conversation_id = body.get("conversation_id")
    if conversation_id is not None:
        if type(conversation_id) is not str:
            return problem(422, "invalid_request")
        try:
            correlation_id = str(UUID(conversation_id))
        except ValueError:
            return problem(422, "invalid_request")
    settings: Settings = request.app.state.settings
    try:
        chunks = load_cached(settings)[1]
    except KbUnavailable:
        return problem(503, "kb_unavailable")
    outcome = answer_question(
        text,
        chunks=chunks,
        top_k=settings.retrieval_top_k,
        min_score=settings.retrieval_min_score,
    )
    try:
        _write_audit(request.app, outcome, correlation_id)
    except AuditUnavailable:
        return problem(503, "audit_unavailable")
    return JSONResponse(
        status_code=200,
        content={
            "reply": outcome.reply,
            "citations": [
                {
                    "article_id": citation.article_id,
                    "version": citation.version,
                    "span": citation.span,
                    "section": citation.section,
                    "start": citation.start,
                    "end": citation.end,
                }
                for citation in outcome.citations
            ],
            "proposed_action": None,
            "state": outcome.state,
        },
    )
