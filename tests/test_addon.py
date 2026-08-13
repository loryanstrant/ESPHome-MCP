from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest

from esphome_mcp import addon

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated_environ():
    """Restore os.environ after every test.

    apply_addon_options_to_env / resolve_mcp_path mutate os.environ directly
    (that's their job) via os.environ[...] = ..., which monkeypatch.setenv/delenv
    can't track or auto-revert since it never made those calls itself. Without
    this, a test setting ESPHOME_DASHBOARD_URL leaks it into every later test in
    the whole suite (including the live-dashboard test, which then tries to
    actually connect to it).
    """
    original = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(original)


# ------------------------------------------------------------------ load_addon_options


def test_load_addon_options_missing_file(tmp_path: Path):
    assert addon.load_addon_options(tmp_path / "options.json") == {}


def test_load_addon_options_malformed_json(tmp_path: Path):
    options_path = tmp_path / "options.json"
    options_path.write_text("{not valid json")
    assert addon.load_addon_options(options_path) == {}


def test_load_addon_options_valid(tmp_path: Path):
    options_path = tmp_path / "options.json"
    options_path.write_text(json.dumps({"dashboard_url": "http://esphome.local"}))
    assert addon.load_addon_options(options_path) == {"dashboard_url": "http://esphome.local"}


# ------------------------------------------------------------- apply_addon_options_to_env


def test_apply_addon_options_to_env_sets_unset_vars(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ESPHOME_DASHBOARD_URL", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    addon.apply_addon_options_to_env(
        {"dashboard_url": "http://esphome.local", "log_level": "debug"}
    )
    import os

    assert os.environ["ESPHOME_DASHBOARD_URL"] == "http://esphome.local"
    assert os.environ["LOG_LEVEL"] == "debug"


def test_apply_addon_options_to_env_explicit_env_wins(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ESPHOME_DASHBOARD_URL", "http://explicit.example.com")
    addon.apply_addon_options_to_env({"dashboard_url": "http://from-options.local"})
    import os

    assert os.environ["ESPHOME_DASHBOARD_URL"] == "http://explicit.example.com"


def test_apply_addon_options_to_env_skips_empty_values(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ESPHOME_DASHBOARD_USERNAME", raising=False)
    addon.apply_addon_options_to_env({"dashboard_username": ""})
    import os

    assert "ESPHOME_DASHBOARD_USERNAME" not in os.environ


# --------------------------------------------------------------- get_or_create_secret_path


def test_get_or_create_secret_path_generates_when_absent(tmp_path: Path):
    secret_file = tmp_path / "secret_path.txt"
    path = addon.get_or_create_secret_path({}, secret_file)
    assert path.startswith("/private_")
    assert len(path) >= 8
    assert secret_file.read_text().strip() == path


def test_get_or_create_secret_path_reuses_existing_valid(tmp_path: Path):
    secret_file = tmp_path / "secret_path.txt"
    secret_file.write_text("/private_existingtoken123")
    path = addon.get_or_create_secret_path({}, secret_file)
    assert path == "/private_existingtoken123"


def test_get_or_create_secret_path_regenerates_when_stored_invalid(tmp_path: Path):
    secret_file = tmp_path / "secret_path.txt"
    secret_file.write_text("short")
    path = addon.get_or_create_secret_path({}, secret_file)
    assert path != "short"
    assert path.startswith("/private_")
    assert secret_file.read_text().strip() == path


def test_get_or_create_secret_path_honors_valid_override(tmp_path: Path):
    secret_file = tmp_path / "secret_path.txt"
    path = addon.get_or_create_secret_path({"secret_path": "/my-custom-path"}, secret_file)
    assert path == "/my-custom-path"
    assert secret_file.read_text().strip() == "/my-custom-path"


def test_get_or_create_secret_path_override_without_leading_slash(tmp_path: Path):
    secret_file = tmp_path / "secret_path.txt"
    path = addon.get_or_create_secret_path({"secret_path": "my-custom-path"}, secret_file)
    assert path == "/my-custom-path"


def test_get_or_create_secret_path_invalid_override_falls_back(tmp_path: Path):
    secret_file = tmp_path / "secret_path.txt"
    secret_file.write_text("/private_storedvalid1234")
    path = addon.get_or_create_secret_path({"secret_path": "/x"}, secret_file)
    assert path == "/private_storedvalid1234"


def test_get_or_create_secret_path_two_calls_are_stable(tmp_path: Path):
    secret_file = tmp_path / "secret_path.txt"
    first = addon.get_or_create_secret_path({}, secret_file)
    second = addon.get_or_create_secret_path({}, secret_file)
    assert first == second


def test_get_or_create_secret_path_regenerate_forces_new_value(tmp_path: Path):
    secret_file = tmp_path / "secret_path.txt"
    secret_file.write_text("/private_originalvalid123")

    new = addon.get_or_create_secret_path({"regenerate_secret_path": True}, secret_file)

    assert new != "/private_originalvalid123"
    assert new.startswith("/private_")
    assert secret_file.read_text().strip() == new


def test_get_or_create_secret_path_override_wins_over_regenerate(tmp_path: Path):
    secret_file = tmp_path / "secret_path.txt"
    path = addon.get_or_create_secret_path(
        {"regenerate_secret_path": True, "secret_path": "/my-pinned-path"}, secret_file
    )
    assert path == "/my-pinned-path"


def test_get_or_create_secret_path_regenerate_resets_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    secret_file = tmp_path / "secret_path.txt"
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        addon, "_reset_regenerate_flag", lambda options, client=None: calls.append(options)
    )

    addon.get_or_create_secret_path({"regenerate_secret_path": True}, secret_file)

    assert len(calls) == 1
    assert calls[0]["regenerate_secret_path"] is True


def test_get_or_create_secret_path_no_regenerate_does_not_reset_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    secret_file = tmp_path / "secret_path.txt"
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        addon, "_reset_regenerate_flag", lambda options, client=None: calls.append(options)
    )

    addon.get_or_create_secret_path({}, secret_file)

    assert calls == []


def test_reset_regenerate_flag_posts_options_with_flag_off(monkeypatch: pytest.MonkeyPatch):
    import httpx

    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    posted: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-token"
        posted.update(json.loads(request.content))
        return httpx.Response(200, json={"result": "ok"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    addon._reset_regenerate_flag({"regenerate_secret_path": True, "log_level": "info"}, client)

    assert posted == {"options": {"regenerate_secret_path": False, "log_level": "info"}}


def test_reset_regenerate_flag_no_token_is_noop(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    # Must not raise even without a client / token.
    addon._reset_regenerate_flag({"regenerate_secret_path": True})


def test_reset_regenerate_flag_swallows_http_errors(monkeypatch: pytest.MonkeyPatch):
    import httpx

    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    # Must not raise.
    addon._reset_regenerate_flag({"regenerate_secret_path": True}, client)


# ------------------------------------------------------------------------ resolve_mcp_path


def test_resolve_mcp_path_no_options_file_defaults_to_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("MCP_PATH", raising=False)
    result = addon.resolve_mcp_path(
        options_path=tmp_path / "missing.json", secret_file=tmp_path / "secret_path.txt"
    )
    assert result == "/mcp"
    assert not (tmp_path / "secret_path.txt").exists()


def test_resolve_mcp_path_no_options_file_honors_explicit_mcp_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MCP_PATH", "/custom")
    result = addon.resolve_mcp_path(
        options_path=tmp_path / "missing.json", secret_file=tmp_path / "secret_path.txt"
    )
    assert result == "/custom"


def test_resolve_mcp_path_with_options_sets_secret_path_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("MCP_PATH", raising=False)
    monkeypatch.delenv("ESPHOME_DASHBOARD_URL", raising=False)
    options_path = tmp_path / "options.json"
    options_path.write_text(json.dumps({"dashboard_url": "http://esphome.local"}))
    secret_file = tmp_path / "secret_path.txt"

    import os

    result = addon.resolve_mcp_path(options_path=options_path, secret_file=secret_file)

    assert result.startswith("/private_")
    assert os.environ["MCP_PATH"] == result
    assert os.environ["ESPHOME_DASHBOARD_URL"] == "http://esphome.local"
    assert secret_file.exists()


def test_resolve_mcp_path_with_options_explicit_mcp_path_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MCP_PATH", "/already-set")
    options_path = tmp_path / "options.json"
    options_path.write_text(json.dumps({"dashboard_url": "http://esphome.local"}))
    secret_file = tmp_path / "secret_path.txt"

    result = addon.resolve_mcp_path(options_path=options_path, secret_file=secret_file)

    assert result == "/already-set"
    assert not secret_file.exists()
