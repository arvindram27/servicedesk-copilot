"""Browser write-guard matrix against the stub route (B4).

Every test runs over HTTP against the production wiring, with an explicit
``base_url`` matching the configured authority. ``app.routes`` is never
introspected.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient

AUTHORITY = "127.0.0.1:8000"
ORIGIN = f"http://{AUTHORITY}"
STUB = "/api/_stub/write"


def _client(tmp_path: Path, *, writes: bool = True) -> TestClient:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    settings = Settings(runtime_dir=runtime, writes_enabled=writes)
    return TestClient(create_app(settings), base_url=f"http://{AUTHORITY}")


def _session(client: TestClient, *, browser: bool = False) -> dict[str, str]:
    headers = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"} if browser else {}
    response = client.post("/api/session", headers=headers)
    assert response.status_code == 200
    return response.json()


def _write(client: TestClient, session: dict[str, str], *, browser: bool = True):
    headers = {
        "X-SDC-Session": session["session_id"],
        "X-SDC-CSRF": session["csrf_token"],
    }
    if browser:
        headers["Origin"] = ORIGIN
        headers["Sec-Fetch-Site"] = "same-origin"
    return client.post(STUB, headers=headers)


def _assert_problem(response, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["title"]
    assert body["status"] == status
    assert body["code"] == code


def test_good_browser_write_allowed(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session = _session(client, browser=True)

    response = _write(client, session)

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


def test_cross_site_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session = _session(client, browser=True)

    response = client.post(
        STUB,
        headers={
            "X-SDC-Session": session["session_id"],
            "X-SDC-CSRF": session["csrf_token"],
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "cross-site",
        },
    )

    _assert_problem(response, 403, "bad_sec_fetch_site")


def test_sec_fetch_without_origin_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session = _session(client, browser=True)

    response = client.post(
        STUB,
        headers={
            "X-SDC-Session": session["session_id"],
            "X-SDC-CSRF": session["csrf_token"],
            "Sec-Fetch-Site": "same-origin",
        },
    )

    _assert_problem(response, 403, "missing_origin")


def test_rebound_host_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session = _session(client, browser=True)

    response = client.post(
        STUB,
        headers={
            "X-SDC-Session": session["session_id"],
            "X-SDC-CSRF": session["csrf_token"],
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
            "Host": "attacker.example:8000",
        },
    )

    _assert_problem(response, 403, "host_not_allowed")
    assert "attacker.example" not in response.text


def test_missing_csrf_header_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session = _session(client, browser=True)

    response = client.post(
        STUB,
        headers={
            "X-SDC-Session": session["session_id"],
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
        },
    )

    _assert_problem(response, 403, "missing_csrf")


def test_cli_write_allowed_and_classified(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session = _session(client)

    assert session["client_kind"] == "cli"

    response = _write(client, session, browser=False)

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


def test_writes_disabled_returns_503(tmp_path: Path) -> None:
    client = _client(tmp_path, writes=False)
    session = _session(client, browser=True)

    response = _write(client, session)

    _assert_problem(response, 503, "writes_disabled")


def test_wrong_csrf_value_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session = _session(client, browser=True)

    response = client.post(
        STUB,
        headers={
            "X-SDC-Session": session["session_id"],
            "X-SDC-CSRF": "A" * 43,
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
        },
    )

    _assert_problem(response, 403, "bad_csrf")


def test_unknown_session_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post(
        STUB,
        headers={
            "X-SDC-Session": "B" * 43,
            "X-SDC-CSRF": "C" * 43,
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
        },
    )

    _assert_problem(response, 403, "bad_session")


def test_loopback_alias_origin_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session = _session(client, browser=True)

    response = client.post(
        STUB,
        headers={
            "X-SDC-Session": session["session_id"],
            "X-SDC-CSRF": session["csrf_token"],
            "Origin": "http://localhost:8000",
            "Sec-Fetch-Site": "same-origin",
        },
    )

    _assert_problem(response, 403, "origin_not_allowed")


@pytest.mark.parametrize(
    ("origin", "code"),
    [
        ("http://127.0.0.1:8001", "origin_not_allowed"),
        ("https://127.0.0.1:8000", "bad_origin"),
        ("null", "bad_origin"),
        ("http://user@127.0.0.1:8000", "bad_origin"),
        ("http://127.0.0.1:8000/path", "bad_origin"),
        ("http://127.0.0.1:8000?x=1", "bad_origin"),
        ("http://127.0.0.1:8000#fragment", "bad_origin"),
        ("http://::1:8000", "bad_origin"),
        ("http://127.0.0.1", "origin_not_allowed"),
    ],
)
def test_bad_origin_forms_rejected(tmp_path: Path, origin: str, code: str) -> None:
    client = _client(tmp_path)
    session = _session(client, browser=True)

    response = client.post(
        STUB,
        headers={
            "X-SDC-Session": session["session_id"],
            "X-SDC-CSRF": session["csrf_token"],
            "Origin": origin,
            "Sec-Fetch-Site": "same-origin",
        },
    )

    _assert_problem(response, 403, code)


@pytest.mark.parametrize(
    "host",
    [
        "localhost.:8000",
        "2130706433:8000",
        "0x7f000001:8000",
        "127.0.0.1:8000,evil.example:8000",
        "a" * 300 + ":8000",
    ],
)
def test_bad_host_forms_rejected(tmp_path: Path, host: str) -> None:
    client = _client(tmp_path)

    response = client.post(STUB, headers={"Host": host})

    _assert_problem(response, 400, "bad_host")


def test_non_ascii_host_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post(
        STUB,
        headers=[(b"host", "localhosté:8000".encode())],
    )

    _assert_problem(response, 400, "bad_host")


def test_duplicate_origin_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session = _session(client, browser=True)

    response = client.post(
        STUB,
        headers=[
            ("x-sdc-session", session["session_id"]),
            ("x-sdc-csrf", session["csrf_token"]),
            ("origin", ORIGIN),
            ("origin", ORIGIN),
            ("sec-fetch-site", "same-origin"),
        ],
    )

    _assert_problem(response, 403, "bad_origin")


def test_duplicate_host_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post(
        STUB,
        headers=[
            (b"host", b"127.0.0.1:8000"),
            (b"host", b"127.0.0.1:8000"),
        ],
    )

    _assert_problem(response, 400, "bad_host")


def test_duplicate_sec_fetch_site_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session = _session(client, browser=True)

    response = client.post(
        STUB,
        headers=[
            ("x-sdc-session", session["session_id"]),
            ("x-sdc-csrf", session["csrf_token"]),
            ("origin", ORIGIN),
            ("sec-fetch-site", "same-origin"),
            ("sec-fetch-site", "same-origin"),
        ],
    )

    _assert_problem(response, 403, "bad_sec_fetch_site")


def test_duplicate_csrf_header_maps_to_missing_csrf(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session = _session(client, browser=True)

    response = client.post(
        STUB,
        headers=[
            ("x-sdc-session", session["session_id"]),
            ("x-sdc-csrf", session["csrf_token"]),
            ("x-sdc-csrf", session["csrf_token"]),
            ("origin", ORIGIN),
            ("sec-fetch-site", "same-origin"),
        ],
    )

    _assert_problem(response, 403, "missing_csrf")


def test_duplicate_session_header_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session = _session(client, browser=True)

    response = client.post(
        STUB,
        headers=[
            ("x-sdc-session", session["session_id"]),
            ("x-sdc-session", session["session_id"]),
            ("x-sdc-csrf", session["csrf_token"]),
            ("origin", ORIGIN),
            ("sec-fetch-site", "same-origin"),
        ],
    )

    _assert_problem(response, 403, "bad_session")


def test_malformed_token_values_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session = _session(client, browser=True)

    short_session = client.post(
        STUB,
        headers={
            "X-SDC-Session": "x" * 42,
            "X-SDC-CSRF": session["csrf_token"],
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
        },
    )
    _assert_problem(short_session, 403, "bad_session")

    bad_csrf = client.post(
        STUB,
        headers={
            "X-SDC-Session": session["session_id"],
            "X-SDC-CSRF": "!" * 43,
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
        },
    )
    _assert_problem(bad_csrf, 403, "bad_csrf")


def test_any_sec_fetch_header_without_origin_is_missing_origin(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session = _session(client)

    response = client.post(
        STUB,
        headers={
            "X-SDC-Session": session["session_id"],
            "X-SDC-CSRF": session["csrf_token"],
            "Sec-Fetch-Mode": "cors",
        },
    )

    _assert_problem(response, 403, "missing_origin")


def test_preflight_has_no_cors_headers(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.options(STUB, headers={"Origin": ORIGIN})

    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-credentials" not in response.headers


def test_problem_bodies_never_echo_input(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session = _session(client, browser=True)
    marker = "MARKER-9f8e7d6c5b4a3210"

    response = client.post(
        STUB,
        headers={
            "X-SDC-Session": session["session_id"],
            "X-SDC-CSRF": marker,
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
        },
    )

    _assert_problem(response, 403, "bad_csrf")
    assert marker not in response.text
    assert session["session_id"] not in response.text
    assert session["csrf_token"] not in response.text
