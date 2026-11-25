"""Deterministic lexical retrieval over a verified KB snapshot (Gate 1).

No embedding model and no external service. Sections are scored by IDF-weighted
token overlap with a stable tie break; a zero-overlap question is never answered,
even when the configured threshold is zero.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Final

from app.kb.manifest import Article, KbSnapshot

_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")
_SECTION_RE: Final[re.Pattern[str]] = re.compile(r"^## (.+)$", re.MULTILINE)
_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "not",
        "of",
        "on",
        "or",
        "our",
        "should",
        "so",
        "that",
        "the",
        "this",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "who",
        "why",
        "with",
        "would",
        "you",
        "your",
    }
)


@dataclass(frozen=True)
class Chunk:
    """One article section with exact character offsets in the article text."""

    article_id: str
    version: int
    title: str
    category: str
    section: str
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class RetrievalResult:
    """One scored chunk."""

    chunk: Chunk
    score: float
    matched_terms: tuple[str, ...]


def tokenize(text: str) -> tuple[str, ...]:
    """Lowercase alphanumeric terms, stopwords and single characters removed."""
    return tuple(
        token
        for token in _TOKEN_RE.findall(text.lower())
        if token not in _STOPWORDS and len(token) > 1
    )


def chunk_article(article: Article) -> tuple[Chunk, ...]:
    """Split one article into section chunks with exact spans."""
    text = article.text
    matches = list(_SECTION_RE.finditer(text))
    chunks: list[Chunk] = []
    for index, match in enumerate(matches):
        heading_end = match.end()
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        raw = text[heading_end:next_start]
        leading = len(raw) - len(raw.lstrip("\r\n"))
        body = raw[leading:].rstrip("\r\n")
        if not body:
            continue
        start = heading_end + leading
        chunks.append(
            Chunk(
                article_id=article.meta.article_id,
                version=article.meta.version,
                title=article.meta.title,
                category=article.meta.category,
                section=match.group(1).strip(),
                text=body,
                start=start,
                end=start + len(body),
            )
        )
    return tuple(chunks)


def index_articles(snapshot: KbSnapshot) -> tuple[Chunk, ...]:
    """Build the section index from a verified snapshot."""
    chunks: list[Chunk] = []
    for article in snapshot.articles:
        chunks.extend(chunk_article(article))
    return tuple(chunks)


def _idf(chunks: tuple[Chunk, ...]) -> dict[str, float]:
    document_frequency: dict[str, int] = {}
    for chunk in chunks:
        for term in set(tokenize(chunk.text)):
            document_frequency[term] = document_frequency.get(term, 0) + 1
    total = len(chunks)
    return {
        term: math.log((total + 1) / (count + 1)) + 1.0
        for term, count in document_frequency.items()
    }


def retrieve(
    chunks: tuple[Chunk, ...],
    question: str,
    *,
    top_k: int,
) -> tuple[RetrievalResult, ...]:
    """Score every chunk and return the top-k in stable order."""
    query_terms = set(tokenize(question))
    idf = _idf(chunks)
    scored: list[RetrievalResult] = []
    for chunk in chunks:
        chunk_terms = set(tokenize(chunk.text))
        matched = tuple(sorted(query_terms & chunk_terms))
        if matched and chunk_terms:
            raw = sum(idf[term] for term in matched)
            score = raw / math.sqrt(len(chunk_terms))
        else:
            score = 0.0
        scored.append(RetrievalResult(chunk=chunk, score=score, matched_terms=matched))
    scored.sort(key=lambda result: (-result.score, result.chunk.article_id, result.chunk.section))
    return tuple(scored[:top_k])


def select_supported(
    results: tuple[RetrievalResult, ...],
    min_score: float,
) -> RetrievalResult | None:
    """Return the best result only when it has positive lexical evidence."""
    if not results:
        return None
    best = results[0]
    if best.score <= 0.0 or best.score < min_score:
        return None
    return best
