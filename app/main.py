"""FastAPI app factory and wiring (B0: skeleton only).

No routes, database, audit writer, metrics or provider adapter exist yet. Those
arrive in later gates. Construction validates configuration, so a non-loopback
listen host is refused before the app exists.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.config import Settings
from app.health import router as health_router
from app.metrics import install_metrics
from app.session import SessionStore
from app.session import router as session_router
from app.stub_write import router as stub_router

VERSION = "0.0.0"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application, validating configuration at construction time."""
    resolved = settings if settings is not None else Settings()
    app = FastAPI(title="ServiceDesk Copilot", version=VERSION)
    app.state.settings = resolved
    app.state.sessions = SessionStore()
    app.include_router(health_router)
    app.include_router(session_router)
    app.include_router(stub_router)
    install_metrics(app)
    return app
