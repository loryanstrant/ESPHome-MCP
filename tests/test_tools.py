from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.conftest import BIKE_OUTLET_YAML, SECRETS_YAML

if TYPE_CHECKING:
    from pathlib import Path

    from fastmcp.client import Client


@pytest.mark.asyncio
async def test_list_devices(mcp_client: Client) -> None:
    """list_devices should return both configured devices and ESPHome version."""
    result = await mcp_client.call_tool("list_devices", {})
    text = result.content[0].text

    assert "bike-outlet" in text.lower() or "Bike Outlet" in text
    assert "garage-sensor" in text.lower() or "Garage Sensor" in text
    assert "device(s)" in text


@pytest.mark.asyncio
async def test_list_device_names(mcp_client: Client) -> None:
    """list_device_names should return only device names, one per line."""
    result = await mcp_client.call_tool("list_device_names", {})
    text = result.content[0].text

    names = text.strip().split("\n")
    assert "bike-outlet" in names
    assert "garage-sensor" in names
    # Should not contain verbose details
    assert "Config:" not in text
    assert "Platform:" not in text


@pytest.mark.asyncio
async def test_list_devices_contains_platform(mcp_client: Client) -> None:
    """list_devices should include platform information."""
    result = await mcp_client.call_tool("list_devices", {})
    text = result.content[0].text

    # At least one device should show platform info
    assert "esp8266" in text.lower() or "esp32" in text.lower() or "Platform:" in text


@pytest.mark.asyncio
async def test_check_device_update_known_device(mcp_client: Client) -> None:
    """check_device_update should return a version status for a known device."""
    result = await mcp_client.call_tool("check_device_update", {"device_name": "bike-outlet"})
    text = result.content[0].text

    # Device hasn't been flashed, so expect "no deployed version" or similar
    assert "bike" in text.lower() or "Bike Outlet" in text
    assert "version" in text.lower()


@pytest.mark.asyncio
async def test_check_device_update_not_found(mcp_client: Client) -> None:
    """check_device_update should list available devices when name is wrong."""
    result = await mcp_client.call_tool(
        "check_device_update", {"device_name": "nonexistent-device"}
    )
    text = result.content[0].text

    assert "not found" in text.lower()
    assert "bike-outlet" in text


@pytest.mark.asyncio
async def test_get_device_configuration(mcp_client: Client) -> None:
    """get_device_configuration should return the YAML config content."""
    result = await mcp_client.call_tool("get_device_configuration", {"device_name": "bike-outlet"})
    text = result.content[0].text

    assert "name: bike-outlet" in text
    assert "esp8266" in text or "esp01_1m" in text
    assert "cse7766" in text


@pytest.mark.asyncio
async def test_get_device_configuration_second_device(mcp_client: Client) -> None:
    """get_device_configuration should work for the second device too."""
    result = await mcp_client.call_tool(
        "get_device_configuration", {"device_name": "garage-sensor"}
    )
    text = result.content[0].text

    assert "name: garage-sensor" in text
    assert "dht" in text


@pytest.mark.asyncio
async def test_get_device_configuration_not_found(mcp_client: Client) -> None:
    """get_device_configuration should return error for unknown device."""
    result = await mcp_client.call_tool("get_device_configuration", {"device_name": "nonexistent"})
    text = result.content[0].text

    assert "not found" in text.lower()


@pytest.mark.asyncio
async def test_get_device_configuration_output_path(mcp_client: Client, tmp_path: Path) -> None:
    """get_device_configuration should write the config to a local file when given output_path."""
    out_file = tmp_path / "nested" / "bike-outlet.yaml"

    result = await mcp_client.call_tool(
        "get_device_configuration",
        {"device_name": "bike-outlet", "output_path": str(out_file)},
    )
    text = result.content[0].text

    # The tool returns a confirmation, not the YAML itself.
    assert "written to" in text.lower()
    assert str(out_file) in text
    # The file (and its parent directory) should have been created with the config.
    assert out_file.is_file()
    written = out_file.read_text()
    assert "name: bike-outlet" in written
    assert "cse7766" in written


@pytest.mark.asyncio
async def test_get_device_logs_offline_device(mcp_client: Client) -> None:
    """get_device_logs should handle offline device gracefully."""
    result = await mcp_client.call_tool(
        "get_device_logs", {"device_name": "bike-outlet", "duration": 2}
    )
    text = result.content[0].text

    # Device is offline so we expect either an error message or "no output" message
    assert len(text) > 0


@pytest.mark.asyncio
async def test_get_device_status(mcp_client: Client) -> None:
    """get_device_status should return status for a known device."""
    result = await mcp_client.call_tool("get_device_status", {"device_name": "bike-outlet"})
    text = result.content[0].text

    assert "bike" in text.lower() or "Bike Outlet" in text
    assert "address" in text.lower()


@pytest.mark.asyncio
async def test_get_device_status_not_found(mcp_client: Client) -> None:
    """get_device_status should return error for unknown device."""
    result = await mcp_client.call_tool("get_device_status", {"device_name": "nonexistent"})
    text = result.content[0].text

    assert "not found" in text.lower()


@pytest.mark.asyncio
async def test_check_device_update_case_insensitive(mcp_client: Client) -> None:
    """Tools should resolve devices by name case-insensitively."""
    result = await mcp_client.call_tool("check_device_update", {"device_name": "Bike-Outlet"})
    text = result.content[0].text

    # Should resolve successfully via case-insensitive match on "name"
    assert "not found" not in text.lower()
    assert "version" in text.lower()


@pytest.mark.asyncio
async def test_get_device_version(mcp_client: Client) -> None:
    """get_device_version should return version info for a known device."""
    result = await mcp_client.call_tool("get_device_version", {"device_name": "bike-outlet"})
    text = result.content[0].text

    assert "bike" in text.lower() or "Bike Outlet" in text
    assert "version" in text.lower()


@pytest.mark.asyncio
async def test_get_device_version_not_found(mcp_client: Client) -> None:
    """get_device_version should return error for unknown device."""
    result = await mcp_client.call_tool("get_device_version", {"device_name": "nonexistent"})
    text = result.content[0].text

    assert "not found" in text.lower()


@pytest.mark.asyncio
async def test_get_esphome_schema_list_components(mcp_client: Client) -> None:
    """get_esphome_schema without component should list available components."""
    result = await mcp_client.call_tool("get_esphome_schema", {"version": "2025.8.0"})
    text = result.content[0].text

    assert "components" in text.lower()
    assert "sensor" in text
    assert "wifi" in text


@pytest.mark.asyncio
async def test_get_esphome_schema_specific_component(mcp_client: Client) -> None:
    """get_esphome_schema with component should return JSON schema."""
    result = await mcp_client.call_tool(
        "get_esphome_schema", {"version": "2025.8.0", "component": "sensor"}
    )
    text = result.content[0].text

    # Should be valid JSON schema content
    assert "sensor" in text.lower() or "{" in text


@pytest.mark.asyncio
async def test_get_esphome_schema_invalid_component(mcp_client: Client) -> None:
    """get_esphome_schema with invalid component should return error."""
    result = await mcp_client.call_tool(
        "get_esphome_schema", {"version": "2025.8.0", "component": "nonexistent_component"}
    )
    text = result.content[0].text

    assert "not found" in text.lower()


# --- Write tool tests ---


@pytest.mark.asyncio
async def test_validate_device_configuration(mcp_client: Client) -> None:
    """validate_device_configuration should return validation output for a known device."""
    result = await mcp_client.call_tool(
        "validate_device_configuration", {"device_or_path": "bike-outlet"}
    )
    text = result.content[0].text

    assert "validation result" in text.lower()
    assert len(text) > 0


@pytest.mark.asyncio
async def test_validate_device_configuration_not_found(mcp_client: Client) -> None:
    """validate_device_configuration should return error for unknown device."""
    result = await mcp_client.call_tool(
        "validate_device_configuration", {"device_or_path": "nonexistent"}
    )
    text = result.content[0].text

    assert "not found" in text.lower()


@pytest.mark.asyncio
async def test_validate_device_configuration_local_file(
    mcp_client: Client, tmp_path: Path
) -> None:
    """validate_device_configuration should validate a local YAML file by path."""
    config_file = tmp_path / "bike-outlet.yaml"
    config_file.write_text(BIKE_OUTLET_YAML)
    (tmp_path / "secrets.yaml").write_text(SECRETS_YAML)

    result = await mcp_client.call_tool(
        "validate_device_configuration", {"device_or_path": str(config_file)}
    )
    text = result.content[0].text

    assert "Validation result: VALID" in text


@pytest.mark.asyncio
async def test_validate_device_configuration_local_file_missing(
    mcp_client: Client, tmp_path: Path
) -> None:
    """validate_device_configuration should report a missing local file path."""
    missing = tmp_path / "does-not-exist.yaml"

    result = await mcp_client.call_tool(
        "validate_device_configuration", {"device_or_path": str(missing)}
    )
    text = result.content[0].text

    assert "not found" in text.lower()


@pytest.mark.asyncio
async def test_edit_device_configuration(mcp_client: Client) -> None:
    """edit_device_configuration should save and validate a modified config."""
    # Modify the friendly name
    modified_yaml = BIKE_OUTLET_YAML.replace(
        "friendly_name: Bike Outlet", "friendly_name: Bike Outlet Modified"
    )

    result = await mcp_client.call_tool(
        "edit_device_configuration",
        {"device_name": "bike-outlet", "yaml_content": modified_yaml},
    )
    text = result.content[0].text

    assert "saved" in text.lower()
    assert "validation result" in text.lower()

    # Verify the change persisted
    read_result = await mcp_client.call_tool(
        "get_device_configuration", {"device_name": "bike-outlet"}
    )
    read_text = read_result.content[0].text
    assert "Bike Outlet Modified" in read_text

    # Restore original config
    await mcp_client.call_tool(
        "edit_device_configuration",
        {"device_name": "bike-outlet", "yaml_content": BIKE_OUTLET_YAML},
    )


@pytest.mark.asyncio
async def test_edit_device_configuration_not_found(mcp_client: Client) -> None:
    """edit_device_configuration should return error for unknown device."""
    result = await mcp_client.call_tool(
        "edit_device_configuration",
        {"device_name": "nonexistent", "yaml_content": "esphome:\n  name: test\n"},
    )
    text = result.content[0].text

    assert "not found" in text.lower()


@pytest.mark.asyncio
async def test_edit_device_configuration_from_path(mcp_client: Client, tmp_path: Path) -> None:
    """edit_device_configuration should read YAML from a local file via config_path."""
    modified_yaml = BIKE_OUTLET_YAML.replace(
        "friendly_name: Bike Outlet", "friendly_name: Bike Outlet From File"
    )
    config_file = tmp_path / "bike-outlet.yaml"
    config_file.write_text(modified_yaml)

    result = await mcp_client.call_tool(
        "edit_device_configuration",
        {"device_name": "bike-outlet", "config_path": str(config_file)},
    )
    text = result.content[0].text

    assert "saved" in text.lower()
    assert "validation result" in text.lower()

    # Verify the file contents were applied to the device.
    read_result = await mcp_client.call_tool(
        "get_device_configuration", {"device_name": "bike-outlet"}
    )
    assert "Bike Outlet From File" in read_result.content[0].text

    # Restore original config.
    await mcp_client.call_tool(
        "edit_device_configuration",
        {"device_name": "bike-outlet", "yaml_content": BIKE_OUTLET_YAML},
    )


@pytest.mark.asyncio
async def test_edit_device_configuration_requires_one_source(mcp_client: Client) -> None:
    """edit_device_configuration should reject calls with neither or both content sources."""
    neither = await mcp_client.call_tool(
        "edit_device_configuration", {"device_name": "bike-outlet"}
    )
    assert "exactly one" in neither.content[0].text.lower()

    both = await mcp_client.call_tool(
        "edit_device_configuration",
        {
            "device_name": "bike-outlet",
            "yaml_content": BIKE_OUTLET_YAML,
            "config_path": "/tmp/whatever.yaml",
        },
    )
    assert "exactly one" in both.content[0].text.lower()


@pytest.mark.asyncio
async def test_install_device_configuration(mcp_client: Client) -> None:
    """install_device_configuration should attempt compile+upload (OTA will fail)."""
    result = await mcp_client.call_tool(
        "install_device_configuration", {"device_name": "bike-outlet"}
    )
    text = result.content[0].text

    # Compilation should start; OTA upload will fail since device is offline
    assert "install result" in text.lower()
    assert len(text) > 0


@pytest.mark.asyncio
async def test_update_device(mcp_client: Client) -> None:
    """update_device should attempt compile+upload (OTA will fail)."""
    result = await mcp_client.call_tool("update_device", {"device_name": "bike-outlet"})
    text = result.content[0].text

    assert "update result" in text.lower()
    assert len(text) > 0


@pytest.mark.asyncio
async def test_update_device_not_found(mcp_client: Client) -> None:
    """update_device should return error for unknown device."""
    result = await mcp_client.call_tool("update_device", {"device_name": "nonexistent"})
    text = result.content[0].text

    assert "not found" in text.lower()
