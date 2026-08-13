from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import httpx
from starlette.testclient import TestClient

from esphome_mcp import server

if TYPE_CHECKING:
    import pytest

# ------------------------------------------------------------------- _resolve_host_port


async def test_resolve_host_port_no_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert await server._resolve_host_port() == server.DEFAULT_PORT


async def test_resolve_host_port_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(200, json={"data": {"network": {"8080/tcp": 9999}}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await server._resolve_host_port(client) == 9999


async def test_resolve_host_port_falls_back_on_http_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await server._resolve_host_port(client) == server.DEFAULT_PORT


async def test_resolve_host_port_falls_back_on_malformed_response(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await server._resolve_host_port(client) == server.DEFAULT_PORT


# --------------------------------------------------------------- _check_dashboard_status


class _SlowClient:
    """Simulates a dashboard call that hangs past the status page's own timeout."""

    async def get_version(self) -> str:
        await asyncio.sleep(10)
        return "unreachable-in-practice"


async def test_check_dashboard_status_times_out_quickly(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "get_client", lambda: _SlowClient())
    monkeypatch.setattr(server, "_STATUS_CHECK_TIMEOUT", 0.05)

    start = time.monotonic()
    result = await server._check_dashboard_status()
    elapsed = time.monotonic() - start

    assert "unreachable" in result.lower()
    assert "timed out" in result.lower()
    assert elapsed < 1.0


# ------------------------------------------------------------------------- status_page


class _FakeClient:
    def __init__(self, version: str = "2026.6.0", error: Exception | None = None) -> None:
        self._version = version
        self._error = error

    async def get_version(self) -> str:
        if self._error:
            raise self._error
        return self._version


async def _fake_port() -> int:
    return 8080


def test_status_page_healthy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "get_client", lambda: _FakeClient())
    monkeypatch.setattr(server, "_resolve_host_port", _fake_port)
    monkeypatch.setenv("MCP_PATH", "/private_abc123")

    client = TestClient(server.mcp.http_app())
    resp = client.get("/")

    assert resp.status_code == 200
    assert "2026.6.0" in resp.text
    assert "/private_abc123" in resp.text
    assert ":8080" in resp.text


def test_status_page_dashboard_unreachable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        server, "get_client", lambda: _FakeClient(error=RuntimeError("connection refused"))
    )
    monkeypatch.setattr(server, "_resolve_host_port", _fake_port)
    monkeypatch.setenv("MCP_PATH", "/private_abc123")

    client = TestClient(server.mcp.http_app())
    resp = client.get("/")

    assert resp.status_code == 200
    assert "unreachable" in resp.text.lower()
    assert "connection refused" in resp.text


def test_status_page_defaults_mcp_path_when_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "get_client", lambda: _FakeClient())
    monkeypatch.setattr(server, "_resolve_host_port", _fake_port)
    monkeypatch.delenv("MCP_PATH", raising=False)

    client = TestClient(server.mcp.http_app())
    resp = client.get("/")

    assert "/mcp" in resp.text
