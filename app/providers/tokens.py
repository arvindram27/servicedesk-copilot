"""Conservative prompt-token upper bound without a tokenizer."""

from __future__ import annotations

from app.providers.base import ChatRequest, validate_chat_request

MESSAGE_MARGIN: int = 8
REQUEST_MARGIN: int = 16


def bound_prompt_tokens(request: ChatRequest) -> int:
    """Return a UTF-8 byte upper bound plus fixed margins.

    A token covers at least one byte, so the token count cannot exceed the byte
    count. The margins are a fixed assumption, not a measured tokenizer count.
    """
    validate_chat_request(request)
    total = REQUEST_MARGIN
    for message in request.messages:
        total += len(message.content.encode("utf-8")) + MESSAGE_MARGIN
    return max(1, total)
