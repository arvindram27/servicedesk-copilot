"""Embeddings protocol (B5): interface only."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class Embedder(Protocol):
    """Minimal embedding surface: identity, dimension and batch embedding."""

    name: str
    dimension: int

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...
