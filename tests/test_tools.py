from __future__ import annotations

import pytest

from esphome_mcp import server
from esphome_mcp.client import ESPHomeClient, ESPHomeSettings, _strip_ansi, runtime_field

# --------------------------------------------------------------------------- unit


def test_derive_ws_url_https():
    assert (
        ESPHomeClient._derive_ws_url("https://esphome.example.com")
        == "wss://esphome.example.com/ws"
    )


def test_derive_ws_url_http_with_port_and_trailing_slash():
    assert ESPHomeClient._derive_ws_url("http://host.local:6052/") == "ws://host.local:6052/ws"


def test_strip_ansi():
    assert _strip_ansi("\x1b[32mINFO\x1b[0m done") == "INFO done"


def test_format_validation_valid():
    output, code = ESPHomeClient._format_validation({"yaml_errors": [], "validation_errors": []})
    assert code == 0
    assert "valid" in output.lower()


def test_format_validation_yaml_error():
    output, code = ESPHomeClient._format_validation(
        {"yaml_errors": [{"message": "mapping values are not allowed"}], "validation_errors": []}
    )
    assert code == 1
    assert "YAML error" in output
    assert "mapping values" in output


def test_format_validation_component_error_with_location():
    output, code = ESPHomeClient._format_validation(
        {
            "yaml_errors": [],
            "validation_errors": [
                {"range": {"start_line": 1, "start_col": 2}, "message": "Platform missing."}
            ],
        }
    )
    assert code == 1
    assert "line 1, col 2" in output
    assert "Platform missing." in output


# ------------------------------------------------------------- device wire shapes

# Device Builder >= 1.5.0: monitor-observed fields live under runtime_state.
NESTED_DEVICE = {
    "name": "bike-outlet",
    "friendly_name": "Bike Outlet",
    "configuration": "bike-outlet.yaml",
    "address": "bike-outlet.local",
    "target_platform": "ESP32",
    "current_version": "2026.8.0",
    "update_available": True,
    "migration_available": True,
    "runtime_state": {
        "state": "online",
        "active_source": "mdns",
        "ip_addresses": ["192.168.1.40"],
        "deployed_version": "2026.6.2",
    },
}

# Device Builder <= 1.4.x: the same fields, flat.
FLAT_DEVICE = {
    "name": "bike-outlet",
    "friendly_name": "Bike Outlet",
    "configuration": "bike-outlet.yaml",
    "address": "bike-outlet.local",
    "current_version": "2026.8.0",
    "state": "online",
    "deployed_version": "2026.6.2",
}


@pytest.mark.parametrize("device", [NESTED_DEVICE, FLAT_DEVICE])
def test_runtime_field_reads_both_wire_shapes(device):
    assert runtime_field(device, "state") == "online"
    assert runtime_field(device, "deployed_version") == "2026.6.2"


def test_runtime_field_prefers_nested_over_flat():
    """A dashboard serving both must not be read from the stale flat copy."""
    device = {"state": "offline", "runtime_state": {"state": "online"}}
    assert runtime_field(device, "state") == "online"


def test_runtime_field_missing_key_returns_default():
    assert runtime_field({"runtime_state": {}}, "state", "unknown") == "unknown"
    assert runtime_field({}, "ip_addresses", []) == []


# --------------------------------------------------------------- tool rendering


class _StubClient:
    """Stands in for ESPHomeClient in the server's tool layer."""

    def __init__(self, devices):
        self._devices = devices

    async def get_configured_devices(self):
        return self._devices

    async def get_version(self):
        return "2026.8.0"

    async def ping(self):
        return None


@pytest.fixture
def stub_devices(monkeypatch):
    def _install(devices):
        client = _StubClient(devices)
        monkeypatch.setattr(server, "get_client", lambda: client)
        return client

    return _install


@pytest.mark.asyncio
@pytest.mark.parametrize("device", [NESTED_DEVICE, FLAT_DEVICE])
async def test_list_devices_renders_real_status_and_version(stub_devices, device):
    stub_devices([device])
    out = await server.list_devices()
    assert "Status: online" in out
    assert "Deployed version: 2026.6.2" in out
    assert "Status: unknown" not in out


@pytest.mark.asyncio
async def test_list_devices_surfaces_dashboard_flags(stub_devices):
    stub_devices([NESTED_DEVICE])
    out = await server.list_devices()
    assert "ESPHome update available" in out
    assert "YAML migration available" in out


@pytest.mark.asyncio
async def test_get_device_status_reports_ip_and_source(stub_devices):
    stub_devices([NESTED_DEVICE])
    out = await server.get_device_status("bike-outlet")
    assert "online" in out
    assert "192.168.1.40" in out
    assert "mdns" in out


@pytest.mark.asyncio
async def test_check_device_update_uses_dashboard_verdict(stub_devices):
    stub_devices([NESTED_DEVICE])
    out = await server.check_device_update("bike-outlet")
    assert "Update available" in out
    assert "2026.6.2" in out


@pytest.mark.asyncio
async def test_check_device_update_up_to_date_flags_pending_migration(stub_devices):
    device = {**NESTED_DEVICE, "update_available": False}
    stub_devices([device])
    out = await server.check_device_update("bike-outlet")
    assert "Up to date at version 2026.6.2" in out
    assert "migrate_device_configuration" in out


# --------------------------------------------------------------- install chain


def _client() -> ESPHomeClient:
    return ESPHomeClient(ESPHomeSettings(esphome_dashboard_url="http://dash.local:6052"))


@pytest.mark.asyncio
async def test_install_follows_dependent_upload_job(monkeypatch):
    """firmware/install returns the COMPILE job; the flash is a dependent job."""
    client = _client()
    followed: list[str] = []

    async def fake_send(command, args=None, timeout=30.0):
        if command == "firmware/install":
            return {"job_id": "compile-1"}
        if command == "firmware/get_jobs":
            return [{"job_id": "upload-1", "job_type": "upload", "depends_on": "compile-1"}]
        raise AssertionError(command)

    async def fake_stream(job_id, on_line=None, timeout=1200.0):
        followed.append(job_id)
        return (f"[{job_id}]", 0, {"job_id": job_id})

    monkeypatch.setattr(client, "_send", fake_send)
    monkeypatch.setattr(client, "_stream_job", fake_stream)

    outcome = await client.install_configuration("bike-outlet.yaml")
    assert followed == ["compile-1", "upload-1"]
    assert outcome.exit_code == 0
    assert outcome.stage == "upload"
    assert outcome.deferred is False


@pytest.mark.asyncio
async def test_install_reports_upload_failure_not_compile_success(monkeypatch):
    client = _client()

    async def fake_send(command, args=None, timeout=30.0):
        if command == "firmware/install":
            return {"job_id": "compile-1"}
        return [{"job_id": "upload-1", "job_type": "upload", "depends_on": "compile-1"}]

    async def fake_stream(job_id, on_line=None, timeout=1200.0):
        code = 0 if job_id == "compile-1" else 1
        return (f"[{job_id}]", code, {"job_id": job_id})

    monkeypatch.setattr(client, "_send", fake_send)
    monkeypatch.setattr(client, "_stream_job", fake_stream)

    outcome = await client.install_configuration("bike-outlet.yaml")
    assert outcome.exit_code == 1
    assert outcome.stage == "upload"
    assert "OTA upload" in server._format_install_outcome("Install", "Bike Outlet", outcome)


@pytest.mark.asyncio
async def test_offline_device_reports_deferred_not_success(monkeypatch):
    """An offline OTA target gets a compile-only job; nothing is flashed."""
    client = _client()

    async def fake_send(command, args=None, timeout=30.0):
        if command == "firmware/install":
            return {"job_id": "compile-1", "is_deferred_install": True}
        return []

    async def fake_stream(job_id, on_line=None, timeout=1200.0):
        return ("compiled", 0, {"job_id": job_id, "is_deferred_install": True})

    monkeypatch.setattr(client, "_send", fake_send)
    monkeypatch.setattr(client, "_stream_job", fake_stream)

    outcome = await client.install_configuration("bike-outlet.yaml")
    assert outcome.deferred is True
    rendered = server._format_install_outcome("Install", "Bike Outlet", outcome)
    assert "FLASH DEFERRED" in rendered
    assert "SUCCESS" not in rendered


@pytest.mark.asyncio
async def test_install_without_dependent_job_falls_back_to_compile(monkeypatch):
    """Pre-chain dashboards install in a single job."""
    client = _client()

    async def fake_send(command, args=None, timeout=30.0):
        if command == "firmware/install":
            return {"job_id": "job-1"}
        return []

    async def fake_stream(job_id, on_line=None, timeout=1200.0):
        return ("done", 0, {"job_id": job_id})

    monkeypatch.setattr(client, "_send", fake_send)
    monkeypatch.setattr(client, "_stream_job", fake_stream)

    outcome = await client.install_configuration("bike-outlet.yaml")
    assert outcome.exit_code == 0
    assert outcome.stage == "compile"
    assert "SUCCESS" in server._format_install_outcome("Install", "Bike Outlet", outcome)


# ---------------------------------------------------------------------------- live


@pytest.mark.asyncio
@pytest.mark.live
async def test_live_read_and_validate(live_dashboard_url: str):
    """Against a real 2026.6 dashboard: list a device, read its YAML, validate it."""
    client = ESPHomeClient(ESPHomeSettings(esphome_dashboard_url=live_dashboard_url))
    try:
        devices = await client.get_configured_devices()
        assert devices, "no configured devices on the dashboard"
        filename = devices[0]["configuration"]

        yaml_content = await client.get_configuration(filename)
        # The exact bug we fixed: this must be real YAML, not the SPA HTML shell.
        assert "esphome:" in yaml_content
        assert "<!doctype html" not in yaml_content.lower()

        output, code = await client.validate_configuration(filename)
        assert code == 0, f"expected valid config, got: {output}"
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.live
async def test_live_device_carries_runtime_state(live_dashboard_url: str):
    """Pin the wire shape so the next Device Builder reshuffle fails a test.

    Device Builder 1.5.0 moved ``state`` / ``deployed_version`` under
    ``runtime_state`` with no flat alias, and the tools silently reported
    "unknown" for every device until it was noticed.
    """
    client = ESPHomeClient(ESPHomeSettings(esphome_dashboard_url=live_dashboard_url))
    try:
        devices = await client.get_configured_devices()
        assert devices, "no configured devices on the dashboard"
        device = devices[0]
        assert "runtime_state" in device or "state" in device, (
            f"neither nested nor flat state on the wire: {sorted(device)}"
        )
        assert runtime_field(device, "state") in {"online", "offline", "unknown"}
    finally:
        await client.close()


# ------------------------------------------------------- older-dashboard fallback


def test_unsupported_command_explains_the_version():
    from esphome_mcp.client import DashboardError

    msg = server._unsupported(
        DashboardError("Unknown command: editor/migrate_config", "unknown_command"),
        "one-click config migration",
        "Device Builder 1.8.0",
    )
    assert msg is not None
    assert "does not support" in msg
    assert "Device Builder 1.8.0" in msg


def test_unsupported_passes_other_errors_through():
    from esphome_mcp.client import DashboardError

    assert server._unsupported(DashboardError("boom", "not_found"), "x", "y") is None
    assert server._unsupported(RuntimeError("boom"), "x", "y") is None
