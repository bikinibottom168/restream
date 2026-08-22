"""Shared pytest fixtures.

Tests never touch the network: provider tests use ``httpx.MockTransport``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Keep every test away from the developer's real .env and data directory."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    for key in list(os.environ):
        if key.startswith(("TELEGRAM_", "ADMIN_")):
            monkeypatch.delenv(key, raising=False)
    from app.core.config import reload_settings

    reload_settings()
    yield
    reload_settings()


@pytest.fixture
def fixture_text():
    def _load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return _load


@pytest.fixture
def settings_store():
    from app.core.config import get_settings
    from app.core.settings_store import SettingsStore

    return SettingsStore(get_settings())
