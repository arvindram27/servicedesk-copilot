"""Registry, budgeted composition and audit builders (B5)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import yaml
from app.audit.writer import write_event
from app.config import Mode, Settings
from app.audit.schema import AuditEvent
from app.providers.base import (
    TOKEN_QUANTITY_LIMIT,
    ChatRequest,
    ChatResult,
    Message,
    Pricing,
    ProviderCallError,
    ProviderConfigStatus,
    ProviderUnavailable,
    Usage,
)
from app.providers.budget import BudgetExceeded, BudgetLedger, BudgetPolicy
from app.providers.fake import FakeProvider
from app.providers.registry import BudgetedProvider, build, provider_error_event
from pydantic import SecretStr

from db_support import migrated_connection

SENTINEL_KEY = "sentinel-key-value-do-not-log"
BASE_URL = "https://api.example.test"


def _approval(**overrides: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "format": 1,
        "run_id": "run-1",
        "provider": "testprov",
        "model": "testmodel",
        "base_url": BASE_URL,
        "label": "test-run",
        "calls_max": 4,
        "tokens_max": 100000,
        "usd_max": "1.00",
    }
    doc.update(overrides)
    return doc


def _pricing_doc() -> dict[str, object]:
    return {
        "providers": {
            "testprov": {
                "models": {
                    "testmodel": {
                        "input_price_per_token": "0.000001",
                        "output_price_per_token": "0.000002",
                        "fixed_fee_per_request": "0.010000",
                    }
                }
            }
        }
    }


def _settings(tmp_path: Path, **overrides: object) -> tuple[Settings, Path]:
    runtime = tmp_path / "runtime"
    (runtime / "runs").mkdir(parents=True, exist_ok=True)
    (runtime / "runs" / "run-1.json").write_text(json.dumps(_approval()), encoding="utf-8")
    pricing_path = tmp_path / "pricing.yaml"
    pricing_path.write_text(yaml.safe_dump(_pricing_doc()), encoding="utf-8")
    values: dict[str, object] = {
        "mode": Mode.HOSTED,
        "runtime_dir": runtime,
        "allow_hosted_calls": True,
        "provider_name": "testprov",
        "provider_model": "testmodel",
        "provider_base_url": BASE_URL,
        "run_id": "run-1",
        "hosted_api_key": SecretStr(SENTINEL_KEY),
    }
    values.update(overrides)
    return Settings(**values), pricing_path


def _request(attempt_request_id: str = "req-1") -> ChatRequest:
    return ChatRequest(
        messages=[Message(role="user", content="hello")],
        max_tokens=50,
        temperature=0.0,
        request_id=attempt_request_id,
        scene_id="greeting",
    )


def _ledger_data(settings: Settings) -> dict[str, object]:
    path = Path(settings.runtime_dir) / "budget" / "run-1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _success_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        },
    )


def test_ci_fake_builds_only_the_fake(tmp_path: Path) -> None:
    settings, _pricing = _settings(tmp_path, mode=Mode.CI_FAKE)

    provider = build(settings)

    assert isinstance(provider, FakeProvider)


def test_demo_fake_builds_only_the_fake(tmp_path: Path) -> None:
    settings, _pricing = _settings(tmp_path, mode=Mode.DEMO_FAKE)

    assert isinstance(build(settings), FakeProvider)


def test_hosted_builds_the_budgeted_provider(tmp_path: Path) -> None:
    settings, pricing_path = _settings(tmp_path)

    provider = build(
        settings,
        token_counter=lambda request: 100,
        transport=httpx.MockTransport(lambda request: _success_response()),
        pricing_path=pricing_path,
    )

    assert isinstance(provider, BudgetedProvider)
    assert provider.name == "budgeted:hosted"


def test_hosted_failure_never_falls_back_to_the_fake(tmp_path: Path) -> None:
    settings, pricing_path = _settings(tmp_path, run_id="run-2")

    with pytest.raises(ProviderUnavailable) as excinfo:
        build(settings, token_counter=lambda request: 100, pricing_path=pricing_path)

    assert excinfo.value.code == "run_not_approved"


def test_successful_call_settles_the_reservation(tmp_path: Path) -> None:
    settings, pricing_path = _settings(tmp_path)
    provider = build(
        settings,
        token_counter=lambda request: 100,
        transport=httpx.MockTransport(lambda request: _success_response()),
        pricing_path=pricing_path,
    )

    result = provider.chat(_request())

    assert result.text == "ok"
    data = _ledger_data(settings)
    assert data["calls_settled"] == 1
    assert data["tokens_settled"] == 110
    assert data["usd_reserved"] == "0.000000"


def test_missing_usage_is_unknown_and_keeps_the_reservation(tmp_path: Path) -> None:
    settings, pricing_path = _settings(tmp_path)
    provider = build(
        settings,
        token_counter=lambda request: 100,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}}]}
            )
        ),
        pricing_path=pricing_path,
    )

    with pytest.raises(ProviderCallError) as excinfo:
        provider.chat(_request())

    assert excinfo.value.code == "usage_unknown"
    data = _ledger_data(settings)
    assert data["calls_reserved"] == 1
    assert data["attempts"][0]["state"] == "unknown"


class _StubUsageProvider:
    """Minimal provider double: returns one scripted usage, valid or not.

    The hosted adapter's ``_extract_usage`` pre-validates usage and collapses
    invalid values to ``None`` (the missing-usage unknown path), so invalid
    typed usage can reach the ``BudgetedProvider`` gate only from a provider
    that reports it verbatim. This stub is that direct injection point.
    """

    name = "stubprov"
    pricing = Pricing(
        input_price_per_token=Decimal("0.000001"),
        output_price_per_token=Decimal("0.000002"),
        fixed_fee_per_request=Decimal("0.010000"),
    )

    def __init__(self, usage: Usage) -> None:
        self._usage = usage

    def config_status(self) -> ProviderConfigStatus:
        return ProviderConfigStatus(present=True, valid=True, reason="ok")

    def bound_prompt_tokens(self, request: ChatRequest) -> int:
        return 100

    def chat(self, request: ChatRequest) -> ChatResult:
        return ChatResult(
            text="ok",
            provider="stubprov",
            model="stubmodel",
            is_fake=True,
            usage=self._usage,
            latency_ms=1,
            finish_reason="stop",
        )


def _budgeted_stub(tmp_path: Path, usage: Usage, events: list[AuditEvent]) -> BudgetedProvider:
    policy = BudgetPolicy(calls_max=4, tokens_max=100000, usd_max=Decimal("1.00"))
    ledger = BudgetLedger(tmp_path / "budget" / "run-1.json", run_id="run-1")
    stub = _StubUsageProvider(usage)
    return BudgetedProvider(
        stub, ledger, policy, pricing=stub.pricing, audit_sink=events.append
    )


@pytest.mark.parametrize(
    "usage",
    [
        Usage(prompt_tokens=True, completion_tokens=10),
        Usage(prompt_tokens=-1, completion_tokens=10),
        Usage(prompt_tokens=TOKEN_QUANTITY_LIMIT + 1, completion_tokens=10),
        Usage(prompt_tokens=100, completion_tokens=True),
        Usage(prompt_tokens=100, completion_tokens=-1),
        Usage(prompt_tokens=100, completion_tokens=TOKEN_QUANTITY_LIMIT + 1),
    ],
)
def test_e3_invalid_usage_is_unknown_and_keeps_the_reservation(
    tmp_path: Path, usage: Usage
) -> None:
    # Direct E3 adapter-gate coverage: a bool, negative or over-limit
    # provider-reported usage in either field must mark the attempt unknown,
    # keep the full reservation (150 tokens / 0.010200 USD), raise
    # usage_unknown and emit the budget.settle_unknown audit event.
    events: list[AuditEvent] = []
    provider = _budgeted_stub(tmp_path, usage, events)

    with pytest.raises(ProviderCallError) as excinfo:
        provider.chat(_request())

    assert excinfo.value.code == "usage_unknown"
    path = tmp_path / "budget" / "run-1.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["calls_reserved"] == 1
    assert data["calls_settled"] == 0
    assert data["tokens_reserved"] == 150
    assert data["usd_reserved"] == "0.010200"
    assert data["attempts"][0]["state"] == "unknown"
    assert data["attempts"][0]["unknown_reason"] == "usage_unknown"
    assert len(events) == 1
    assert events[0].event_type == "budget.settle_unknown"
    assert events[0].payload == {
        "run_id": "run-1",
        "reason": "usage_unknown",
        "calls_reserved": 1,
        "tokens_reserved": 150,
        "usd_reserved": "0.010200",
    }


def test_e3_zero_usage_is_a_valid_boundary(tmp_path: Path) -> None:
    # Valid-boundary control: zero usage in both fields is in-contract
    # (0 <= usage) and settles, with no settle_unknown event.
    events: list[AuditEvent] = []
    provider = _budgeted_stub(tmp_path, Usage(prompt_tokens=0, completion_tokens=0), events)

    result = provider.chat(_request())

    assert result.text == "ok"
    path = tmp_path / "budget" / "run-1.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["calls_settled"] == 1
    assert data["tokens_settled"] == 0
    assert data["usd_reserved"] == "0.000000"
    assert events == []


def test_m6_error_response_with_valid_usage_settles(tmp_path: Path) -> None:
    settings, pricing_path = _settings(tmp_path)
    provider = build(
        settings,
        token_counter=lambda request: 100,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                429,
                json={
                    "error": {"message": "rate limited"},
                    "usage": {"prompt_tokens": 100, "completion_tokens": 0},
                },
            )
        ),
        pricing_path=pricing_path,
    )

    with pytest.raises(ProviderCallError) as excinfo:
        provider.chat(_request())

    assert excinfo.value.code == "http_429"
    data = _ledger_data(settings)
    assert data["calls_settled"] == 1
    assert data["usd_reserved"] == "0.000000"


def test_m6_error_without_usage_is_unknown(tmp_path: Path) -> None:
    settings, pricing_path = _settings(tmp_path)
    provider = build(
        settings,
        token_counter=lambda request: 100,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, json={"error": {"message": "boom"}})
        ),
        pricing_path=pricing_path,
    )

    with pytest.raises(ProviderCallError):
        provider.chat(_request())

    data = _ledger_data(settings)
    assert data["calls_reserved"] == 1
    assert data["attempts"][0]["state"] == "unknown"


def test_m7_transport_exception_is_unknown_and_not_retried(tmp_path: Path) -> None:
    settings, pricing_path = _settings(tmp_path)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    provider = build(
        settings,
        token_counter=lambda request: 100,
        transport=httpx.MockTransport(handler),
        pricing_path=pricing_path,
    )

    with pytest.raises(ProviderCallError):
        provider.chat(_request())

    assert len(calls) == 1
    data = _ledger_data(settings)
    assert data["calls_reserved"] == 1
    assert data["attempts"][0]["state"] == "unknown"


def test_m7_a_new_attempt_reserves_again(tmp_path: Path) -> None:
    settings, pricing_path = _settings(tmp_path)
    provider = build(
        settings,
        token_counter=lambda request: 100,
        transport=httpx.MockTransport(lambda request: _success_response()),
        pricing_path=pricing_path,
    )

    provider.chat(_request(), attempt=1)
    provider.chat(_request(), attempt=2)

    data = _ledger_data(settings)
    assert data["calls_settled"] == 2
    assert len(data["attempts"]) == 2


def test_cap_breach_happens_before_transport(tmp_path: Path) -> None:
    settings, pricing_path = _settings(tmp_path)
    (Path(settings.runtime_dir) / "runs" / "run-1.json").write_text(
        json.dumps(_approval(calls_max=1)), encoding="utf-8"
    )
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _success_response()

    provider = build(
        settings,
        token_counter=lambda request: 100,
        transport=httpx.MockTransport(handler),
        pricing_path=pricing_path,
    )
    provider.chat(_request(), attempt=1)

    with pytest.raises(BudgetExceeded) as excinfo:
        provider.chat(_request(), attempt=2)

    assert excinfo.value.code == "calls_cap"
    assert len(calls) == 1


def test_problem_mapping_is_503(tmp_path: Path) -> None:
    unavailable = ProviderUnavailable("run_not_approved")
    exceeded = BudgetExceeded("calls_cap")

    for error in (unavailable, exceeded):
        response = error.to_problem()
        assert response.status_code == 503
        assert response.headers["content-type"].startswith("application/problem+json")


def test_provider_error_event_conforms_and_writes(tmp_path: Path) -> None:
    event = provider_error_event(
        error_class="transport_error",
        provider="testprov",
        model="testmodel",
        attempt=2,
        retryable=False,
    )

    database_path = tmp_path / "audit.db"
    with migrated_connection(database_path) as connection:
        with connection.begin():
            write_event(connection, event)

    import sqlite3

    reader = sqlite3.connect(database_path)
    try:
        row = reader.execute(
            "SELECT event_type, node, outcome, payload_json FROM audit_events"
        ).fetchone()
    finally:
        reader.close()

    assert row[0] == "provider.error"
    assert row[1] == "provider"
    assert row[2] == "error"
    payload = json.loads(row[3])
    assert set(payload) == {"stage", "error_class", "provider", "model", "attempt", "retryable"}
    assert payload["stage"] == "provider"
    assert payload["error_class"] == "transport_error"
    assert Decimal("0") == Decimal("0")
