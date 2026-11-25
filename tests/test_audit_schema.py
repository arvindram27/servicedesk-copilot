"""Schema-contract tests for the B2 audit event registry."""

from __future__ import annotations

import json

import pytest
from app.audit.schema import EVENT_TYPES, AuditEvent, AuditSchemaError, to_json


def _payload(event_type: str) -> dict[str, object]:
    if event_type == "migration.applied":
        return {
            "from_revision": None,
            "to_revision": "0001",
            "backup_path": "/tmp/backup.db",
            "skipped_reason": None,
            "schema_no_change": False,
        }
    if event_type == "provider.error":
        return {"stage": "chat", "error_class": "TimeoutError"}
    if event_type == "budget.settle_unknown":
        return {"run_id": "run-1", "reason": "timeout"}
    if event_type == "chat.answer":
        return {
            "article_id": "PRN-001",
            "article_version": 1,
            "section": "Check the connection",
            "citation_ok": True,
            "top_k": 3,
        }
    if event_type == "chat.refusal":
        return {"reason": "no_supporting_article"}
    if event_type == "ticket.confirm":
        return {"draft_hash": "draft-hash", "token_issued": True}
    if event_type == "ticket.create":
        return {"ticket_id": "INC-TEST", "draft_hash": "draft-hash"}
    if event_type == "ticket.replay":
        return {"ticket_id": "INC-TEST", "draft_hash": "draft-hash"}
    if event_type == "ticket.reject":
        return {"reason": "confirmation_required"}
    raise AssertionError(f"no required-key fixture for {event_type}")


def test_registry_contains_exactly_the_nine_types() -> None:
    assert set(EVENT_TYPES) == {
        "migration.applied",
        "provider.error",
        "budget.settle_unknown",
        "chat.answer",
        "chat.refusal",
        "ticket.confirm",
        "ticket.create",
        "ticket.replay",
        "ticket.reject",
    }


def _optional_payload(event_type: str) -> dict[str, object] | None:
    """Payload with every declared optional key supplied, or None if none exist."""
    if event_type == "provider.error":
        payload = _payload(event_type)
        payload.update(
            {
                "provider": "example-provider",
                "model": "example-model",
                "attempt": 2,
                "retryable": False,
            }
        )
        return payload
    if event_type == "budget.settle_unknown":
        payload = _payload(event_type)
        payload.update({"calls_reserved": 1, "tokens_reserved": 2048, "usd_reserved": "0.0100"})
        return payload
    if event_type == "chat.answer":
        payload = _payload(event_type)
        payload.update({"mode": "extractive"})
        return payload
    if event_type == "chat.refusal":
        payload = _payload(event_type)
        payload.update({"top_k": 3, "best_score_milli": 0})
        return payload
    return None


def test_each_registered_type_serializes_with_required_keys_only() -> None:
    for event_type, spec in EVENT_TYPES.items():
        payload = _payload(event_type)
        event = AuditEvent(event_type, payload)

        text = to_json(event.payload)
        round_tripped = json.loads(text)
        assert round_tripped == payload
        assert set(spec.required) <= set(round_tripped)
        assert set(round_tripped) <= set(spec.required) | set(spec.optional)
        assert event.event_id
        assert event.occurred_at is not None
        assert event.occurred_at.endswith("+00:00")
        assert event.node == spec.node
        assert event.outcome == spec.outcome


def test_optional_keys_round_trip_when_supplied() -> None:
    covered = 0
    for event_type, spec in EVENT_TYPES.items():
        payload = _optional_payload(event_type)
        if payload is None:
            assert not spec.optional
            continue
        covered += 1
        event = AuditEvent(event_type, payload)

        round_tripped = json.loads(to_json(event.payload))
        assert round_tripped == payload
        assert set(round_tripped) == set(spec.required) | set(spec.optional)
    assert covered == 4


def test_unknown_event_type_is_rejected() -> None:
    supplied = "totally-not-a-registered-type"
    with pytest.raises(AuditSchemaError) as excinfo:
        AuditEvent(supplied, {"stage": "chat", "error_class": "X"})

    assert excinfo.value.category == "unknown_event_type"
    assert supplied not in str(excinfo.value)


_MISSING_REQUIRED_CASES: list[tuple[str, str]] = [
    ("migration.applied", "from_revision"),
    ("migration.applied", "to_revision"),
    ("migration.applied", "backup_path"),
    ("migration.applied", "skipped_reason"),
    ("migration.applied", "schema_no_change"),
    ("provider.error", "stage"),
    ("provider.error", "error_class"),
    ("budget.settle_unknown", "run_id"),
    ("budget.settle_unknown", "reason"),
    ("chat.answer", "article_id"),
    ("chat.answer", "article_version"),
    ("chat.answer", "section"),
    ("chat.answer", "citation_ok"),
    ("chat.answer", "top_k"),
    ("chat.refusal", "reason"),
]


@pytest.mark.parametrize(("event_type", "field_name"), _MISSING_REQUIRED_CASES)
def test_each_required_payload_key_is_rejected(event_type: str, field_name: str) -> None:
    payload = _payload(event_type)
    del payload[field_name]
    with pytest.raises(AuditSchemaError) as excinfo:
        AuditEvent(event_type, payload)

    assert excinfo.value.category == "missing_required_key"
    assert excinfo.value.field_name == field_name


_EXTRA_KEY_CASES: list[tuple[str, str]] = [
    ("provider.error", "undeclared_key_name"),
    ("chat.answer", "message"),
    ("chat.refusal", "article_id"),
]


@pytest.mark.parametrize(("event_type", "extra_key"), _EXTRA_KEY_CASES)
def test_extra_payload_key_is_rejected(event_type: str, extra_key: str) -> None:
    payload = _payload(event_type)
    payload[extra_key] = "value"
    with pytest.raises(AuditSchemaError) as excinfo:
        AuditEvent(event_type, payload)

    assert excinfo.value.category == "extra_payload_key"
    assert extra_key not in str(excinfo.value)


_WRONG_TYPE_CASES: list[tuple[str, str, object]] = [
    ("chat.answer", "article_id", 1),
    ("chat.answer", "article_version", "1"),
    ("chat.answer", "article_version", True),
    ("chat.answer", "section", 1),
    ("chat.answer", "citation_ok", 1),
    ("chat.answer", "top_k", 3.0),
    ("chat.answer", "mode", 1),
    ("chat.refusal", "reason", 5),
    ("chat.refusal", "top_k", "3"),
    ("chat.refusal", "best_score_milli", 1.0),
]


@pytest.mark.parametrize(("event_type", "field_name", "bad_value"), _WRONG_TYPE_CASES)
def test_declared_payload_kinds_are_enforced(
    event_type: str, field_name: str, bad_value: object
) -> None:
    payload = _payload(event_type)
    payload[field_name] = bad_value
    with pytest.raises(AuditSchemaError) as excinfo:
        AuditEvent(event_type, payload)

    assert excinfo.value.category == "invalid_scalar_type"
    assert excinfo.value.field_name == field_name


def test_non_json_values_are_rejected() -> None:
    fixtures: list[object] = [object(), {1, 2}, b"bytes", float("nan"), float("inf")]
    for value in fixtures:
        with pytest.raises(AuditSchemaError):
            to_json({"value": value})


def test_depth_and_node_bounds_are_enforced() -> None:
    deep: object = 1
    for _ in range(6):
        deep = {"level": deep}
    with pytest.raises(AuditSchemaError) as excinfo:
        to_json({"nested": deep})
    assert excinfo.value.category == "payload_too_deep"

    with pytest.raises(AuditSchemaError) as excinfo:
        to_json({"many": list(range(120))})
    assert excinfo.value.category == "payload_too_large"


def test_secret_shaped_identifiers_are_rejected() -> None:
    payload = {"stage": "chat", "error_class": "TimeoutError"}

    with pytest.raises(AuditSchemaError) as excinfo:
        AuditEvent("provider.error", payload, session_id="sk-live-abcdefghijklmnop1234")
    assert excinfo.value.category == "secret_shaped_identifier"
    assert "sk-live" not in str(excinfo.value)

    with pytest.raises(AuditSchemaError):
        AuditEvent(
            "provider.error",
            payload,
            requester_label="0123456789abcdef0123456789abcdef",
        )

    ordinary = AuditEvent(
        "provider.error",
        payload,
        session_id="123e4567-e89b-12d3-a456-426614174000",
        requester_label="local-user",
    )
    assert ordinary.session_id == "123e4567-e89b-12d3-a456-426614174000"
    assert ordinary.requester_label == "local-user"
