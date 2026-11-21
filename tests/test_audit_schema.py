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
    return {"run_id": "run-1", "reason": "timeout"}


def test_registry_contains_exactly_the_three_types() -> None:
    assert set(EVENT_TYPES) == {"migration.applied", "provider.error", "budget.settle_unknown"}


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
        payload.update(
            {"calls_reserved": 1, "tokens_reserved": 2048, "usd_reserved": "0.0100"}
        )
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
    assert covered >= 1


def test_unknown_event_type_is_rejected() -> None:
    supplied = "totally-not-a-registered-type"
    with pytest.raises(AuditSchemaError) as excinfo:
        AuditEvent(supplied, {"stage": "chat", "error_class": "X"})

    assert excinfo.value.category == "unknown_event_type"
    assert supplied not in str(excinfo.value)


def test_missing_required_payload_key_is_rejected() -> None:
    payload = _payload("migration.applied")
    del payload["to_revision"]
    with pytest.raises(AuditSchemaError) as excinfo:
        AuditEvent("migration.applied", payload)

    assert excinfo.value.category == "missing_required_key"
    assert excinfo.value.field_name == "to_revision"


def test_extra_payload_key_is_rejected() -> None:
    payload = _payload("provider.error")
    payload["undeclared_key_name"] = "value"
    with pytest.raises(AuditSchemaError) as excinfo:
        AuditEvent("provider.error", payload)

    assert excinfo.value.category == "extra_payload_key"
    assert "undeclared_key_name" not in str(excinfo.value)


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
