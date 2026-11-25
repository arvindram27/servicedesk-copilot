"""Public /chat read path, audit evidence and CLI constraints (Gate 1)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from app.cli import CliError, build_client, validate_base_url
from app.cli import main as cli_main
from app.config import Mode, Settings
from app.kb.citation import REFUSAL_REPLY, CitationError
from app.main import create_app
from fastapi.testclient import TestClient

from db_support import run_upgrade

ARTICLES: dict[str, tuple[str, str]] = {
    "PRN-001": (
        "Printer shows offline",
        "## Check the connection\n\n"
        "A printer that shows offline has usually lost its network link. "
        "Confirm the network cable is seated and the printer is on the office network.\n",
    ),
    "PRN-002": (
        "Paper jam in the printer",
        "## Open the panel\n\nOpen the panel named on the printer display and support it.\n",
    ),
}


def _write_kb(root: Path) -> Path:
    target_dir = root / "data" / "kb" / "printing"
    target_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, object]] = []
    for article_id, (title, body) in ARTICLES.items():
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
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_bytes(json.dumps({"format": 1, "articles": entries}).encode("utf-8"))
    return manifest


def _settings(root: Path, **overrides: object) -> Settings:
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    database = runtime / "servicedesk.db"
    run_upgrade(database)
    values: dict[str, object] = {
        "mode": Mode.CI_FAKE,
        "runtime_dir": runtime,
        "database_path": database,
        "kb_dir": root / "data" / "kb",
        "kb_manifest_path": _write_kb(root),
        "retrieval_top_k": 3,
        "retrieval_min_score": 0.0,
    }
    values.update(overrides)
    return Settings(**values)


def _client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8000")


def _audit_rows(settings: Settings) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(settings.resolved_database_path)
    try:
        rows = connection.execute(
            "SELECT event_type, node, outcome, payload_json, session_id"
            " FROM audit_events ORDER BY seq"
        ).fetchall()
    finally:
        connection.close()
    return [tuple(row) for row in rows]


def test_supported_question_returns_checked_citation(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.post("/chat", json={"message": "my printer shows offline"})

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "answered"
    assert body["proposed_action"] is None
    citations = body["citations"]
    assert len(citations) == 1
    citation = citations[0]
    assert citation["article_id"] == "PRN-001"
    assert citation["version"] == 1
    assert citation["section"] == "Check the connection"
    assert citation["span"] in body["reply"]
    assert citation["end"] - citation["start"] == len(citation["span"])


def test_unsupported_question_refuses(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.post("/chat", json={"message": "how do I bake sourdough bread"})

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "refused"
    assert body["citations"] == []
    assert body["reply"] == REFUSAL_REPLY


def test_answer_and_refusal_write_audit_without_ticket_rows(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)

    client.post("/chat", json={"message": "my printer shows offline"})
    client.post("/chat", json={"message": "how do I bake sourdough bread"})

    rows = _audit_rows(settings)
    assert [row[0] for row in rows] == ["chat.answer", "chat.refusal"]
    assert rows[0][1] == "chat"
    assert rows[0][2] == "ok"
    assert rows[1][2] == "refused"
    answered = json.loads(rows[0][3])
    refused = json.loads(rows[1][3])
    assert answered["article_id"] == "PRN-001"
    assert answered["article_version"] == 1
    assert "message" not in answered
    assert "printer" not in json.dumps(answered).lower()
    assert refused["reason"] == "no_supporting_article"
    assert rows[0][4] is not None

    connection = sqlite3.connect(settings.resolved_database_path)
    try:
        for table in ("tickets", "confirmation_tokens", "idempotency_keys"):
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0
    finally:
        connection.close()


def test_existing_write_guards_still_apply(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.post("/api/_stub/write")

    assert response.status_code == 403
    assert response.json()["code"] == "bad_session"


def test_missing_kb_returns_fixed_503(tmp_path: Path) -> None:
    settings = _settings(tmp_path, kb_manifest_path=tmp_path / "missing.json")
    client = _client(settings)

    response = client.post("/chat", json={"message": "printer offline"})

    assert response.status_code == 503
    assert response.json()["code"] == "kb_unavailable"
    assert "missing.json" not in response.text


def test_missing_audit_database_returns_fixed_503(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    database = runtime / "absent.db"
    settings = Settings(
        mode=Mode.CI_FAKE,
        runtime_dir=runtime,
        database_path=database,
        kb_dir=tmp_path / "data" / "kb",
        kb_manifest_path=_write_kb(tmp_path),
        retrieval_min_score=0.0,
    )
    client = _client(settings)

    response = client.post("/chat", json={"message": "printer offline"})

    assert response.status_code == 503
    assert response.json()["code"] == "audit_unavailable"
    assert not database.exists()


def test_snapshot_is_cached_and_not_silently_reread(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)
    first = client.post("/chat", json={"message": "printer shows offline"}).json()

    article = tmp_path / "data" / "kb" / "printing" / "PRN-001.md"
    article.write_bytes(article.read_bytes() + b"\nChanged after load.\n")

    second = client.post("/chat", json={"message": "printer shows offline"}).json()

    assert second["reply"] == first["reply"]
    assert second["citations"] == first["citations"]


def test_citation_failure_refuses_before_showing_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)

    def reject(citation: object, chunks: object) -> object:
        raise CitationError("citation_invalid")

    monkeypatch.setattr("app.chat.validate_citation", reject)

    response = client.post("/chat", json={"message": "printer shows offline"})

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "refused"
    assert body["citations"] == []
    assert body["reply"] == REFUSAL_REPLY
    rows = _audit_rows(settings)
    assert rows[-1][0] == "chat.refusal"
    assert json.loads(rows[-1][3])["reason"] == "citation_invalid"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"message": ""},
        {"message": "   "},
        {"message": "x" * 1001},
        {"message": 5},
        {"message": "printer", "conversation_id": "not-a-uuid"},
    ],
)
def test_invalid_request_uses_fixed_error(tmp_path: Path, payload: dict[str, object]) -> None:
    client = _client(_settings(tmp_path))

    response = client.post("/chat", json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"
    assert "x" * 1001 not in response.text


def test_conversation_id_is_correlation_only(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)
    conversation_id = str(uuid4())

    response = client.post(
        "/chat",
        json={"message": "printer shows offline", "conversation_id": conversation_id},
    )

    assert response.status_code == 200
    rows = _audit_rows(settings)
    assert rows[0][4] == conversation_id


def _chat_payload() -> dict[str, object]:
    return {
        "reply": "Answer",
        "citations": [
            {
                "article_id": "PRN-001",
                "version": 1,
                "span": "Excerpt",
                "section": "Connection",
                "start": 0,
                "end": 7,
            }
        ],
        "proposed_action": None,
        "state": "answered",
    }


def test_cli_ask_prints_answer_and_source(capsys: pytest.CaptureFixture[str]) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=_chat_payload()))

    code = cli_main(["ask", "printer offline"], transport=transport)

    assert code == 0
    output = capsys.readouterr().out
    assert "Answer" in output
    assert "Source: PRN-001 version 1, Connection" in output


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8000",
        "http://example.com",
        "http://user:pass@127.0.0.1:8000",
        "http://127.0.0.1:8000/chat",
        "http://127.0.0.1:8000?x=1",
        "http://[::1]:8000",
    ],
)
def test_cli_rejects_unsafe_targets(url: str) -> None:
    with pytest.raises(CliError):
        validate_base_url(url)


def test_cli_does_not_follow_redirects(capsys: pytest.CaptureFixture[str]) -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8000/chat"})

    code = cli_main(["ask", "printer offline"], transport=httpx.MockTransport(redirect))

    assert code == 1
    assert "request failed with status 302" in capsys.readouterr().out


def test_cli_reports_transport_failure_without_echo(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    code = cli_main(["ask", "printer offline"], transport=httpx.MockTransport(fail))

    assert code == 2
    assert "error: request failed" in capsys.readouterr().out


def test_cli_client_disables_redirects() -> None:
    client = build_client("http://127.0.0.1:8000")
    try:
        assert client.follow_redirects is False
    finally:
        client.close()
