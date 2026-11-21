"""Validator tests for Settings.database_path (B1, brief section B.1).

The plan's symlink claim - a symlink inside runtime/ that resolves outside is
rejected because both paths are ``Path.resolve()``d - holds by construction but
is NOT separately tested here: creating symlinks can require privileges on
Windows.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import PROJECT_ROOT, Settings
from pydantic import ValidationError


def test_default_resolved_path_is_inside_runtime() -> None:
    settings = Settings()
    assert settings.resolved_database_path == settings.runtime_dir / "servicedesk.db"
    assert settings.database_path is None


def test_override_inside_runtime_dir_is_accepted() -> None:
    target = PROJECT_ROOT / "runtime" / "custom.db"
    settings = Settings(database_path=target)
    assert settings.resolved_database_path == target


def test_override_outside_runtime_dir_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(database_path=tmp_path / "outside.db")
