"""Shared pytest fixtures for the minipic test suite.

Strategy:
- Mock all httpx traffic via httpx.MockTransport (no real network).
- Redirect the user data dir to a tmp path so storage tests never touch the
  real user config / data dir.
- Provide a sample Config + a `make_client` helper that wires MockTransport.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest.mock import patch

import httpx
import pytest

# Ensure src/ is on sys.path so `import minipic` works without `pip install -e`.
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


from minipic.client import MiniMaxClient  # noqa: E402
from minipic.config import Config, user_data_path  # noqa: E402
from minipic.storage import db_path  # noqa: E402


# --------------------------------------------------------------------------- env
@pytest.fixture(autouse=True)
def _isolate_user_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force user data + config dirs to a tmp path for every test.

    Also clear env vars that could leak between tests.

    On Windows, platformdirs ignores APPDATA / LOCALAPPDATA env vars
    (it hardcodes ``C:\\Users\\<user>\\AppData\\Local``), so we also
    monkeypatch the ``_user_config_path`` and ``_user_data_dir`` helpers
    directly.
    """
    # Try to redirect platformdirs via env first (helps on Linux/Mac).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))

    # Direct monkeypatch — works on Windows too.
    import minipic.config as cfg_mod
    import minipic.web as web_mod
    fake_user_cfg = tmp_path / "user_cfg" / "config.json"
    fake_user_data = tmp_path / "user_data"
    monkeypatch.setattr(cfg_mod, "_user_config_path", lambda: fake_user_cfg)
    monkeypatch.setattr(cfg_mod, "_user_data_dir", lambda: fake_user_data)
    monkeypatch.setattr(cfg_mod, "user_data_path",
                        lambda name: _fake_user_data_path(fake_user_data, name))
    # web.py also imports _user_config_path at module level; patch both
    # namespaces so _detect_key_source and any other web helper see the
    # isolated tmp path rather than the real %APPDATA%/minipic/config.json.
    monkeypatch.setattr(web_mod, "_user_config_path", lambda: fake_user_cfg)

    # Strip secrets from env
    for k in ("MINIMAX_API_KEY", "MINIMAX_BASE_URL", "MINIPIC_CONFIG_PATH"):
        monkeypatch.delenv(k, raising=False)
    # Make sure cwd isn't polluted with a stray config.json
    monkeypatch.chdir(tmp_path)
    yield


def _fake_user_data_path(root: Path, name: str) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# --------------------------------------------------------------------------- config
@pytest.fixture
def sample_config() -> Config:
    """A valid in-memory Config — no file I/O."""
    return Config(api_key="test-key-123", base_url="https://api.example.com")


@pytest.fixture
def empty_config() -> Config:
    return Config()


# --------------------------------------------------------------------------- httpx mock
@pytest.fixture
def make_client(sample_config: Config):
    """Return a factory: `make_client(handler)` -> (client, transport, calls).

    Usage:
        async with make_client(my_handler) as (client, transport, calls):
            ... await client.create_video_task(...)
        # `calls` is a list of httpx.Request objects the mock received
    """

    def _make(handler: Callable[[httpx.Request], httpx.Response]):
        calls: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return handler(request)

        transport = httpx.MockTransport(wrapped)
        return MiniMaxClient, sample_config, transport, calls

    return _make


# --------------------------------------------------------------------------- storage
@pytest.fixture
def storage_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Make db_path() return a tmp path; also clear any pre-existing DB.

    Returns the tmp Path for assertions.
    """
    target = tmp_path / "tasks.db"
    monkeypatch.setattr("minipic.storage.db_path", lambda: target)
    # init_db is called lazily by insert_task / get_task
    return target


# --------------------------------------------------------------------------- misc helpers
@pytest.fixture
def tmp_file(tmp_path: Path) -> Callable[[str, bytes], Path]:
    """Create a temp file with given contents; return its path."""

    def _make(name: str, data: bytes) -> Path:
        p = tmp_path / name
        p.write_bytes(data)
        return p

    return _make


@pytest.fixture
def payload_file(tmp_path: Path) -> Path:
    """A small JSON file used to test uploads / ref-resolution paths."""
    p = tmp_path / "scene.json"
    p.write_text(json.dumps({"ok": True}), encoding="utf-8")
    return p
