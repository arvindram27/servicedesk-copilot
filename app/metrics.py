"""Base metrics and the ``/metrics`` exposition (B3).

One ``CollectorRegistry`` per application, stored on ``app.state``. The global
default registry is never used, so repeated ``create_app`` calls stay
independent and tests cannot contaminate each other.

The spend counter is deliberately absent: no approved budget configuration
exists, so ``sdc_budget_configured`` is pinned to 0 and
``sdc_provider_spend_usd_estimate_total`` is never created. An absent series is
honest; a zero would not be. Nothing here reads a pricing file.

Request accounting is a pure ASGI middleware: it never buffers bodies, never
opens a database and never calls a provider. Labels are closed — the route
template or the fixed label ``unmatched``, an allowlisted method or ``OTHER``,
and the integer status or ``OTHER``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Final

from fastapi import FastAPI, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

ALLOWED_METHODS: Final[frozenset[str]] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)
UNMATCHED_ROUTE: Final[str] = "unmatched"
OTHER_LABEL: Final[str] = "OTHER"


class BaseMetrics:
    """The base collector set bound to one registry."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self.registry = registry
        self.http_requests = Counter(
            "sdc_http_requests_total",
            "HTTP requests by route template, method and status.",
            ["route", "method", "status"],
            registry=registry,
        )
        self.http_duration = Histogram(
            "sdc_http_request_duration_seconds",
            "HTTP request duration by route template and method.",
            ["route", "method"],
            registry=registry,
        )
        self.provider_calls = Counter(
            "sdc_provider_calls_total",
            "Provider calls by provider and outcome.",
            ["provider", "outcome"],
            registry=registry,
        )
        self.provider_tokens = Counter(
            "sdc_provider_tokens_total",
            "Provider tokens by kind.",
            ["kind"],
            registry=registry,
        )
        self.budget_configured = Gauge(
            "sdc_budget_configured",
            "1 when an approved budget configuration exists, else 0.",
            registry=registry,
        )
        self.budget_configured.set(0)
        self.budget_refusals = Counter(
            "sdc_budget_refusals_total",
            "Requests refused by the model-call budget.",
            registry=registry,
        )
        self.guardrail_hits = Counter(
            "sdc_guardrail_hits_total",
            "Guardrail hits by guardrail name.",
            ["guardrail"],
            registry=registry,
        )
        self.refusals = Counter(
            "sdc_refusals_total",
            "Policy refusals.",
            registry=registry,
        )
        self.escalations = Counter(
            "sdc_escalations_total",
            "Escalations.",
            registry=registry,
        )
        self.ticket_creates = Counter(
            "sdc_ticket_creates_total",
            "Ticket creates by outcome.",
            ["outcome"],
            registry=registry,
        )
        self.idempotent_retries = Counter(
            "sdc_idempotent_retries_total",
            "Idempotent retries.",
            registry=registry,
        )
        self.duplicate_prevented = Counter(
            "sdc_duplicate_prevented_total",
            "Duplicate writes prevented by the idempotency layer.",
            registry=registry,
        )


def _template_for(route: object) -> str | None:
    for attribute in ("path_format", "path"):
        value = getattr(route, attribute, None)
        if isinstance(value, str) and value:
            return value
    return None


def _route_label(scope: Scope, routes: list[Any]) -> str:
    matched = scope.get("route")
    if matched is not None:
        template = _template_for(matched)
        if template is not None:
            return template
    for candidate in routes:
        match, _child_scope = candidate.matches(scope)
        if match is Match.FULL:
            template = _template_for(candidate)
            if template is not None:
                return template
    return UNMATCHED_ROUTE


def _method_label(scope: Scope) -> str:
    method = scope.get("method", "")
    if not isinstance(method, str):
        return OTHER_LABEL
    upper = method.upper()
    return upper if upper in ALLOWED_METHODS else OTHER_LABEL


def _status_label(status: int) -> str:
    return str(status) if 100 <= status <= 599 else OTHER_LABEL


class RequestMetricsMiddleware:
    """Pure ASGI request accounting: no buffering, no dependencies."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        metrics: BaseMetrics,
        routes: Callable[[], list[Any]],
    ) -> None:
        self.app = app
        self.metrics = metrics
        self._routes = routes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 500
        failed = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            failed = True
            raise
        finally:
            self._record(scope, 500 if failed else status, started)

    def _record(self, scope: Scope, status: int, started: float) -> None:
        route = _route_label(scope, self._routes())
        method = _method_label(scope)
        self.metrics.http_requests.labels(
            route=route, method=method, status=_status_label(status)
        ).inc()
        self.metrics.http_duration.labels(route=route, method=method).observe(
            max(0.0, time.perf_counter() - started)
        )


def install_metrics(app: FastAPI) -> None:
    """Create this app's registry, collectors, middleware and ``/metrics``."""
    registry = CollectorRegistry()
    metrics = BaseMetrics(registry)
    app.state.metrics_registry = registry
    app.state.metrics = metrics
    app.add_middleware(
        RequestMetricsMiddleware,
        metrics=metrics,
        routes=lambda: list(app.router.routes),
    )

    @app.get("/metrics")
    def metrics_endpoint() -> Response:
        body = generate_latest(registry)
        return Response(content=body, headers={"Content-Type": CONTENT_TYPE_LATEST})
