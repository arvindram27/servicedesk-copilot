"""Citation provenance for the extractive answer (Gate 1).

A citation is accepted only when its article identity, version, section and
character span match a chunk of the same verified snapshot. Integrity is not a
claim that the cited text supports the answer; support stays human-scored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.kb.retrieval import Chunk

REFUSAL_REPLY: Final[str] = (
    "I do not have a supported article for that question. "
    "Please raise a ticket or contact the service desk."
)


class CitationError(RuntimeError):
    """A citation does not match the verified snapshot."""


@dataclass(frozen=True)
class Citation:
    """One checked citation with its exact span."""

    article_id: str
    version: int
    span: str
    section: str
    start: int
    end: int


def build_citation(chunk: Chunk) -> Citation:
    """Build a citation from a retrieved chunk."""
    return Citation(
        article_id=chunk.article_id,
        version=chunk.version,
        span=chunk.text,
        section=chunk.section,
        start=chunk.start,
        end=chunk.end,
    )


def validate_citation(citation: Citation, chunks: tuple[Chunk, ...]) -> Chunk:
    """Return the matching chunk, or raise ``CitationError``."""
    for chunk in chunks:
        if (
            chunk.article_id == citation.article_id
            and chunk.version == citation.version
            and chunk.section == citation.section
            and chunk.start == citation.start
            and chunk.end == citation.end
            and chunk.text == citation.span
        ):
            return chunk
    raise CitationError("citation_invalid")


def render_answer(citation: Citation) -> str:
    """Fixed framing around the verbatim excerpt."""
    return (
        f"According to {citation.article_id} version {citation.version}, "
        f"section {citation.section}:\n\n{citation.span}"
    )
