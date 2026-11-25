"""Frozen knowledge-base manifest loading and byte verification (Gate 1).

The manifest is the only source of article identity, version and content for the
read path. A snapshot is loaded once and fully verified: every entry must exist,
match its recorded byte length and SHA-256, decode as UTF-8 and stay inside the
knowledge-base root. Any failure rejects the whole load; no partial KB is used.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

MANIFEST_FORMAT: Final[int] = 1
_KIND_ARTICLE: Final[str] = "kb_article"
_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "path",
    "sha256",
    "bytes",
    "kind",
    "frozen_at",
    "article_id",
    "version",
    "title",
    "category",
)
_HEX_DIGITS: Final[frozenset[str]] = frozenset("0123456789abcdef")


class KbError(RuntimeError):
    """The knowledge base is missing or invalid; the ask path fails closed."""


@dataclass(frozen=True)
class ArticleMeta:
    """One verified manifest entry."""

    article_id: str
    version: int
    title: str
    category: str
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class Article:
    """One verified article and the exact text that was hashed."""

    meta: ArticleMeta
    text: str


@dataclass(frozen=True)
class KbSnapshot:
    """Immutable, fully verified knowledge-base snapshot."""

    articles: tuple[Article, ...]


def _is_sha256(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(char in _HEX_DIGITS for char in value)


def _require_str(entry: dict[str, Any], key: str) -> str:
    value = entry.get(key)
    if type(value) is not str or not value.strip():
        raise KbError("kb_invalid")
    return value


def _require_int(entry: dict[str, Any], key: str) -> int:
    value = entry.get(key)
    if type(value) is not int or value < 0:
        raise KbError("kb_invalid")
    return value


def _entry_meta(entry: object, root: Path, kb_root: Path) -> ArticleMeta:
    if type(entry) is not dict:
        raise KbError("kb_invalid")
    for field_name in _REQUIRED_FIELDS:
        if field_name not in entry:
            raise KbError("kb_invalid")
    if entry.get("kind") != _KIND_ARTICLE:
        raise KbError("kb_invalid")
    relative = _require_str(entry, "path")
    if relative.startswith("/") or "\\" in relative or ".." in relative.split("/"):
        raise KbError("kb_invalid")
    sha256 = entry.get("sha256")
    if not _is_sha256(sha256):
        raise KbError("kb_invalid")
    length = _require_int(entry, "bytes")
    article_id = _require_str(entry, "article_id")
    if not article_id.isascii() or not article_id.replace("-", "").isalnum():
        raise KbError("kb_invalid")
    version = _require_int(entry, "version")
    if version < 1:
        raise KbError("kb_invalid")
    title = _require_str(entry, "title")
    category = _require_str(entry, "category")
    _require_str(entry, "frozen_at")
    target = (root / relative).resolve()
    if not target.is_relative_to(kb_root):
        raise KbError("kb_invalid")
    return ArticleMeta(
        article_id=article_id,
        version=version,
        title=title,
        category=category,
        path=relative,
        sha256=sha256,
        bytes=length,
    )


def load_snapshot(manifest_path: Path, *, root: Path, kb_root: Path) -> KbSnapshot:
    """Load and verify every article in the manifest, or raise ``KbError``."""
    try:
        raw = manifest_path.read_bytes()
    except OSError:
        raise KbError("kb_unavailable") from None
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise KbError("kb_invalid") from None
    if type(document) is not dict or document.get("format") != MANIFEST_FORMAT:
        raise KbError("kb_invalid")
    entries = document.get("articles")
    if type(entries) is not list or not entries:
        raise KbError("kb_invalid")

    metas: list[ArticleMeta] = []
    seen_ids: set[tuple[str, int]] = set()
    seen_paths: set[str] = set()
    for entry in entries:
        meta = _entry_meta(entry, root, kb_root)
        identity = (meta.article_id, meta.version)
        if identity in seen_ids or meta.path in seen_paths:
            raise KbError("kb_invalid")
        seen_ids.add(identity)
        seen_paths.add(meta.path)
        metas.append(meta)

    articles: list[Article] = []
    for meta in metas:
        target = (root / meta.path).resolve()
        try:
            data = target.read_bytes()
        except OSError:
            raise KbError("kb_unavailable") from None
        if len(data) != meta.bytes or hashlib.sha256(data).hexdigest() != meta.sha256:
            raise KbError("kb_invalid")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise KbError("kb_invalid") from None
        if not text.startswith(f"# {meta.title}\n"):
            raise KbError("kb_invalid")
        articles.append(Article(meta=meta, text=text))
    return KbSnapshot(articles=tuple(articles))
