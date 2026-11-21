"""Budget ledger, policy and approval file (B5).

Money is ``Decimal`` only, computed under ``localcontext(prec=50)`` and quantized
up to six decimal places. The admission invariant is evaluated entirely under
the exclusive OS file lock; ``reserved`` and ``unknown`` attempts count as
outstanding forever, and no reader reclassifies another process's attempt.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Final

from app.audit.schema import AuditEvent
from app.providers.base import (
    CALLS_LIMIT,
    REQUEST_ID_MAX,
    TOKEN_QUANTITY_LIMIT,
    Pricing,
    ProviderUnavailable,
    parse_money,
    quantize_usd,
    validate_positive_int,
    validate_token_quantity,
)
from app.security import problem

try:  # Windows only.
    import msvcrt
except ImportError:  # pragma: no cover - POSIX hosts
    msvcrt = None

try:  # POSIX only.
    import fcntl
except ImportError:  # pragma: no cover - Windows hosts
    fcntl = None

LEDGER_FORMAT: Final[int] = 1
APPROVAL_MAX_BYTES: Final[int] = 64 * 1024
LEDGER_MAX_BYTES: Final[int] = 1_000_000
LOCK_TIMEOUT_S: Final[float] = 5.0
LEDGER_AMOUNT_ZERO: Final[str] = "0"


class BudgetError(RuntimeError):
    """Base class for ledger and budget failures."""


class BudgetExceeded(BudgetError):
    """A cap, the lock, or a duplicate reservation refused the operation."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def to_problem(self) -> object:
        return problem(503, self.code)


class LedgerCorrupt(BudgetError):
    """The ledger cannot be read or is inconsistent; never reset to zero."""


class SettlementConflict(BudgetError):
    """A settled attempt was presented with different usage."""


class UnknownAttemptFinal(BudgetError):
    """An unknown attempt cannot be settled later."""


@dataclass(frozen=True)
class RunApproval:
    run_id: str
    provider: str
    model: str
    base_url: str
    label: str
    calls_max: int
    tokens_max: int
    usd_max: Decimal


@dataclass(frozen=True)
class BudgetPolicy:
    calls_max: int
    tokens_max: int
    usd_max: Decimal


@dataclass(frozen=True)
class AttemptRecord:
    request_id: str
    attempt: int
    calls: int
    tokens: int
    usd: Decimal
    state: str
    actual_prompt_tokens: int | None = None
    actual_completion_tokens: int | None = None
    actual_usd: Decimal | None = None
    usage_exceeded: bool = False
    conflict: dict[str, Any] | None = None
    unknown_reason: str | None = None


@dataclass(frozen=True)
class SettlementOutcome:
    state: str
    actual_usd: Decimal
    released_usd: Decimal
    usage_exceeded: bool


def _approval_error(code: str) -> ProviderUnavailable:
    return ProviderUnavailable(code)


def load_approval(runtime_dir: Path, run_id: str) -> RunApproval:
    """Load and strictly validate the human-written run approval file."""
    path = Path(runtime_dir) / "runs" / f"{run_id}.json"
    try:
        if not path.is_file():
            raise _approval_error("run_not_approved")
        if path.stat().st_size > APPROVAL_MAX_BYTES:
            raise _approval_error("run_not_approved")
        raw = path.read_text(encoding="utf-8")
    except OSError:
        raise _approval_error("run_not_approved") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise _approval_error("run_not_approved") from None
    if type(data) is not dict:
        raise _approval_error("run_not_approved")

    expected = {
        "format",
        "run_id",
        "provider",
        "model",
        "base_url",
        "label",
        "calls_max",
        "tokens_max",
        "usd_max",
    }
    if set(data) != expected:
        raise _approval_error("run_not_approved")
    if type(data["format"]) is not int or data["format"] != 1:
        raise _approval_error("run_not_approved")
    if data["run_id"] != run_id:
        raise _approval_error("run_not_approved")
    for key in ("provider", "model", "base_url", "label"):
        if type(data[key]) is not str or not data[key]:
            raise _approval_error("run_not_approved")
    try:
        calls_max = validate_positive_int(data["calls_max"], maximum=CALLS_LIMIT)
        tokens_max = validate_positive_int(data["tokens_max"], maximum=TOKEN_QUANTITY_LIMIT)
        usd_max = parse_money(data["usd_max"], allow_zero=False)
    except ValueError:
        raise _approval_error("run_not_approved") from None

    return RunApproval(
        run_id=run_id,
        provider=data["provider"],
        model=data["model"],
        base_url=data["base_url"],
        label=data["label"],
        calls_max=calls_max,
        tokens_max=tokens_max,
        usd_max=usd_max,
    )


def compute_reserve_usd(bound_prompt_tokens: int, max_tokens: int, pricing: Pricing) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        total = (
            Decimal(bound_prompt_tokens) * pricing.input_price_per_token
            + Decimal(max_tokens) * pricing.output_price_per_token
            + pricing.fixed_fee_per_request
        )
    return quantize_usd(total)


def compute_actual_usd(prompt_tokens: int, completion_tokens: int, pricing: Pricing) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        total = (
            Decimal(prompt_tokens) * pricing.input_price_per_token
            + Decimal(completion_tokens) * pricing.output_price_per_token
            + pricing.fixed_fee_per_request
        )
    return quantize_usd(total)


def validate_usage_tokens(prompt: object, completion: object) -> tuple[int, int]:
    """Validate reported usage; raise ValueError when malformed."""
    return (validate_token_quantity(prompt), validate_token_quantity(completion))


def settle_unknown_event(record: AttemptRecord, run_id: str) -> AuditEvent:
    """B2-conforming ``budget.settle_unknown`` event for one attempt."""
    payload: dict[str, Any] = {
        "run_id": run_id,
        "reason": record.unknown_reason or "unknown",
        "calls_reserved": record.calls,
        "tokens_reserved": record.tokens,
        "usd_reserved": format(record.usd, "f"),
    }
    return AuditEvent("budget.settle_unknown", payload)


class _LockBusy(Exception):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """JSON object hook that refuses duplicate keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ledger_duplicate_key")
        result[key] = value
    return result


def _try_lock(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if msvcrt is not None:
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise _LockBusy(str(exc)) from exc
        return
    if fcntl is not None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise _LockBusy(str(exc)) from exc
        return
    raise BudgetError("no_os_lock_primitive")


def _unlock(descriptor: int) -> None:
    if msvcrt is not None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


class BudgetLedger:
    """One ledger file per run, guarded by an exclusive OS file lock."""

    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        lock_timeout_s: float = LOCK_TIMEOUT_S,
    ) -> None:
        self._path = Path(path)
        self._run_id = run_id
        self._lock_path = self._path.with_name(f"{run_id}.lock")
        self._lock_timeout_s = lock_timeout_s
        self._thread_lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            deadline = time.monotonic() + self._lock_timeout_s
            while True:
                try:
                    _try_lock(descriptor)
                    break
                except _LockBusy:
                    if time.monotonic() >= deadline:
                        raise BudgetExceeded("lock_busy") from None
                    time.sleep(0.01)
            try:
                yield
            finally:
                _unlock(descriptor)
        finally:
            os.close(descriptor)

    def _empty(self) -> dict[str, Any]:
        return {
            "format": LEDGER_FORMAT,
            "run_id": self._run_id,
            "calls_reserved": 0,
            "tokens_reserved": 0,
            "usd_reserved": LEDGER_AMOUNT_ZERO,
            "calls_settled": 0,
            "tokens_settled": 0,
            "usd_settled": LEDGER_AMOUNT_ZERO,
            "attempts": [],
        }

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return self._empty()
        try:
            if self._path.stat().st_size > LEDGER_MAX_BYTES:
                raise LedgerCorrupt("ledger_too_large")
            data = json.loads(
                self._path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except LedgerCorrupt:
            raise
        except OSError:
            raise LedgerCorrupt("ledger_unreadable") from None
        except ValueError:
            raise LedgerCorrupt("ledger_unreadable") from None
        if type(data) is not dict:
            raise LedgerCorrupt("ledger_invalid")
        if type(data.get("format")) is not int or data["format"] != LEDGER_FORMAT:
            raise LedgerCorrupt("ledger_invalid")
        if data.get("run_id") != self._run_id:
            raise LedgerCorrupt("ledger_invalid")
        attempts = data.get("attempts")
        if type(attempts) is not list:
            raise LedgerCorrupt("ledger_invalid")
        for key in ("calls_reserved", "tokens_reserved", "calls_settled", "tokens_settled"):
            if type(data.get(key)) is not int or data[key] < 0:
                raise LedgerCorrupt("ledger_invalid")
        for key in ("usd_reserved", "usd_settled"):
            self._load_money(data.get(key))
        try:
            self._validate_attempts(attempts)
            self._validate_totals(data, attempts)
        except LedgerCorrupt:
            raise
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise LedgerCorrupt("ledger_invalid") from None
        return data

    @staticmethod
    def _load_money(value: object) -> Decimal:
        try:
            return parse_money(value, allow_zero=True)
        except ValueError:
            raise LedgerCorrupt("ledger_invalid") from None

    @staticmethod
    def _validate_attempts(attempts: list[Any]) -> None:
        identities: set[tuple[str, int]] = set()
        for entry in attempts:
            if type(entry) is not dict:
                raise LedgerCorrupt("ledger_invalid")
            request_id = entry.get("request_id")
            if (
                type(request_id) is not str
                or not request_id
                or not request_id.isascii()
                or len(request_id) > REQUEST_ID_MAX
            ):
                raise LedgerCorrupt("ledger_invalid")
            attempt = entry.get("attempt")
            if type(attempt) is not int or attempt <= 0:
                raise LedgerCorrupt("ledger_invalid")
            identity = (request_id, attempt)
            if identity in identities:
                raise LedgerCorrupt("ledger_duplicate_identity")
            identities.add(identity)

            calls = entry.get("calls")
            if type(calls) is not int or calls != 1:
                raise LedgerCorrupt("ledger_invalid")
            try:
                validate_token_quantity(entry.get("tokens"))
            except ValueError:
                raise LedgerCorrupt("ledger_invalid") from None
            BudgetLedger._load_money(entry.get("usd"))

            state = entry.get("state")
            if type(state) is not str or state not in {"reserved", "settled", "unknown"}:
                raise LedgerCorrupt("ledger_invalid")
            if state == "settled":
                for key in ("actual_prompt_tokens", "actual_completion_tokens"):
                    try:
                        validate_token_quantity(entry.get(key))
                    except ValueError:
                        raise LedgerCorrupt("ledger_invalid") from None
                BudgetLedger._load_money(entry.get("actual_usd"))
            if state == "unknown" and type(entry.get("unknown_reason")) is not str:
                raise LedgerCorrupt("ledger_invalid")

    @staticmethod
    def _validate_totals(data: dict[str, Any], attempts: list[Any]) -> None:
        reserved_calls = 0
        reserved_tokens = 0
        reserved_usd = Decimal("0")
        settled_calls = 0
        settled_tokens = 0
        settled_usd = Decimal("0")
        with localcontext() as context:
            context.prec = 50
            for entry in attempts:
                if entry["state"] in {"reserved", "unknown"}:
                    reserved_calls += 1
                    reserved_tokens += entry["tokens"]
                    reserved_usd += Decimal(entry["usd"])
                else:
                    settled_calls += 1
                    settled_tokens += (
                        entry["actual_prompt_tokens"] + entry["actual_completion_tokens"]
                    )
                    settled_usd += Decimal(entry["actual_usd"])

        def _money(value: str) -> Decimal:
            return Decimal(value)

        if reserved_calls != data["calls_reserved"]:
            raise LedgerCorrupt("ledger_totals_mismatch")
        if reserved_tokens != data["tokens_reserved"]:
            raise LedgerCorrupt("ledger_totals_mismatch")
        if reserved_usd != _money(data["usd_reserved"]):
            raise LedgerCorrupt("ledger_totals_mismatch")
        if settled_calls != data["calls_settled"]:
            raise LedgerCorrupt("ledger_totals_mismatch")
        if settled_tokens != data["tokens_settled"]:
            raise LedgerCorrupt("ledger_totals_mismatch")
        if settled_usd != _money(data["usd_settled"]):
            raise LedgerCorrupt("ledger_totals_mismatch")

    def _store(self, data: dict[str, Any]) -> None:
        text = json.dumps(data, sort_keys=True)
        temporary = self._path.with_name(self._path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._path)

    @staticmethod
    def _find(data: dict[str, Any], request_id: str, attempt: int) -> dict[str, Any] | None:
        for entry in data["attempts"]:
            if entry.get("request_id") == request_id and entry.get("attempt") == attempt:
                return entry
        return None

    def reserve(
        self,
        *,
        request_id: str,
        attempt: int,
        bound_prompt_tokens: int,
        max_tokens: int,
        pricing: Pricing,
        policy: BudgetPolicy,
    ) -> AttemptRecord:
        """Reserve the worst case, or raise ``BudgetExceeded`` before any call."""
        if (
            type(request_id) is not str
            or not request_id
            or not request_id.isascii()
            or len(request_id) > REQUEST_ID_MAX
        ):
            raise BudgetExceeded("invalid_identity")
        if type(attempt) is not int or attempt <= 0:
            raise BudgetExceeded("invalid_identity")
        tokens = validate_token_quantity(bound_prompt_tokens) + max_tokens
        usd = compute_reserve_usd(bound_prompt_tokens, max_tokens, pricing)
        with self._thread_lock, self._exclusive():
            data = self._load()
            if self._find(data, request_id, attempt) is not None:
                raise BudgetExceeded("duplicate_reservation")
            reserved_calls = data["calls_reserved"]
            reserved_tokens = data["tokens_reserved"]
            reserved_usd = Decimal(data["usd_reserved"])
            if data["calls_settled"] + reserved_calls + 1 > policy.calls_max:
                raise BudgetExceeded("calls_cap")
            if data["tokens_settled"] + reserved_tokens + tokens > policy.tokens_max:
                raise BudgetExceeded("tokens_cap")
            if Decimal(data["usd_settled"]) + reserved_usd + usd > policy.usd_max:
                raise BudgetExceeded("usd_cap")
            record = {
                "request_id": request_id,
                "attempt": attempt,
                "calls": 1,
                "tokens": tokens,
                "usd": format(usd, "f"),
                "state": "reserved",
                "actual_prompt_tokens": None,
                "actual_completion_tokens": None,
                "actual_usd": None,
                "usage_exceeded": False,
                "conflict": None,
                "unknown_reason": None,
            }
            data["attempts"].append(record)
            data["calls_reserved"] = reserved_calls + 1
            data["tokens_reserved"] = reserved_tokens + tokens
            data["usd_reserved"] = format(reserved_usd + usd, "f")
            self._store(data)
        return AttemptRecord(
            request_id=request_id,
            attempt=attempt,
            calls=1,
            tokens=tokens,
            usd=usd,
            state="reserved",
        )

    def settle(
        self,
        *,
        request_id: str,
        attempt: int,
        prompt_tokens: int,
        completion_tokens: int,
        pricing: Pricing,
    ) -> SettlementOutcome:
        """Settle one attempt against validated provider-reported usage."""
        try:
            prompt_value = validate_token_quantity(prompt_tokens)
            completion_value = validate_token_quantity(completion_tokens)
        except ValueError:
            raise BudgetError("invalid_usage") from None
        actual_usd = compute_actual_usd(prompt_value, completion_value, pricing)
        with self._thread_lock, self._exclusive():
            data = self._load()
            entry = self._find(data, request_id, attempt)
            if entry is None:
                raise BudgetError("unknown_attempt")
            state = entry.get("state")
            if state == "unknown":
                raise UnknownAttemptFinal("unknown_is_final")
            if state == "settled":
                same = (
                    entry.get("actual_prompt_tokens") == prompt_value
                    and entry.get("actual_completion_tokens") == completion_value
                )
                if same:
                    return SettlementOutcome(
                        state="duplicate",
                        actual_usd=Decimal(entry["actual_usd"]),
                        released_usd=Decimal("0"),
                        usage_exceeded=bool(entry.get("usage_exceeded")),
                    )
                entry["conflict"] = {
                    "actual_prompt_tokens": prompt_value,
                    "actual_completion_tokens": completion_value,
                    "actual_usd": format(actual_usd, "f"),
                }
                self._store(data)
                raise SettlementConflict("settlement_conflict")
            if state != "reserved":
                raise LedgerCorrupt("ledger_invalid")

            reserved_usd = Decimal(entry["usd"])
            actual_tokens = prompt_value + completion_value
            exceeded = actual_tokens > entry["tokens"] or actual_usd > reserved_usd
            entry["state"] = "settled"
            entry["actual_prompt_tokens"] = prompt_value
            entry["actual_completion_tokens"] = completion_value
            entry["actual_usd"] = format(actual_usd, "f")
            entry["usage_exceeded"] = exceeded
            data["calls_reserved"] = max(0, data["calls_reserved"] - entry["calls"])
            data["tokens_reserved"] = max(0, data["tokens_reserved"] - entry["tokens"])
            data["usd_reserved"] = format(
                quantize_usd(max(Decimal("0"), Decimal(data["usd_reserved"]) - reserved_usd)),
                "f",
            )
            data["calls_settled"] += 1
            data["tokens_settled"] += actual_tokens
            data["usd_settled"] = format(Decimal(data["usd_settled"]) + actual_usd, "f")
            self._store(data)
        return SettlementOutcome(
            state="settled",
            actual_usd=actual_usd,
            released_usd=max(Decimal("0"), reserved_usd - actual_usd),
            usage_exceeded=exceeded,
        )

    def mark_unknown(
        self,
        *,
        request_id: str,
        attempt: int,
        reason: str,
        audit_sink: Callable[[AuditEvent], None] | None = None,
    ) -> AttemptRecord:
        """Mark an attempt unknown: the reservation stands; notify after the lock."""
        with self._thread_lock, self._exclusive():
            data = self._load()
            entry = self._find(data, request_id, attempt)
            if entry is None:
                raise BudgetError("unknown_attempt")
            if entry.get("state") == "settled":
                raise BudgetError("already_settled")
            if entry.get("state") == "unknown":
                record = self._record(entry)
                event = settle_unknown_event(record, self._run_id)
            else:
                entry["state"] = "unknown"
                entry["unknown_reason"] = reason
                self._store(data)
                record = self._record(entry)
                event = settle_unknown_event(record, self._run_id)
        if audit_sink is not None:
            try:
                audit_sink(event)
            except Exception:  # noqa: BLE001 - notification must not change state
                pass
        return record

    @staticmethod
    def _record(entry: dict[str, Any]) -> AttemptRecord:
        return AttemptRecord(
            request_id=entry["request_id"],
            attempt=entry["attempt"],
            calls=entry["calls"],
            tokens=entry["tokens"],
            usd=Decimal(entry["usd"]),
            state=entry["state"],
            actual_prompt_tokens=entry.get("actual_prompt_tokens"),
            actual_completion_tokens=entry.get("actual_completion_tokens"),
            actual_usd=(None if entry.get("actual_usd") is None else Decimal(entry["actual_usd"])),
            usage_exceeded=bool(entry.get("usage_exceeded")),
            conflict=entry.get("conflict"),
            unknown_reason=entry.get("unknown_reason"),
        )
