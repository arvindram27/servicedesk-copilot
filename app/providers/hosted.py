"""Hosted OpenAI-compatible adapter (B5).

Disabled by default and fail-closed: mode, opt-in flag, run identity, provider
configuration, endpoint policy, approval file, pricing, tokenization and key
presence are checked in that order, and no HTTP client is constructed until all
of them pass. No vendor SDK is used. Every transport exception is UNKNOWN in
this build: the reservation stands and nothing is released.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from app.config import PROJECT_ROOT, Mode, Settings
from app.providers.base import (
    TOKEN_QUANTITY_LIMIT,
    ChatRequest,
    ChatResult,
    Pricing,
    ProviderCallError,
    ProviderConfigStatus,
    ProviderUnavailable,
    Usage,
    parse_money,
    validate_chat_request,
)
from app.providers.budget import RunApproval, load_approval

RUN_ID_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PRICING_PATH = PROJECT_ROOT / "config" / "pricing.yaml"
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
CONNECT_TIMEOUT_S = 5.0
WRITE_TIMEOUT_S = 10.0
POOL_TIMEOUT_S = 5.0

TokenCounter = Callable[[ChatRequest], object]


def validate_run_id(run_id: object) -> str:
    if type(run_id) is not str or not run_id:
        raise ProviderUnavailable("run_id_missing")
    if RUN_ID_RE.match(run_id) is None or ".." in run_id:
        raise ProviderUnavailable("run_id_invalid")
    return run_id


def validate_base_url(base_url: object) -> str:
    """Enforce https (loopback http for tests only), no userinfo, no redirects."""
    if type(base_url) is not str or not base_url:
        raise ProviderUnavailable("invalid_base_url")
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"}:
        raise ProviderUnavailable("invalid_base_url")
    if parts.username or parts.password:
        raise ProviderUnavailable("invalid_base_url")
    if parts.query or parts.fragment or not parts.hostname:
        raise ProviderUnavailable("invalid_base_url")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        raise ProviderUnavailable("invalid_base_url")
    return base_url


def load_pricing(provider: str, model: str, path: Path | None = None) -> Pricing:
    """Load one complete pricing entry; any absence or bad value fails closed."""
    import yaml

    pricing_path = PRICING_PATH if path is None else Path(path)
    try:
        raw = pricing_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError):
        raise ProviderUnavailable("pricing_unavailable") from None
    try:
        entry = data["providers"][provider]["models"][model]
    except (KeyError, TypeError):
        raise ProviderUnavailable("pricing_incomplete") from None
    if type(entry) is not dict:
        raise ProviderUnavailable("pricing_incomplete")
    required = {"input_price_per_token", "output_price_per_token", "fixed_fee_per_request"}
    if not required <= set(entry):
        raise ProviderUnavailable("pricing_incomplete")
    try:
        return Pricing(
            input_price_per_token=parse_money(entry["input_price_per_token"], allow_zero=False),
            output_price_per_token=parse_money(entry["output_price_per_token"], allow_zero=False),
            fixed_fee_per_request=parse_money(entry["fixed_fee_per_request"], allow_zero=True),
        )
    except ValueError:
        raise ProviderUnavailable("pricing_incomplete") from None


def _extract_usage(payload: Any) -> Usage:
    if not isinstance(payload, dict):
        return Usage(prompt_tokens=None, completion_tokens=None)
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return Usage(prompt_tokens=None, completion_tokens=None)
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if (
        type(prompt) is int
        and type(completion) is int
        and 0 <= prompt <= TOKEN_QUANTITY_LIMIT
        and 0 <= completion <= TOKEN_QUANTITY_LIMIT
    ):
        return Usage(prompt_tokens=prompt, completion_tokens=completion)
    return Usage(prompt_tokens=None, completion_tokens=None)


class HostedProvider:
    """Thin OpenAI-compatible client behind the provider protocol."""

    name = "hosted"

    def __init__(
        self,
        settings: Settings,
        *,
        token_counter: TokenCounter | None = None,
        transport: httpx.BaseTransport | None = None,
        pricing_path: Path | None = None,
    ) -> None:
        if settings.mode is not Mode.HOSTED:
            raise ProviderUnavailable("mode_not_hosted")
        if not settings.allow_hosted_calls:
            raise ProviderUnavailable("hosted_calls_disabled")
        run_id = validate_run_id(settings.run_id)

        provider = settings.provider_name
        model = settings.provider_model
        base_url = settings.provider_base_url
        if not provider or not model or not base_url:
            raise ProviderUnavailable("provider_not_configured")
        validate_base_url(base_url)

        approval: RunApproval = load_approval(settings.runtime_dir, run_id)
        if approval.provider != provider or approval.model != model:
            raise ProviderUnavailable("approval_mismatch")
        if approval.base_url != base_url:
            raise ProviderUnavailable("approval_base_url_mismatch")

        pricing = load_pricing(provider, model, pricing_path)

        if token_counter is None:
            raise ProviderUnavailable("tokenization_unavailable")

        key: SecretStr | None = settings.hosted_api_key
        if key is None:
            raise ProviderUnavailable("missing_key")

        timeout = httpx.Timeout(
            connect=CONNECT_TIMEOUT_S,
            read=settings.provider_timeout_s,
            write=WRITE_TIMEOUT_S,
            pool=POOL_TIMEOUT_S,
        )
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.run_id = run_id
        self.approval = approval
        self.pricing = pricing
        self._token_counter = token_counter
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            follow_redirects=False,
            headers={"Authorization": f"Bearer {key.get_secret_value()}"},
            transport=transport,
        )

    def config_status(self) -> ProviderConfigStatus:
        return ProviderConfigStatus(present=True, valid=True, reason="configured")

    def bound_prompt_tokens(self, request: ChatRequest) -> int:
        validate_chat_request(request)
        try:
            value = self._token_counter(request)
        except Exception:  # noqa: BLE001 - any counter failure fails closed
            raise ProviderUnavailable("tokenization_unavailable") from None
        if value is None:
            raise ProviderUnavailable("tokenization_unavailable")
        if type(value) is not int or value <= 0 or value > TOKEN_QUANTITY_LIMIT:
            raise ProviderUnavailable("tokenization_invalid")
        return value

    def chat(self, request: ChatRequest) -> ChatResult:
        validate_chat_request(request)
        self.bound_prompt_tokens(request)

        body = {
            "model": self.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        started = time.perf_counter()
        try:
            response = self._client.post("/chat/completions", json=body)
        except httpx.HTTPError:
            raise ProviderCallError("transport_error", usage=None) from None
        latency_ms = int((time.perf_counter() - started) * 1000)

        try:
            payload = response.json()
        except ValueError:
            raise ProviderCallError("protocol_error", usage=None) from None
        usage = _extract_usage(payload)

        status = response.status_code
        if status >= 300:
            code = "protocol_error" if status < 400 else f"http_{status}"
            raise ProviderCallError(code, usage=usage)

        try:
            choice = payload["choices"][0]
            text = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError):
            raise ProviderCallError("protocol_error", usage=usage) from None
        if type(text) is not str:
            raise ProviderCallError("protocol_error", usage=usage)

        return ChatResult(
            text=text,
            provider=self.provider,
            model=self.model,
            is_fake=False,
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
        )
