"""Provider registry and the budgeted composition (B5).

Exactly one provider per build; no silent fallback. The hosted branch wraps the
adapter in ``BudgetedProvider``, which reserves before the call and settles after
it. No production audit sink is wired here: the sink is an injected callable,
and no route or startup path exists in B5.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from app.audit.schema import AuditEvent
from app.config import Mode, Settings
from app.providers.base import (
    TOKEN_QUANTITY_LIMIT,
    ChatRequest,
    ChatResult,
    Provider,
    ProviderCallError,
    ProviderConfigStatus,
    ProviderError,
    ProviderUnavailable,
    validate_chat_request,
)
from app.providers.budget import BudgetLedger, BudgetPolicy
from app.providers.fake import FakeProvider
from app.providers.hosted import HostedProvider

AuditSink = Callable[[AuditEvent], None]


def provider_error_event(
    *,
    error_class: str,
    provider: str | None = None,
    model: str | None = None,
    attempt: int | None = None,
    retryable: bool = False,
) -> AuditEvent:
    """B2-conforming ``provider.error`` event (fixed registry, not extended)."""
    payload: dict[str, Any] = {"stage": "provider", "error_class": error_class}
    if provider is not None:
        payload["provider"] = provider
    if model is not None:
        payload["model"] = model
    if attempt is not None:
        payload["attempt"] = attempt
    payload["retryable"] = retryable
    return AuditEvent("provider.error", payload)


class BudgetedProvider:
    """Reserve -> call -> settle/unknown, with caps enforced before transport."""

    def __init__(
        self,
        provider: Provider,
        ledger: BudgetLedger,
        policy: BudgetPolicy,
        *,
        pricing: Any,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._provider = provider
        self._ledger = ledger
        self._policy = policy
        self._pricing = provider.pricing  # type: ignore[attr-defined]
        self._audit_sink = audit_sink
        self.name = f"budgeted:{provider.name}"

    def config_status(self) -> ProviderConfigStatus:
        return self._provider.config_status()

    def bound_prompt_tokens(self, request: ChatRequest) -> int:
        return self._provider.bound_prompt_tokens(request)

    def chat(self, request: ChatRequest, *, attempt: int = 1) -> ChatResult:
        validate_chat_request(request)
        bound = self._provider.bound_prompt_tokens(request)
        request_id = request.request_id

        self._ledger.reserve(
            request_id=request_id,
            attempt=attempt,
            bound_prompt_tokens=bound,
            max_tokens=request.max_tokens,
            pricing=self._pricing,
            policy=self._policy,
        )

        try:
            result = self._provider.chat(request)
        except ProviderCallError as exc:
            self._resolve_failure(request_id, attempt, exc)
            raise
        except ProviderError:
            self._ledger.mark_unknown(
                request_id=request_id,
                attempt=attempt,
                reason="provider_error",
                audit_sink=self._audit_sink,
            )
            raise
        except Exception:
            self._ledger.mark_unknown(
                request_id=request_id,
                attempt=attempt,
                reason="unexpected_error",
                audit_sink=self._audit_sink,
            )
            raise

        usage = result.usage
        if not self._usage_is_valid(usage):
            self._ledger.mark_unknown(
                request_id=request_id,
                attempt=attempt,
                reason="usage_unknown",
                audit_sink=self._audit_sink,
            )
            raise ProviderCallError("usage_unknown")

        self._ledger.settle(
            request_id=request_id,
            attempt=attempt,
            prompt_tokens=usage.prompt_tokens,  # type: ignore[arg-type]
            completion_tokens=usage.completion_tokens,  # type: ignore[arg-type]
            pricing=self._pricing,
        )
        return result

    def _resolve_failure(self, request_id: str, attempt: int, exc: ProviderCallError) -> None:
        usage = exc.usage
        if usage is not None and self._usage_is_valid(usage):
            self._ledger.settle(
                request_id=request_id,
                attempt=attempt,
                prompt_tokens=usage.prompt_tokens,  # type: ignore[arg-type]
                completion_tokens=usage.completion_tokens,  # type: ignore[arg-type]
                pricing=self._pricing,
            )
            return
        self._ledger.mark_unknown(
            request_id=request_id,
            attempt=attempt,
            reason=exc.code,
            audit_sink=self._audit_sink,
        )

    @staticmethod
    def _usage_is_valid(usage: object) -> bool:
        if usage is None:
            return False
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
        return (
            type(prompt) is int
            and type(completion) is int
            and 0 <= prompt <= TOKEN_QUANTITY_LIMIT
            and 0 <= completion <= TOKEN_QUANTITY_LIMIT
        )


def build(
    settings: Settings,
    *,
    audit_sink: AuditSink | None = None,
    token_counter: Callable[[ChatRequest], object] | None = None,
    transport: httpx.BaseTransport | None = None,
    pricing_path: Path | None = None,
) -> Provider:
    """Return exactly one provider, or raise ``ProviderUnavailable``."""
    if settings.mode is Mode.CI_FAKE or settings.mode is Mode.DEMO_FAKE:
        return FakeProvider()
    if settings.mode is Mode.HOSTED:
        hosted = HostedProvider(
            settings,
            token_counter=token_counter,
            transport=transport,
            pricing_path=pricing_path,
        )
        approval = hosted.approval
        policy = BudgetPolicy(
            calls_max=approval.calls_max,
            tokens_max=approval.tokens_max,
            usd_max=approval.usd_max,
        )
        ledger = BudgetLedger(
            settings.runtime_dir / "budget" / f"{approval.run_id}.json",
            run_id=approval.run_id,
        )
        return BudgetedProvider(
            hosted,
            ledger,
            policy,
            pricing=hosted.pricing,
            audit_sink=audit_sink,
        )
    raise ProviderUnavailable("unknown_mode")
