"""Key- and token-shaped strings are redacted from application logs."""

from __future__ import annotations

import io
import json
import logging

import pytest
from app.logging import (
    FALLBACK_LINE,
    REJECTED_MARKER,
    RedactionFilter,
    configure_logging,
    redact,
)

API_KEY = "sk-live-abcdefghijklmnop1234"
GOOGLE_KEY = "AIzaSyA1234567890abcdefghijklmnop"
BEARER = "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"
CONFIRMATION_TOKEN = "confirmation_token=ct_9f8e7d6c5b4a"
HEX_ID = "0123456789abcdef0123456789abcdef"
ENV_STYLE = "OPENAI_API_KEY=sk-live-abcdefghijklmnop1234"
BEARER_VALUE = BEARER.split(" ", 1)[1]


class _BrokenStream(io.StringIO):
    """A stream whose write always fails, to exercise logging's error path."""

    def write(self, text: str) -> int:
        raise OSError("simulated stream failure")


def _last_payload(buffer: io.StringIO) -> dict[str, object]:
    return json.loads(buffer.getvalue().splitlines()[-1])


@pytest.mark.parametrize(
    "secret",
    [API_KEY, GOOGLE_KEY, BEARER, CONFIRMATION_TOKEN, HEX_ID, ENV_STYLE],
)
def test_redact_removes_the_secret(secret: str) -> None:
    cleaned = redact(f"call failed for {secret}")
    assert "[REDACTED]" in cleaned
    assert secret not in cleaned


def test_redact_leaves_plain_text_untouched() -> None:
    text = "ticket created for requester label demo-user"
    assert redact(text) == text


def test_filter_redacts_formatted_arguments() -> None:
    record = logging.LogRecord(
        name="sdc",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="provider key %s used",
        args=(API_KEY,),
        exc_info=None,
    )
    assert RedactionFilter().filter(record) is True
    rendered = record.getMessage()
    assert API_KEY not in rendered
    assert "[REDACTED]" in rendered


def test_json_formatter_emits_one_redacted_object() -> None:
    buffer = io.StringIO()
    logger = configure_logging(level=logging.INFO, stream=buffer)
    logger.info("attempt with %s", API_KEY, extra={"event": "provider.call"})

    line = buffer.getvalue().strip()
    payload = json.loads(line)
    assert payload["event"] == "provider.call"
    assert payload["level"] == "INFO"
    assert API_KEY not in line
    assert "[REDACTED]" in payload["message"]


def test_configure_logging_keeps_a_single_handler() -> None:
    first = configure_logging(level=logging.INFO, stream=io.StringIO())
    second = configure_logging(level=logging.INFO, stream=io.StringIO())
    assert first is second
    assert len(second.handlers) == 1


def test_token_shapes_are_redacted_in_the_full_log_line() -> None:
    buffer = io.StringIO()
    logger = configure_logging(level=logging.INFO, stream=buffer)
    logger.info(
        "confirm idempotency_key=%s token=%s header=%s",
        HEX_ID,
        CONFIRMATION_TOKEN,
        BEARER,
        extra={"outcome": "refused"},
    )

    line = buffer.getvalue()
    assert HEX_ID not in line
    assert CONFIRMATION_TOKEN.split("=", 1)[1] not in line
    assert BEARER.split(" ", 1)[1] not in line
    assert json.loads(line)["outcome"] == "refused"


def test_lg1_container_extra_is_rejected() -> None:
    buffer = io.StringIO()
    logger = configure_logging(level=logging.INFO, stream=buffer)
    logger.info(
        "container extras",
        extra={"event": {"authorization": BEARER}, "outcome": [BEARER, API_KEY]},
    )

    line = buffer.getvalue()
    payload = _last_payload(buffer)
    assert payload["event"] == REJECTED_MARKER
    assert payload["outcome"] == REJECTED_MARKER
    assert BEARER_VALUE not in line
    assert API_KEY not in line


def test_lg2_custom_object_is_replaced_without_conversion() -> None:
    calls = {"str": 0, "repr": 0}

    class Boom:
        def __str__(self) -> str:
            calls["str"] += 1
            return API_KEY

        def __repr__(self) -> str:
            calls["repr"] += 1
            return API_KEY

    buffer = io.StringIO()
    logger = configure_logging(level=logging.INFO, stream=buffer)
    logger.info("custom object", extra={"outcome": Boom()})

    line = buffer.getvalue()
    assert _last_payload(buffer)["outcome"] == REJECTED_MARKER
    assert calls == {"str": 0, "repr": 0}
    assert API_KEY not in line


def test_lg3_str_subclass_does_not_bypass_redaction() -> None:
    class SneakyStr(str):
        pass

    buffer = io.StringIO()
    logger = configure_logging(level=logging.INFO, stream=buffer)
    logger.info("subclass", extra={"event": SneakyStr(API_KEY)})

    line = buffer.getvalue()
    assert _last_payload(buffer)["event"] == REJECTED_MARKER
    assert API_KEY not in line


def test_lg4_benign_scalars_preserve_types_and_none_is_omitted() -> None:
    buffer = io.StringIO()
    logger = configure_logging(level=logging.INFO, stream=buffer)
    logger.info(
        "benign scalars",
        extra={"request_id": 12345, "outcome": True, "session_id": 1.5},
    )
    payload = _last_payload(buffer)
    assert payload["request_id"] == 12345
    assert type(payload["request_id"]) is int
    assert payload["outcome"] is True
    assert type(payload["session_id"]) is float

    logger.info("none extra", extra={"event": None})
    assert "event" not in _last_payload(buffer)


def test_lg5_secret_shaped_string_extra_is_redacted() -> None:
    buffer = io.StringIO()
    logger = configure_logging(level=logging.INFO, stream=buffer)
    logger.info("string extra", extra={"event": API_KEY})

    line = buffer.getvalue()
    assert API_KEY not in line
    assert "[REDACTED]" in str(_last_payload(buffer)["event"])


def test_lg6_non_finite_float_is_rejected() -> None:
    buffer = io.StringIO()
    logger = configure_logging(level=logging.INFO, stream=buffer)
    logger.info("nan", extra={"session_id": float("nan")})
    assert _last_payload(buffer)["session_id"] == REJECTED_MARKER

    logger.info("inf", extra={"session_id": float("inf")})
    assert _last_payload(buffer)["session_id"] == REJECTED_MARKER


def test_lg7_serializer_failure_emits_the_fixed_fallback_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = io.StringIO()
    logger = configure_logging(level=logging.INFO, stream=buffer)
    calls = {"count": 0}

    def failing_dumps(*args: object, **kwargs: object) -> str:
        calls["count"] += 1
        raise ValueError("simulated serializer failure")

    monkeypatch.setattr(json, "dumps", failing_dumps)
    logger.info("serializer failure with %s", API_KEY, extra={"event": API_KEY})

    line = buffer.getvalue().strip()
    assert line == FALLBACK_LINE
    assert calls["count"] == 1
    assert API_KEY not in line
    assert "[LOG_FORMATTING_FAILED]" in line


def test_lg8_emit_failure_surfaces_without_leaking(
    capsys: pytest.CaptureFixture[str],
) -> None:
    broken = _BrokenStream()
    logger = configure_logging(level=logging.INFO, stream=broken)
    logger.info("emit failure with %s", API_KEY, extra={"event": API_KEY})

    captured = capsys.readouterr()
    assert captured.err.strip() != ""
    assert "Logging error" in captured.err
    assert API_KEY not in captured.err
