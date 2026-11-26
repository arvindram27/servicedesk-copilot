"""Deterministic scripted fake provider (B5).

Scenes live in this module. Lookup is exact on ``scene_id``; an unknown scene or
``scene_id=None`` raises rather than improvising. No HTTP client is imported, so
no network access is possible by construction.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.providers.base import (
    ChatRequest,
    ChatResult,
    ProviderConfigStatus,
    Usage,
    validate_chat_request,
)


@dataclass(frozen=True)
class Scene:
    text: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str | None


SCENES: dict[str, Scene] = {
    "greeting": Scene(
        text="Hello. This is a scripted fake reply.",
        prompt_tokens=12,
        completion_tokens=9,
        finish_reason="stop",
    ),
    "kb_answer": Scene(
        text="Reset the printer queue and retry the job.",
        prompt_tokens=48,
        completion_tokens=14,
        finish_reason="stop",
    ),
    "refusal": Scene(
        text="I cannot help with that request.",
        prompt_tokens=21,
        completion_tokens=8,
        finish_reason="stop",
    ),
    "ticket_draft": Scene(
        text=(
            '{"title": "Printer offline", "description": '
            '"The printer is offline.", "category": "hardware"}'
        ),
        prompt_tokens=64,
        completion_tokens=24,
        finish_reason="stop",
    ),
    "ticket_draft_invalid": Scene(
        text="not a JSON draft",
        prompt_tokens=64,
        completion_tokens=8,
        finish_reason="stop",
    ),
}


class FakeSceneMissing(LookupError):
    """No scripted scene matches the requested id."""


class FakeProvider:
    """Scripted, deterministic, offline provider."""

    name = "fake"

    def chat(self, request: ChatRequest) -> ChatResult:
        validate_chat_request(request)
        scene = self._scene(request)
        return ChatResult(
            text=scene.text,
            provider=self.name,
            model="scripted-v1",
            is_fake=True,
            usage=Usage(
                prompt_tokens=scene.prompt_tokens,
                completion_tokens=scene.completion_tokens,
                reported_cost=None,
            ),
            latency_ms=0,
            finish_reason=scene.finish_reason,
        )

    def config_status(self) -> ProviderConfigStatus:
        return ProviderConfigStatus(present=True, valid=True, reason="fake_always_available")

    def bound_prompt_tokens(self, request: ChatRequest) -> int:
        validate_chat_request(request)
        return self._scene(request).prompt_tokens

    def _scene(self, request: ChatRequest) -> Scene:
        if request.scene_id is None:
            raise FakeSceneMissing("no scene id was supplied")
        try:
            return SCENES[request.scene_id]
        except KeyError:
            raise FakeSceneMissing("unknown scene id") from None
