"""Lexical retrieval and manifest verification (Gate 1)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from app.config import Settings
from app.kb.manifest import KbError, load_snapshot
from app.kb.retrieval import index_articles, retrieve, select_supported, tokenize
from pydantic import ValidationError

ARTICLES: dict[str, tuple[str, str]] = {
    "PRN-001": (
        "Printer shows offline",
        "## Check the connection\n\n"
        "A printer that shows offline has usually lost its network link. "
        "Confirm the network cable is seated and the printer is on the office network.\n\n"
        "## Power-cycle in order\n\n"
        "Turn the printer off, wait ten seconds, then turn it on again.\n",
    ),
    "PRN-002": (
        "Paper jam in the printer",
        "## Open the panel\n\n"
        "Open the panel named on the printer display and support it.\n\n"
        "## Remove the sheet\n\n"
        "Pull the jammed sheet in the direction the paper travels.\n",
    ),
}


def _write_kb(root: Path, articles: dict[str, tuple[str, str]] | None = None) -> Path:
    selected = ARTICLES if articles is None else articles
    target_dir = root / "data" / "kb" / "printing"
    target_dir.mkdir(parents=True)
    entries: list[dict[str, Any]] = []
    for article_id, (title, body) in selected.items():
        text = f"# {title}\n\nSynthetic demo article.\n\n{body}"
        relative = f"data/kb/printing/{article_id}.md"
        data = text.encode("utf-8")
        (root / relative).write_bytes(data)
        entries.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "kind": "kb_article",
                "frozen_at": "2026-11-23T00:00:00Z",
                "article_id": article_id,
                "version": 1,
                "title": title,
                "category": "hardware",
            }
        )
    manifest = root / "data" / "fixtures" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(json.dumps({"format": 1, "articles": entries}).encode("utf-8"))
    return manifest


def _load(manifest: Path, root: Path) -> Any:
    return load_snapshot(manifest, root=root, kb_root=root / "data" / "kb")


def _mutate(manifest: Path, change: Callable[[dict[str, Any]], None]) -> None:
    document = json.loads(manifest.read_bytes().decode("utf-8"))
    change(document)
    manifest.write_bytes(json.dumps(document).encode("utf-8"))


def _chunks(manifest: Path, root: Path) -> tuple[Any, ...]:
    return index_articles(_load(manifest, root))


def test_supported_question_ranks_expected_article(tmp_path: Path) -> None:
    manifest = _write_kb(tmp_path)
    chunks = _chunks(manifest, tmp_path)

    results = retrieve(chunks, "printer shows offline", top_k=3)
    best = select_supported(results, 0.0)

    assert best is not None
    assert best.chunk.article_id == "PRN-001"
    assert best.score > 0.0


def test_unrelated_question_has_no_evidence(tmp_path: Path) -> None:
    chunks = _chunks(_write_kb(tmp_path), tmp_path)

    results = retrieve(chunks, "sourdough starter hydration", top_k=3)

    assert results
    assert results[0].score == 0.0
    assert select_supported(results, 0.0) is None


def test_zero_threshold_never_selects_zero_overlap(tmp_path: Path) -> None:
    chunks = _chunks(_write_kb(tmp_path), tmp_path)

    results = retrieve(chunks, "quantum chromodynamics", top_k=3)

    assert select_supported(results, 0.0) is None


def test_high_threshold_refuses_a_weak_match(tmp_path: Path) -> None:
    chunks = _chunks(_write_kb(tmp_path), tmp_path)

    results = retrieve(chunks, "printer", top_k=3)

    assert select_supported(results, 100.0) is None


def test_tie_break_orders_by_article_id(tmp_path: Path) -> None:
    articles = {
        "PRN-001": ("Alpha", "## Same\n\nprinter offline cable\n"),
        "PRN-002": ("Beta", "## Same\n\nprinter offline cable\n"),
    }
    chunks = _chunks(_write_kb(tmp_path, articles), tmp_path)

    results = retrieve(chunks, "printer", top_k=5)

    assert [result.chunk.article_id for result in results] == ["PRN-001", "PRN-002"]
    assert results[0].score == results[1].score


def test_tokenize_filters_stopwords_and_short_terms() -> None:
    terms = tokenize("The printer is offline")

    assert "printer" in terms
    assert "offline" in terms
    assert "the" not in terms
    assert "is" not in terms


@pytest.mark.parametrize(
    "overrides",
    [
        {"retrieval_top_k": 0},
        {"retrieval_top_k": 11},
        {"retrieval_min_score": -1.0},
        {"retrieval_min_score": float("nan")},
        {"retrieval_min_score": float("inf")},
    ],
)
def test_invalid_retrieval_settings_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**overrides)


def test_missing_manifest_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(KbError):
        _load(tmp_path / "missing.json", tmp_path)


def test_malformed_manifest_rejected(tmp_path: Path) -> None:
    manifest = _write_kb(tmp_path)
    manifest.write_bytes(b"{not json")

    with pytest.raises(KbError):
        _load(manifest, tmp_path)


def test_checksum_mismatch_rejected(tmp_path: Path) -> None:
    manifest = _write_kb(tmp_path)
    _mutate(manifest, lambda document: document["articles"][0].update({"sha256": "0" * 64}))

    with pytest.raises(KbError):
        _load(manifest, tmp_path)


def test_byte_length_mismatch_rejected(tmp_path: Path) -> None:
    manifest = _write_kb(tmp_path)
    _mutate(manifest, lambda document: document["articles"][0].update({"bytes": 1}))

    with pytest.raises(KbError):
        _load(manifest, tmp_path)


def test_article_bytes_changed_rejected(tmp_path: Path) -> None:
    manifest = _write_kb(tmp_path)
    article = tmp_path / "data" / "kb" / "printing" / "PRN-001.md"
    article.write_bytes(article.read_bytes() + b"changed\n")

    with pytest.raises(KbError):
        _load(manifest, tmp_path)


def test_duplicate_identity_rejected(tmp_path: Path) -> None:
    manifest = _write_kb(tmp_path)

    def change(document: dict[str, Any]) -> None:
        document["articles"].append(dict(document["articles"][0]))

    _mutate(manifest, change)

    with pytest.raises(KbError):
        _load(manifest, tmp_path)


def test_duplicate_path_rejected(tmp_path: Path) -> None:
    manifest = _write_kb(tmp_path)

    def change(document: dict[str, Any]) -> None:
        second = dict(document["articles"][1])
        second["article_id"] = "PRN-009"
        document["articles"].append(second)

    _mutate(manifest, change)

    with pytest.raises(KbError):
        _load(manifest, tmp_path)


def test_path_escape_rejected(tmp_path: Path) -> None:
    manifest = _write_kb(tmp_path)
    _mutate(manifest, lambda document: document["articles"][0].update({"path": "../outside.md"}))

    with pytest.raises(KbError):
        _load(manifest, tmp_path)


def test_absolute_path_rejected(tmp_path: Path) -> None:
    manifest = _write_kb(tmp_path)

    def change(document: dict[str, Any]) -> None:
        document["articles"][0]["path"] = "/data/kb/printing/PRN-001.md"

    _mutate(manifest, change)

    with pytest.raises(KbError):
        _load(manifest, tmp_path)


def test_non_article_kind_rejected(tmp_path: Path) -> None:
    manifest = _write_kb(tmp_path)
    _mutate(manifest, lambda document: document["articles"][0].update({"kind": "golden_case"}))

    with pytest.raises(KbError):
        _load(manifest, tmp_path)
