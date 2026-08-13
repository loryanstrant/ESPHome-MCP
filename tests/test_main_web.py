from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from esphome_mcp import __main__ as main_module


def test_main_web_passes_resolved_path_to_mcp_run(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main_module.addon, "resolve_mcp_path", lambda: "/private_abc123")
    monkeypatch.setattr(main_module, "_configure_logging", lambda: None)
    monkeypatch.setattr(main_module, "_check_connectivity_background", lambda: None)
    run_mock = MagicMock()
    monkeypatch.setattr(main_module.mcp, "run", run_mock)

    main_module.main_web()

    run_mock.assert_called_once_with(
        transport="http", host="0.0.0.0", port=8080, path="/private_abc123"
    )


def test_main_web_defaults_path_to_mcp(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main_module.addon, "resolve_mcp_path", lambda: "/mcp")
    monkeypatch.setattr(main_module, "_configure_logging", lambda: None)
    monkeypatch.setattr(main_module, "_check_connectivity_background", lambda: None)
    run_mock = MagicMock()
    monkeypatch.setattr(main_module.mcp, "run", run_mock)

    main_module.main_web()

    run_mock.assert_called_once_with(transport="http", host="0.0.0.0", port=8080, path="/mcp")


def test_main_web_configures_logging_before_resolving_mcp_path(
    monkeypatch: pytest.MonkeyPatch,
):
    """Regression test: logging must be configured before resolve_mcp_path() runs,
    or any INFO/WARNING it logs (options merge, secret-path resolution) is
    silently dropped by Python's no-handler-yet fallback — as happened on a real
    HA instance, where a failed discovery attempt left zero trace in the logs."""
    call_order: list[str] = []
    monkeypatch.setattr(
        main_module.addon,
        "resolve_mcp_path",
        lambda: call_order.append("resolve_mcp_path") or "/mcp",
    )
    monkeypatch.setattr(
        main_module, "_configure_logging", lambda: call_order.append("_configure_logging")
    )
    monkeypatch.setattr(main_module, "_check_connectivity_background", lambda: None)
    monkeypatch.setattr(main_module.mcp, "run", MagicMock())

    main_module.main_web()

    assert call_order.index("_configure_logging") < call_order.index("resolve_mcp_path")


def test_main_web_starts_server_without_waiting_on_connectivity_check(
    monkeypatch: pytest.MonkeyPatch,
):
    """Regression test: the HTTP server (and the add-on's ingress status page)
    must come up immediately, not after the connectivity check finishes --
    otherwise a user whose dashboard is unreachable can't see *why* until the
    whole retry window elapses (or, before an earlier fix, ever). Verified live
    on a real HA instance."""
    monkeypatch.setattr(main_module.addon, "resolve_mcp_path", lambda: "/mcp")
    monkeypatch.setattr(main_module, "_configure_logging", lambda: None)

    call_order: list[str] = []
    monkeypatch.setattr(
        main_module,
        "_check_connectivity_background",
        lambda: call_order.append("_check_connectivity_background"),
    )
    monkeypatch.setattr(
        main_module.mcp, "run", MagicMock(side_effect=lambda **kw: call_order.append("mcp.run"))
    )

    main_module.main_web()

    assert call_order == ["_check_connectivity_background", "mcp.run"]


def test_check_connectivity_background_spawns_daemon_thread_non_fatal(
    monkeypatch: pytest.MonkeyPatch,
):
    received: list[bool] = []
    done = threading.Event()

    def fake_check_connectivity(fatal: bool = True) -> None:
        received.append(fatal)
        done.set()

    monkeypatch.setattr(main_module, "_check_connectivity", fake_check_connectivity)

    main_module._check_connectivity_background()

    assert done.wait(timeout=2), "background thread never ran _check_connectivity"
    assert received == [False]


def test_check_connectivity_fatal_raises_after_exhausting_retries(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(main_module, "_RETRY_DELAY", 0)
    monkeypatch.setattr(main_module, "_MAX_RETRIES", 1)

    class _FakeClient:
        async def get_version(self) -> str:
            raise RuntimeError("connection refused")

    monkeypatch.setattr(main_module, "get_client", lambda: _FakeClient())

    with pytest.raises(SystemExit):
        main_module._check_connectivity(fatal=True)


def test_check_connectivity_non_fatal_returns_after_exhausting_retries(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(main_module, "_RETRY_DELAY", 0)
    monkeypatch.setattr(main_module, "_MAX_RETRIES", 1)

    class _FakeClient:
        async def get_version(self) -> str:
            raise RuntimeError("connection refused")

    monkeypatch.setattr(main_module, "get_client", lambda: _FakeClient())

    main_module._check_connectivity(fatal=False)  # must not raise
