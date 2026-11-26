"""FastAPI app factory and wiring (B0: skeleton only).

No routes, database, audit writer, metrics or provider adapter exist yet. Those
arrive in later gates. Construction validates configuration, so a non-loopback
listen host is refused before the app exists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.chat import router as chat_router
from app.config import Settings
from app.db.connection import create_engine_for
from app.drafts import router as drafts_router
from app.health import router as health_router
from app.metrics import install_metrics
from app.session import SessionStore
from app.session import router as session_router
from app.stub_write import router as stub_router
from app.tickets import router as tickets_router

VERSION = "0.0.0"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Dispose the app-owned audit engine once, after serving ends."""
    try:
        yield
    finally:
        engine = getattr(app.state, "audit_engine", None)
        if engine is not None:
            engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application, validating configuration at construction time."""
    resolved = settings if settings is not None else Settings()
    app = FastAPI(title="ServiceDesk Copilot", version=VERSION, lifespan=_lifespan)
    app.state.settings = resolved
    app.state.sessions = SessionStore()
    app.state.audit_engine = create_engine_for(resolved.resolved_database_path)
    app.include_router(health_router)
    app.include_router(session_router)
    app.include_router(stub_router)
    app.include_router(tickets_router)
    app.include_router(drafts_router)
    app.include_router(chat_router)
    install_metrics(app)
    return app
