"""Citation provenance and span integrity (Gate 1)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from app.kb.citation import CitationError, build_citation, render_answer, validate_citation
from app.kb.manifest import Article, ArticleMeta
from app.kb.retrieval import Chunk, chunk_article


def _article(text: str) -> Article:
    meta = ArticleMeta(
        article_id="PRN-001",
        version=1,
        title="Printer",
        category="hardware",
        path="data/kb/printing/PRN-001.md",
        sha256="0" * 64,
        bytes=len(text.encode("utf-8")),
    )
    return Article(meta=meta, text=text)


def _chunks(text: str) -> tuple[Chunk, ...]:
    return chunk_article(_article(text))


def test_valid_citation_round_trips() -> None:
    chunks = _chunks("# Printer\n\n## Connection\n\nCheck the cable.\n")

    citation = build_citation(chunks[0])

    assert validate_citation(citation, chunks) == chunks[0]
    assert citation.article_id == "PRN-001"
    assert citation.version == 1
    assert citation.section == "Connection"
    assert citation.span == "Check the cable."
    assert citation.span in render_answer(citation)


@pytest.mark.parametrize(
    "change",
    [
        {"article_id": "PRN-999"},
        {"version": 2},
        {"section": "Other"},
        {"start": 0},
        {"end": 0},
        {"span": "tampered"},
    ],
)
def test_citation_mismatch_rejected(change: dict[str, object]) -> None:
    chunks = _chunks("# Printer\n\n## Connection\n\nCheck the cable.\n")
    citation = replace(build_citation(chunks[0]), **change)

    with pytest.raises(CitationError):
        validate_citation(citation, chunks)


def test_crlf_and_non_ascii_offsets_are_character_offsets() -> None:
    text = "# Printer\r\n\r\n## Café\r\n\r\nCafé printer offline.\r\n"

    chunks = _chunks(text)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.section == "Café"
    assert chunk.text == "Café printer offline."
    assert text[chunk.start : chunk.end] == chunk.text
    assert chunk.start == text.index("Café printer offline.")
    assert len(text[: chunk.start].encode("utf-8")) > chunk.start


def test_span_must_be_exact_after_normalisation_attempt() -> None:
    chunks = _chunks("# Printer\n\n## Connection\n\nCheck the cable.\n")
    citation = replace(build_citation(chunks[0]), span="Check the  cable.")

    with pytest.raises(CitationError):
        validate_citation(citation, chunks)
