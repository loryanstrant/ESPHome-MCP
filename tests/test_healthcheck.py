from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

_HEALTHCHECK_PATH = Path(__file__).resolve().parent.parent / "healthcheck.py"


def _load_healthcheck():
    spec = importlib.util.spec_from_file_location("healthcheck", _HEALTHCHECK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["healthcheck"] = module
    spec.loader.exec_module(module)
    return module


def test_mcp_url_defaults_to_mcp(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MCP_PATH", raising=False)
    healthcheck = _load_healthcheck()
    assert healthcheck._mcp_url() == "http://localhost:8080/mcp"


def test_mcp_url_honors_mcp_path_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MCP_PATH", "/private_abc123")
    healthcheck = _load_healthcheck()
    assert healthcheck._mcp_url() == "http://localhost:8080/private_abc123"
