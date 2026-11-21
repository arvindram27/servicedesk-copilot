"""Session bootstrap and store behaviour (B4)."""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.main import create_app
from app.session import SessionStore
from fastapi.testclient import TestClient

AUTHORITY = "127.0.0.1:8000"
ORIGIN = f"http://{AUTHORITY}"


def _client(tmp_path: Path, *, writes: bool = True) -> TestClient:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    settings = Settings(runtime_dir=runtime, writes_enabled=writes)
    return TestClient(create_app(settings), base_url=f"http://{AUTHORITY}")


def _browser_headers() -> dict[str, str]:
    return {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_bootstrap_returns_four_fields_and_no_store(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post("/api/session")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"session_id", "csrf_token", "expires_at", "client_kind"}
    assert len(body["session_id"]) == 43
    assert len(body["csrf_token"]) == 43
    assert body["client_kind"] == "cli"
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


def test_bootstrap_mints_distinct_sessions(tmp_path: Path) -> None:
    client = _client(tmp_path)

    first = client.post("/api/session").json()
    second = client.post("/api/session").json()

    assert first["session_id"] != second["session_id"]
    assert first["csrf_token"] != second["csrf_token"]


def test_browser_bootstrap_is_labelled_browser(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post("/api/session", headers=_browser_headers())

    assert response.status_code == 200
    assert response.json()["client_kind"] == "browser"


def test_bootstrap_works_while_writes_are_disabled(tmp_path: Path) -> None:
    client = _client(tmp_path, writes=False)

    response = client.post("/api/session")

    assert response.status_code == 200


def test_bootstrap_rejects_cross_site_origin(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/session",
        headers={"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "bad_sec_fetch_site"


def test_expired_session_is_refused_and_swept(tmp_path: Path) -> None:
    clock = _Clock()
    client = _client(tmp_path)
    client.app.state.sessions = SessionStore(clock=clock)  # type: ignore[attr-defined]

    session = client.post("/api/session").json()
    clock.now = 3601.0

    response = client.post(
        "/api/_stub/write",
        headers={
            "X-SDC-Session": session["session_id"],
            "X-SDC-CSRF": session["csrf_token"],
        },
    )

    assert response.status_code == 403
    assert response.json()["code"] == "bad_session"


def test_purge_expired_removes_dead_sessions() -> None:
    clock = _Clock()
    store = SessionStore(ttl_s=10.0, clock=clock)
    assert store.mint("cli") is not None
    assert store.mint("cli") is not None

    clock.now = 11.0

    assert store.purge_expired() == 2


def test_mint_sweeps_expired_rows_before_capacity_check() -> None:
    clock = _Clock()
    store = SessionStore(ttl_s=10.0, capacity=1, clock=clock)
    assert store.mint("cli") is not None

    clock.now = 11.0

    assert store.mint("cli") is not None


def test_full_store_of_live_sessions_answers_503(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.app.state.sessions = SessionStore(capacity=1)  # type: ignore[attr-defined]

    assert client.post("/api/session").status_code == 200

    response = client.post("/api/session")

    assert response.status_code == 503
    assert response.json()["code"] == "session_store_full"
    assert response.headers["content-type"].startswith("application/problem+json")


def test_independent_apps_have_independent_stores(tmp_path: Path) -> None:
    first = _client(tmp_path / "first")
    second = _client(tmp_path / "second")

    session = first.post("/api/session").json()

    response = second.post(
        "/api/_stub/write",
        headers={
            "X-SDC-Session": session["session_id"],
            "X-SDC-CSRF": session["csrf_token"],
        },
    )

    assert response.status_code == 403
    assert response.json()["code"] == "bad_session"
