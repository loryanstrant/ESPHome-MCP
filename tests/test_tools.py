from __future__ import annotations

import pytest

from esphome_mcp.client import ESPHomeClient, ESPHomeSettings, _strip_ansi

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
