"""Local CLI for the deterministic cited-answer read path (Gate 1)."""

from __future__ import annotations

import argparse
import sys
from typing import Final
from urllib.parse import urlsplit

import httpx

DEFAULT_BASE_URL: Final[str] = "http://127.0.0.1:8000"
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost"})
_TIMEOUT_S: Final[float] = 10.0


class CliError(RuntimeError):
    """The requested CLI target or response is not permitted."""


def validate_base_url(value: str) -> str:
    """Accept only a plain http loopback target with no credentials or path."""
    parsed = urlsplit(value)
    if parsed.scheme != "http":
        raise CliError("base URL must use http")
    if parsed.username is not None or parsed.password is not None:
        raise CliError("base URL must not carry credentials")
    if parsed.hostname not in _LOOPBACK_HOSTS:
        raise CliError("base URL must target loopback")
    if parsed.path not in {"", "/"}:
        raise CliError("base URL must not carry a path")
    if parsed.query or parsed.fragment:
        raise CliError("base URL must not carry a query or fragment")
    try:
        _ = parsed.port
    except ValueError:
        raise CliError("base URL has an invalid port") from None
    return value.rstrip("/")


def build_client(base_url: str, *, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """Build a loopback HTTP client that never follows redirects or proxies."""
    return httpx.Client(
        base_url=base_url,
        timeout=httpx.Timeout(_TIMEOUT_S),
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    )


def ask(
    base_url: str,
    message: str,
    *,
    transport: httpx.BaseTransport | None = None,
    conversation_id: str | None = None,
) -> tuple[int, dict[str, object]]:
    """Send one ask request and return (status, parsed body)."""
    target = validate_base_url(base_url)
    payload: dict[str, object] = {"message": message}
    if conversation_id is not None:
        payload["conversation_id"] = conversation_id
    with build_client(target, transport=transport) as client:
        try:
            response = client.post("/chat", json=payload)
        except httpx.HTTPError:
            raise CliError("request failed") from None
    if response.status_code != 200:
        return response.status_code, {}
    try:
        body = response.json()
    except ValueError:
        raise CliError("response was not valid JSON") from None
    if type(body) is not dict:
        raise CliError("response was not a JSON object")
    return 200, body


def _print_result(body: dict[str, object]) -> int:
    reply = body.get("reply")
    if type(reply) is not str:
        print("response missing reply")
        return 1
    print(reply)
    citations = body.get("citations")
    if type(citations) is list:
        for citation in citations:
            if type(citation) is dict:
                article_id = citation.get("article_id")
                version = citation.get("version")
                section = citation.get("section")
                print(f"Source: {article_id} version {version}, {section}")
    return 0


def main(argv: list[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    """Parse arguments and run one ask command."""
    parser = argparse.ArgumentParser(prog="sdc")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ask_parser = subparsers.add_parser("ask", help="ask one supported question")
    ask_parser.add_argument("message")
    ask_parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args(argv)
    try:
        status, body = ask(args.base_url, args.message, transport=transport)
    except CliError as exc:
        print(f"error: {exc}")
        return 2
    if status != 200:
        print(f"request failed with status {status}")
        return 1
    return _print_result(body)


if __name__ == "__main__":
    sys.exit(main())
