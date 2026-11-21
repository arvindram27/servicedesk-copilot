"""Budget ledger behaviour (B5): M1-M5 and the R-series ledger clauses."""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from app.providers.base import Pricing
from app.providers.budget import (
    BudgetError,
    BudgetExceeded,
    BudgetLedger,
    BudgetPolicy,
    LedgerCorrupt,
    SettlementConflict,
    UnknownAttemptFinal,
    compute_actual_usd,
    compute_reserve_usd,
    settle_unknown_event,
)

from db_support import PROJECT_ROOT

PRICING = Pricing(
    input_price_per_token=Decimal("0.000001"),
    output_price_per_token=Decimal("0.000002"),
    fixed_fee_per_request=Decimal("0.010000"),
)
POLICY = BudgetPolicy(calls_max=2, tokens_max=1000, usd_max=Decimal("1"))


def _ledger(tmp_path: Path, *, run_id: str = "run-1") -> BudgetLedger:
    return BudgetLedger(tmp_path / "budget" / f"{run_id}.json", run_id=run_id)


def _data(ledger: BudgetLedger) -> dict[str, object]:
    return json.loads(ledger.path.read_text(encoding="utf-8"))


def _reserve(ledger: BudgetLedger, *, request_id: str = "req-1", attempt: int = 1) -> None:
    ledger.reserve(
        request_id=request_id,
        attempt=attempt,
        bound_prompt_tokens=100,
        max_tokens=50,
        pricing=PRICING,
        policy=POLICY,
    )


def test_m1_reserve_covers_input_output_and_fee(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    _reserve(ledger)

    data = _data(ledger)
    assert data["usd_reserved"] == "0.010200"
    assert data["tokens_reserved"] == 150
    assert data["calls_reserved"] == 1


def test_m1_reservation_rounds_up_to_six_places() -> None:
    cheap = Pricing(Decimal("0.0000001"), Decimal("0"), Decimal("0"))

    assert compute_reserve_usd(3, 0, cheap) == Decimal("0.000001")


def test_m1_actual_rounds_up_to_six_places() -> None:
    cheap = Pricing(Decimal("0.0000001"), Decimal("0"), Decimal("0"))

    assert compute_actual_usd(3, 0, cheap) == Decimal("0.000001")


def test_m1_calls_cap_blocks_before_any_call(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _reserve(ledger, request_id="req-1", attempt=1)
    _reserve(ledger, request_id="req-1", attempt=2)

    with pytest.raises(BudgetExceeded) as excinfo:
        _reserve(ledger, request_id="req-1", attempt=3)

    assert excinfo.value.code == "calls_cap"


def test_m1_tokens_cap_and_usd_cap(tmp_path: Path) -> None:
    tokens_policy = BudgetPolicy(calls_max=5, tokens_max=100, usd_max=Decimal("10"))
    ledger = _ledger(tmp_path / "tokens")
    with pytest.raises(BudgetExceeded) as excinfo:
        ledger.reserve(
            request_id="req-1",
            attempt=1,
            bound_prompt_tokens=100,
            max_tokens=50,
            pricing=PRICING,
            policy=tokens_policy,
        )
    assert excinfo.value.code == "tokens_cap"

    usd_policy = BudgetPolicy(calls_max=5, tokens_max=10_000, usd_max=Decimal("0.005"))
    with pytest.raises(BudgetExceeded) as excinfo:
        ledger.reserve(
            request_id="req-2",
            attempt=1,
            bound_prompt_tokens=100,
            max_tokens=50,
            pricing=PRICING,
            policy=usd_policy,
        )
    assert excinfo.value.code == "usd_cap"


def test_r2_duplicate_reservation_is_refused(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _reserve(ledger, request_id="dup", attempt=1)

    with pytest.raises(BudgetExceeded) as excinfo:
        _reserve(ledger, request_id="dup", attempt=1)

    assert excinfo.value.code == "duplicate_reservation"
    assert _data(ledger)["calls_reserved"] == 1


def test_m2_settle_releases_only_the_unused_remainder(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _reserve(ledger)

    outcome = ledger.settle(
        request_id="req-1",
        attempt=1,
        prompt_tokens=100,
        completion_tokens=10,
        pricing=PRICING,
    )

    assert outcome.state == "settled"
    assert outcome.released_usd == Decimal("0.000080")
    data = _data(ledger)
    assert data["usd_reserved"] == "0.000000"
    assert data["usd_settled"] == "0.010120"
    assert data["tokens_settled"] == 110


def test_m2_identical_duplicate_settle_is_a_noop(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _reserve(ledger)
    first = ledger.settle(
        request_id="req-1", attempt=1, prompt_tokens=100, completion_tokens=10, pricing=PRICING
    )
    second = ledger.settle(
        request_id="req-1", attempt=1, prompt_tokens=100, completion_tokens=10, pricing=PRICING
    )

    assert first.state == "settled"
    assert second.state == "duplicate"


def test_m2_conflicting_settle_preserves_the_settled_state(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _reserve(ledger)
    ledger.settle(
        request_id="req-1", attempt=1, prompt_tokens=100, completion_tokens=10, pricing=PRICING
    )
    before = _data(ledger)

    with pytest.raises(SettlementConflict):
        ledger.settle(
            request_id="req-1",
            attempt=1,
            prompt_tokens=999,
            completion_tokens=999,
            pricing=PRICING,
        )

    after = _data(ledger)
    assert after["usd_settled"] == before["usd_settled"]
    assert after["attempts"][0]["conflict"]["actual_prompt_tokens"] == 999


def test_m5_unknown_is_final_and_rejects_later_settle(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _reserve(ledger)
    record = ledger.mark_unknown(request_id="req-1", attempt=1, reason="transport_error")

    assert record.state == "unknown"

    with pytest.raises(UnknownAttemptFinal):
        ledger.settle(
            request_id="req-1", attempt=1, prompt_tokens=1, completion_tokens=1, pricing=PRICING
        )

    data = _data(ledger)
    assert data["calls_reserved"] == 1
    assert data["calls_settled"] == 0


def test_m3_usage_exceeded_records_actuals_unclamped(tmp_path: Path) -> None:
    policy = BudgetPolicy(calls_max=5, tokens_max=15, usd_max=Decimal("1"))
    ledger = _ledger(tmp_path)
    ledger.reserve(
        request_id="req-1",
        attempt=1,
        bound_prompt_tokens=1,
        max_tokens=1,
        pricing=PRICING,
        policy=policy,
    )

    outcome = ledger.settle(
        request_id="req-1",
        attempt=1,
        prompt_tokens=10,
        completion_tokens=10,
        pricing=PRICING,
    )

    assert outcome.usage_exceeded is True
    assert _data(ledger)["tokens_settled"] == 20

    with pytest.raises(BudgetExceeded) as excinfo:
        ledger.reserve(
            request_id="req-2",
            attempt=1,
            bound_prompt_tokens=1,
            max_tokens=1,
            pricing=PRICING,
            policy=policy,
        )
    assert excinfo.value.code == "tokens_cap"


def test_m3_malformed_usage_is_rejected(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _reserve(ledger)

    with pytest.raises(BudgetError, match="^invalid_usage$"):
        ledger.settle(
            request_id="req-1",
            attempt=1,
            prompt_tokens=True,  # type: ignore[arg-type]
            completion_tokens=1,
            pricing=PRICING,
        )


def test_m4_sink_is_invoked_after_the_record_and_failure_changes_nothing(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _reserve(ledger)
    events = []

    def failing_sink(event: object) -> None:
        events.append(event)
        raise RuntimeError("sink failure")

    record = ledger.mark_unknown(
        request_id="req-1", attempt=1, reason="usage_unknown", audit_sink=failing_sink
    )

    assert record.state == "unknown"
    assert len(events) == 1
    assert _data(ledger)["calls_reserved"] == 1


def test_m4_none_sink_keeps_the_record(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _reserve(ledger)

    record = ledger.mark_unknown(request_id="req-1", attempt=1, reason="usage_unknown")

    assert record.state == "unknown"
    assert _data(ledger)["attempts"][0]["unknown_reason"] == "usage_unknown"


def test_r3_corrupt_ledger_fails_closed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text("{not json", encoding="utf-8")

    with pytest.raises(LedgerCorrupt):
        _reserve(ledger)

    assert ledger.path.read_text(encoding="utf-8") == "{not json"


def test_r9_settle_unknown_event_matches_the_b2_contract(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _reserve(ledger)
    record = ledger.mark_unknown(request_id="req-1", attempt=1, reason="transport_error")

    event = settle_unknown_event(record, "run-1")

    assert event.event_type == "budget.settle_unknown"
    assert set(event.payload) == {
        "run_id",
        "reason",
        "calls_reserved",
        "tokens_reserved",
        "usd_reserved",
    }
    assert isinstance(event.payload["calls_reserved"], int)
    assert isinstance(event.payload["tokens_reserved"], int)
    assert isinstance(event.payload["usd_reserved"], str)


def _reserved_ledger(tmp_path: Path) -> tuple[BudgetLedger, dict[str, object]]:
    ledger = _ledger(tmp_path)
    _reserve(ledger)
    return ledger, _data(ledger)


def test_e4_malformed_entry_fails_closed(tmp_path: Path) -> None:
    ledger, data = _reserved_ledger(tmp_path)
    del data["attempts"][0]["state"]
    tampered = json.dumps(data)
    ledger.path.write_text(tampered, encoding="utf-8")

    with pytest.raises(LedgerCorrupt):
        _reserve(ledger, request_id="req-2", attempt=1)

    assert ledger.path.read_text(encoding="utf-8") == tampered


def test_e4_duplicate_identity_fails_closed(tmp_path: Path) -> None:
    ledger, data = _reserved_ledger(tmp_path)
    data["attempts"].append(dict(data["attempts"][0]))
    tampered = json.dumps(data)
    ledger.path.write_text(tampered, encoding="utf-8")

    with pytest.raises(LedgerCorrupt):
        _reserve(ledger, request_id="req-2", attempt=1)

    assert ledger.path.read_text(encoding="utf-8") == tampered


def test_e4_totals_mismatch_fails_closed(tmp_path: Path) -> None:
    ledger, data = _reserved_ledger(tmp_path)
    data["calls_reserved"] = 5
    tampered = json.dumps(data)
    ledger.path.write_text(tampered, encoding="utf-8")

    with pytest.raises(LedgerCorrupt):
        _reserve(ledger, request_id="req-2", attempt=1)

    assert ledger.path.read_text(encoding="utf-8") == tampered


def test_f3_invalid_identity_is_rejected_before_any_write(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    with pytest.raises(BudgetExceeded) as excinfo:
        ledger.reserve(
            request_id="x" * 129,
            attempt=1,
            bound_prompt_tokens=100,
            max_tokens=50,
            pricing=PRICING,
            policy=POLICY,
        )
    assert excinfo.value.code == "invalid_identity"
    assert not ledger.path.exists()

    with pytest.raises(BudgetExceeded) as excinfo:
        ledger.reserve(
            request_id="r\u00e9q-1",
            attempt=1,
            bound_prompt_tokens=100,
            max_tokens=50,
            pricing=PRICING,
            policy=POLICY,
        )
    assert excinfo.value.code == "invalid_identity"
    assert not ledger.path.exists()

    with pytest.raises(BudgetExceeded) as excinfo:
        ledger.reserve(
            request_id="req-1",
            attempt=True,  # type: ignore[arg-type]
            bound_prompt_tokens=100,
            max_tokens=50,
            pricing=PRICING,
            policy=POLICY,
        )
    assert excinfo.value.code == "invalid_identity"
    assert not ledger.path.exists()


WORKER = """
import sys
from decimal import Decimal
from pathlib import Path

from app.providers.base import Pricing
from app.providers.budget import BudgetLedger, BudgetPolicy

ledger = BudgetLedger(Path(sys.argv[1]), run_id=sys.argv[2], lock_timeout_s=10.0)
pricing = Pricing(Decimal("0.000001"), Decimal("0.000002"), Decimal("0"))
policy = BudgetPolicy(calls_max=10, tokens_max=10000, usd_max=Decimal("1"))
ledger.reserve(
    request_id=sys.argv[3],
    attempt=1,
    bound_prompt_tokens=10,
    max_tokens=10,
    pricing=pricing,
    policy=policy,
)
"""


def test_r2_two_process_reservations_are_not_lost_or_duplicated(tmp_path: Path) -> None:
    ledger_path = tmp_path / "budget" / "run-1.json"
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", WORKER, str(ledger_path), "run-1", f"proc-{index}"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for index in (1, 2)
    ]
    for process in processes:
        _, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr

    data = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert data["calls_reserved"] == 2
    assert sorted(entry["request_id"] for entry in data["attempts"]) == ["proc-1", "proc-2"]
