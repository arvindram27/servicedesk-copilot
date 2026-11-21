"""Provider interface types, validation and money bounds (B5).

No provider is selected in this gate. The protocol, the request/result types and
the validation helpers are the contracts later gates build on. ``ChatRequest``
carries no confirmation token, draft hash or internal identifier *field*; message
content is arbitrary user text, so nothing here claims the outbound body is
secret-free beyond the declared fields.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol

ROLES: frozenset[str] = frozenset({"system", "user", "assistant"})
MAX_TOKENS_LIMIT: int = 1_000_000
TOKEN_QUANTITY_LIMIT: int = 10**12
CALLS_LIMIT: int = 10**6
REQUEST_ID_MAX: int = 128
TEMPERATURE_MIN: float = 0.0
TEMPERATURE_MAX: float = 2.0
MONEY_MIN: Decimal = Decimal("1e-12")
MONEY_MAX: Decimal = Decimal("1e9")
MONEY_MAX_DIGITS: int = 30
MONEY_EXPONENT_MIN: int = -12
MONEY_EXPONENT_MAX: int = 9
USD_QUANTUM: Decimal = Decimal("0.000001")

Role = Literal["system", "user", "assistant"]


class ProviderError(RuntimeError):
    """Base class for provider failures."""


class ProviderUnavailable(ProviderError):
    """The provider cannot be used; carries a fixed reason code only."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def to_problem(self) -> object:
        from app.security import problem

        return problem(503, self.code)


class ProviderRequestError(ProviderError):
    """The request is malformed. Raised before any reservation or transport."""

    def __init__(self, code: str = "invalid_request") -> None:
        self.code = code
        super().__init__(code)


class ProviderCallError(ProviderError):
    """A call failed. ``usage`` is present only when the provider reported it."""

    def __init__(self, code: str, *, usage: Usage | None = None) -> None:
        self.code = code
        self.usage = usage
        super().__init__(code)


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class ChatRequest:
    messages: Sequence[Message]
    max_tokens: int
    temperature: float
    request_id: str
    scene_id: str | None = None


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None
    completion_tokens: int | None
    reported_cost: str | None = None


@dataclass(frozen=True)
class ChatResult:
    text: str
    provider: str
    model: str
    is_fake: bool
    usage: Usage
    latency_ms: int
    finish_reason: str | None


@dataclass(frozen=True)
class ProviderConfigStatus:
    present: bool
    valid: bool
    reason: str


@dataclass(frozen=True)
class Pricing:
    input_price_per_token: Decimal
    output_price_per_token: Decimal
    fixed_fee_per_request: Decimal


class Provider(Protocol):
    name: str

    def chat(self, request: ChatRequest) -> ChatResult: ...

    def config_status(self) -> ProviderConfigStatus: ...

    def bound_prompt_tokens(self, request: ChatRequest) -> int | None: ...


def _is_int(value: object) -> bool:
    return type(value) is int


def validate_token_quantity(value: object) -> int:
    """Validate a non-negative token/call quantity."""
    if not _is_int(value):
        raise ValueError("invalid_quantity")
    if value < 0 or value > TOKEN_QUANTITY_LIMIT:
        raise ValueError("invalid_quantity")
    return value


def validate_positive_int(value: object, *, maximum: int) -> int:
    if not _is_int(value) or value <= 0 or value > maximum:
        raise ValueError("invalid_quantity")
    return value


def parse_money(text: object, *, allow_zero: bool) -> Decimal:
    """Parse a quoted decimal string and enforce the money bounds."""
    if type(text) is not str:
        raise ValueError("invalid_money")
    stripped = text.strip()
    if not stripped or stripped != text:
        raise ValueError("invalid_money")
    try:
        value = Decimal(stripped)
    except InvalidOperation:
        raise ValueError("invalid_money") from None
    if not value.is_finite():
        raise ValueError("invalid_money")
    if value == 0:
        if allow_zero:
            return value
        raise ValueError("invalid_money")
    if value < 0:
        raise ValueError("invalid_money")
    if value < MONEY_MIN or value > MONEY_MAX:
        raise ValueError("invalid_money")
    sign, digits, exponent = value.as_tuple()
    if len(digits) > MONEY_MAX_DIGITS:
        raise ValueError("invalid_money")
    if not isinstance(exponent, int):
        raise ValueError("invalid_money")
    if exponent < MONEY_EXPONENT_MIN:
        raise ValueError("invalid_money")
    if value.adjusted() > MONEY_EXPONENT_MAX:
        raise ValueError("invalid_money")
    return value


def quantize_usd(value: Decimal) -> Decimal:
    """Round a money figure up to six decimal places."""
    from decimal import ROUND_CEILING, localcontext

    with localcontext() as context:
        context.prec = 50
        return value.quantize(USD_QUANTUM, rounding=ROUND_CEILING)


def validate_chat_request(request: ChatRequest) -> None:
    """Validate at the adapter boundary; raise before reservation or transport."""
    messages = request.messages
    if not messages:
        raise ProviderRequestError("messages_empty")
    for message in messages:
        if not isinstance(message, Message):
            raise ProviderRequestError("invalid_message")
        if message.role not in ROLES:
            raise ProviderRequestError("invalid_role")
        if type(message.content) is not str:
            raise ProviderRequestError("invalid_content")
    if not _is_int(request.max_tokens):
        raise ProviderRequestError("invalid_max_tokens")
    if not 1 <= request.max_tokens <= MAX_TOKENS_LIMIT:
        raise ProviderRequestError("invalid_max_tokens")
    temperature = request.temperature
    if type(temperature) not in (int, float):
        raise ProviderRequestError("invalid_temperature")
    if not math.isfinite(float(temperature)):
        raise ProviderRequestError("invalid_temperature")
    if not TEMPERATURE_MIN <= float(temperature) <= TEMPERATURE_MAX:
        raise ProviderRequestError("invalid_temperature")
    request_id = request.request_id
    if type(request_id) is not str or not request_id:
        raise ProviderRequestError("invalid_request_id")
    if len(request_id) > REQUEST_ID_MAX or not request_id.isascii():
        raise ProviderRequestError("invalid_request_id")
