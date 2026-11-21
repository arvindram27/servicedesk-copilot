"""Static checks on the Dockerfile and compose file.

No Docker build, start, pull or compose-up happens here. These checks parse the
authored files as text and YAML only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = PROJECT_ROOT / "docker-compose.yml"
DOCKERFILE_PATH = PROJECT_ROOT / "Dockerfile"


def _compose() -> dict[str, Any]:
    with COMPOSE_PATH.open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict)
    return loaded


def test_compose_publishes_exactly_one_loopback_port() -> None:
    compose = _compose()
    published: list[tuple[str, str]] = []
    for name, service in compose["services"].items():
        for mapping in service.get("ports", []) or []:
            published.append((name, str(mapping)))
    assert published == [("app", "127.0.0.1:8000:8000")]


def test_every_published_mapping_is_host_loopback_prefixed() -> None:
    compose = _compose()
    for name, service in compose["services"].items():
        for mapping in service.get("ports", []) or []:
            assert str(mapping).startswith("127.0.0.1:"), (name, mapping)


def test_itsm_service_publishes_no_port() -> None:
    compose = _compose()
    itsm = compose["services"]["itsm"]
    assert "ports" not in itsm
    assert itsm["expose"] == ["8010"]


def test_no_service_uses_host_networking() -> None:
    compose = _compose()
    for name, service in compose["services"].items():
        assert "network_mode" not in service, name


def test_services_share_a_private_internal_network() -> None:
    compose = _compose()
    assert compose["networks"]["internal"]["internal"] is True
    for name, service in compose["services"].items():
        assert service["networks"] == ["internal"], name


def test_compose_does_not_widen_the_listen_host() -> None:
    compose = _compose()
    for name, service in compose["services"].items():
        env = service.get("environment", {}) or {}
        assert env.get("SDC_LISTEN_HOST") in (None, "127.0.0.1"), name


def test_dockerfile_binds_the_container_interface_only() -> None:
    text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert '"--host", "0.0.0.0"' in text
    assert '"--workers", "1"' in text


def test_dockerfile_does_not_publish_or_use_host_network() -> None:
    text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert "--publish" not in text
    assert "--network=host" not in text
