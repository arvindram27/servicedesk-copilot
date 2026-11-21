"""Provider adapter behaviour (B5): fake, pricing, policy and enablement."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml
from app.config import Mode, Settings
from app.providers.base import (
    ChatRequest,
    Message,
    ProviderCallError,
    ProviderUnavailable,
    validate_chat_request,
)
from app.providers.fake import SCENES, FakeProvider, FakeSceneMissing
from app.providers.hosted import (
    HostedProvider,
    load_pricing,
    validate_base_url,
    validate_run_id,
)
from pydantic import SecretStr, ValidationError

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


def _request(scene_id: str | None = "greeting") -> ChatRequest:
    return ChatRequest(
        messages=[Message(role="user", content="hello")],
        max_tokens=50,
        temperature=0.0,
        request_id="req-1",
        scene_id=scene_id,
    )


def test_fake_returns_the_scripted_scene() -> None:
    result = FakeProvider().chat(_request())

    scene = SCENES["greeting"]
    assert result.text == scene.text
    assert result.is_fake is True
    assert result.usage.prompt_tokens == scene.prompt_tokens
    assert result.usage.completion_tokens == scene.completion_tokens


def test_fake_unknown_scene_raises() -> None:
    with pytest.raises(FakeSceneMissing):
        FakeProvider().chat(_request("nope"))


def test_fake_missing_scene_id_raises() -> None:
    with pytest.raises(FakeSceneMissing):
        FakeProvider().chat(_request(None))


def test_fake_is_deterministic() -> None:
    provider = FakeProvider()

    assert provider.chat(_request()) == provider.chat(_request())


def test_fake_bound_matches_the_scripted_prompt_count() -> None:
    assert FakeProvider().bound_prompt_tokens(_request()) == SCENES["greeting"].prompt_tokens


@pytest.mark.parametrize(
    "chat_request",
    [
        ChatRequest([], 10, 0.0, "r"),
        ChatRequest([Message(role="tool", content="x")], 10, 0.0, "r"),  # type: ignore[arg-type]
        ChatRequest([Message(role="user", content="x")], 0, 0.0, "r"),
        ChatRequest([Message(role="user", content="x")], True, 0.0, "r"),  # type: ignore[arg-type]
        ChatRequest([Message(role="user", content="x")], 10, 3.0, "r"),
        ChatRequest([Message(role="user", content="x")], 10, 0.0, ""),
    ],
)
def test_chat_request_validation_rejects_bad_input(chat_request: ChatRequest) -> None:
    with pytest.raises(Exception):
        validate_chat_request(chat_request)


def test_pricing_loader_accepts_a_complete_entry(tmp_path: Path) -> None:
    _settings_value, pricing_path = _settings(tmp_path)

    pricing = load_pricing("testprov", "testmodel", pricing_path)

    assert str(pricing.input_price_per_token) == "0.000001"
    assert str(pricing.fixed_fee_per_request) == "0.010000"


@pytest.mark.parametrize(
    "mutation",
    [
        {"input_price_per_token": "0"},
        {"input_price_per_token": 0.000001},
        {"output_price_per_token": "-1"},
        {"fixed_fee_per_request": "1e999999999"},
        {"input_price_per_token": "1.0000000000001"},
    ],
)
def test_pricing_loader_rejects_bad_values(tmp_path: Path, mutation: dict[str, object]) -> None:
    doc = _pricing_doc()
    entry = doc["providers"]["testprov"]["models"]["testmodel"]  # type: ignore[index]
    entry.update(mutation)  # type: ignore[union-attr]
    pricing_path = tmp_path / "bad-pricing.yaml"
    pricing_path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    with pytest.raises(ProviderUnavailable) as excinfo:
        load_pricing("testprov", "testmodel", pricing_path)

    assert excinfo.value.code == "pricing_incomplete"


def test_pricing_loader_allows_a_zero_fixed_fee(tmp_path: Path) -> None:
    doc = _pricing_doc()
    doc["providers"]["testprov"]["models"]["testmodel"][  # type: ignore[index]
        "fixed_fee_per_request"
    ] = "0"
    pricing_path = tmp_path / "zero-fee.yaml"
    pricing_path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    assert str(load_pricing("testprov", "testmodel", pricing_path).fixed_fee_per_request) == "0"


def test_pricing_loader_rejects_a_missing_entry(tmp_path: Path) -> None:
    pricing_path = tmp_path / "empty.yaml"
    pricing_path.write_text("providers: {}\n", encoding="utf-8")

    with pytest.raises(ProviderUnavailable) as excinfo:
        load_pricing("testprov", "testmodel", pricing_path)

    assert excinfo.value.code == "pricing_incomplete"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user@example.com",
        "https://example.com?x=1",
        "https://example.com#frag",
        "ftp://example.com",
        "not-a-url",
    ],
)
def test_endpoint_policy_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(ProviderUnavailable) as excinfo:
        validate_base_url(url)

    assert excinfo.value.code == "invalid_base_url"


def test_endpoint_policy_allows_loopback_http_for_tests() -> None:
    assert validate_base_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000"


@pytest.mark.parametrize("run_id", ["", None, "../evil", "a/b", "run id"])
def test_run_id_validation_rejects_unsafe_values(run_id: object) -> None:
    with pytest.raises(ProviderUnavailable):
        validate_run_id(run_id)


def test_hosted_raises_before_transport_under_ci_fake(tmp_path: Path) -> None:
    settings, pricing_path = _settings(tmp_path, mode=Mode.CI_FAKE)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    with pytest.raises(ProviderUnavailable) as excinfo:
        HostedProvider(
            settings,
            token_counter=lambda request: 10,
            transport=httpx.MockTransport(handler),
            pricing_path=pricing_path,
        )

    assert excinfo.value.code == "mode_not_hosted"
    assert calls == []


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"run_id": None}, "run_id_missing"),
        ({"run_id": "../evil"}, "run_id_invalid"),
        ({"provider_name": None}, "provider_not_configured"),
        ({"provider_base_url": "http://example.com"}, "invalid_base_url"),
        ({"hosted_api_key": None}, "missing_key"),
    ],
)
def test_hosted_enablement_fails_closed(
    tmp_path: Path, overrides: dict[str, object], code: str
) -> None:
    settings, pricing_path = _settings(tmp_path, **overrides)

    with pytest.raises(ProviderUnavailable) as excinfo:
        HostedProvider(
            settings, token_counter=lambda request: 10, pricing_path=pricing_path
        )

    assert excinfo.value.code == code


def test_hosted_mode_without_opt_in_is_refused_by_settings() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mode=Mode.HOSTED, allow_hosted_calls=False)


def test_hosted_calls_disabled_is_enforced_by_the_adapter(tmp_path: Path) -> None:
    # model_construct deliberately bypasses Settings validation so the
    # adapter-level hosted_calls_disabled guard is reached in isolation.
    # The Settings opt-in validator itself is unchanged and covered above.
    settings, pricing_path = _settings(tmp_path)
    bypassed = Settings.model_construct(
        mode=Mode.HOSTED,
        allow_hosted_calls=False,
        runtime_dir=Path(settings.runtime_dir),
        provider_name="testprov",
        provider_model="testmodel",
        provider_base_url=BASE_URL,
        hosted_api_key=SecretStr(SENTINEL_KEY),
        run_id="run-1",
    )

    with pytest.raises(ProviderUnavailable) as excinfo:
        HostedProvider(
            bypassed, token_counter=lambda request: 10, pricing_path=pricing_path
        )

    assert excinfo.value.code == "hosted_calls_disabled"


def test_hosted_requires_an_approval_file(tmp_path: Path) -> None:
    # run-2 has no approval file; only run-1.json exists.
    settings, pricing_path = _settings(tmp_path, run_id="run-2")

    with pytest.raises(ProviderUnavailable) as excinfo:
        HostedProvider(
            settings, token_counter=lambda request: 10, pricing_path=pricing_path
        )

    assert excinfo.value.code == "run_not_approved"


def test_hosted_requires_approval_binding(tmp_path: Path) -> None:
    settings, pricing_path = _settings(tmp_path)
    runtime = Path(settings.runtime_dir)
    (runtime / "runs" / "run-1.json").write_text(
        json.dumps(_approval(provider="other")), encoding="utf-8"
    )

    with pytest.raises(ProviderUnavailable) as excinfo:
        HostedProvider(
            settings, token_counter=lambda request: 10, pricing_path=pricing_path
        )

    assert excinfo.value.code == "approval_mismatch"

    (runtime / "runs" / "run-1.json").write_text(
        json.dumps(_approval(base_url="https://other.example.test")), encoding="utf-8"
    )
    with pytest.raises(ProviderUnavailable) as excinfo:
        HostedProvider(
            settings, token_counter=lambda request: 10, pricing_path=pricing_path
        )

    assert excinfo.value.code == "approval_base_url_mismatch"


def test_hosted_requires_tokenization(tmp_path: Path) -> None:
    settings, pricing_path = _settings(tmp_path)

    with pytest.raises(ProviderUnavailable) as excinfo:
        HostedProvider(settings, token_counter=None, pricing_path=pricing_path)

    assert excinfo.value.code == "tokenization_unavailable"


def test_key_value_is_absent_from_the_tested_serialization_paths(tmp_path: Path) -> None:
    settings, _pricing_path = _settings(tmp_path)

    assert SENTINEL_KEY not in repr(settings)
    assert SENTINEL_KEY not in str(settings)
    assert SENTINEL_KEY not in str(settings.model_dump())
    assert SENTINEL_KEY not in settings.model_dump_json()


def test_f2_hosted_key_constructor_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SDC_HOSTED_API_KEY", raising=False)
    monkeypatch.delenv("HOSTED_API_KEY", raising=False)

    settings = Settings(
        _env_file=None,
        mode=Mode.HOSTED,
        allow_hosted_calls=True,
        hosted_api_key=SecretStr(SENTINEL_KEY),
    )

    assert settings.hosted_api_key is not None
    assert settings.hosted_api_key.get_secret_value() == SENTINEL_KEY


def test_f2_hosted_key_prefixed_env_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOSTED_API_KEY", raising=False)
    monkeypatch.setenv("SDC_HOSTED_API_KEY", SENTINEL_KEY)

    settings = Settings(_env_file=None)

    assert settings.hosted_api_key is not None
    assert settings.hosted_api_key.get_secret_value() == SENTINEL_KEY


def test_f2_hosted_key_unprefixed_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SDC_HOSTED_API_KEY", raising=False)
    monkeypatch.setenv("HOSTED_API_KEY", SENTINEL_KEY)

    settings = Settings(_env_file=None)

    assert settings.hosted_api_key is None


@pytest.mark.parametrize("value", [None, 0, -1, True, "10"])
def test_m8_token_counter_results_are_validated(tmp_path: Path, value: object) -> None:
    settings, pricing_path = _settings(tmp_path)
    provider = HostedProvider(
        settings, token_counter=lambda request: value, pricing_path=pricing_path
    )

    with pytest.raises(ProviderUnavailable) as excinfo:
        provider.bound_prompt_tokens(_request())

    assert excinfo.value.code in {"tokenization_unavailable", "tokenization_invalid"}


def test_m8_valid_token_counter_is_accepted(tmp_path: Path) -> None:
    settings, pricing_path = _settings(tmp_path)
    provider = HostedProvider(
        settings, token_counter=lambda request: 100, pricing_path=pricing_path
    )

    assert provider.bound_prompt_tokens(_request()) == 100


def test_m7_transport_exception_is_unknown_with_no_usage(tmp_path: Path) -> None:
    settings, pricing_path = _settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    provider = HostedProvider(
        settings,
        token_counter=lambda request: 100,
        transport=httpx.MockTransport(handler),
        pricing_path=pricing_path,
    )

    with pytest.raises(ProviderCallError) as excinfo:
        provider.chat(_request())

    assert excinfo.value.code == "transport_error"
    assert excinfo.value.usage is None


def test_redirect_is_not_followed(tmp_path: Path) -> None:
    settings, pricing_path = _settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.test"})

    provider = HostedProvider(
        settings,
        token_counter=lambda request: 100,
        transport=httpx.MockTransport(handler),
        pricing_path=pricing_path,
    )

    with pytest.raises(ProviderCallError) as excinfo:
        provider.chat(_request())

    assert excinfo.value.code == "protocol_error"


def test_body_carries_only_the_declared_fields(tmp_path: Path) -> None:
    settings, pricing_path = _settings(tmp_path)
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            },
        )

    provider = HostedProvider(
        settings,
        token_counter=lambda request: 100,
        transport=httpx.MockTransport(handler),
        pricing_path=pricing_path,
    )
    provider.chat(_request())

    body = json.loads(captured[0])
    assert set(body) == {"model", "messages", "max_tokens", "temperature"}
    assert "CONFIRMATION-TOKEN-SENTINEL" not in captured[0].decode("utf-8")
