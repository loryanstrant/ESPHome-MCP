from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from esphome_mcp.client import fetch_schema, get_client, validate_local_configuration

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
This server provides access to an ESPHome dashboard, with tools for reading device \
information and modifying device configurations.

## Workflow

1. **Always start by calling `list_device_names`** to get the list of known device names. \
Device names must match exactly (case-insensitive), so confirm the name against this list \
before passing it to any other tool.

2. Once you have a valid device name, use the read tools as needed:
   - `list_devices` — detailed info on all devices (versions, status, addresses, platform)
   - `check_device_update` — check if a firmware update is available
   - `get_device_status` — check if a device is online or offline
   - `get_device_version` — get the deployed and current firmware versions
   - `get_device_configuration` — view the full YAML configuration, or save it to \
a local file with `output_path`
   - `get_device_logs` — stream recent logs (default 10s, max 30s). \
The device must be online for logs to be available.

3. To look up ESPHome configuration schema:
   - `get_esphome_schema(version)` — list available components for a version
   - `get_esphome_schema(version, component)` — get the JSON schema for a specific component
   - Use `get_device_version` to find the version a device is running, then fetch the \
matching schema.

4. To modify a device configuration:
   - First read the current config with `get_device_configuration`
   - Make your changes to the YAML
   - Save with `edit_device_configuration` — pass the YAML inline via `yaml_content`, \
or a local file path via `config_path`. This saves AND validates, reporting any errors
   - Ensure edits conform to the ESPHome schema (use `get_esphome_schema` to check)
   - If validation passes, flash with `install_device_configuration`

5. To validate without saving:
   - Use `validate_device_configuration` with a device name to check a device's saved \
config, or with a local YAML file path to validate that file

6. To update a device to the latest ESPHome version:
   - Check for updates with `check_device_update`
   - If an update is available, use `update_device` to recompile and flash

## ESPHome documentation
- Components: https://esphome.io/components/
- Guides: https://esphome.io/guides/
- Cookbook (example configs): https://esphome.io/cookbook/
- Changelog: https://esphome.io/changelog

## Important notes
- Device names are the ESPHome `name` field (e.g. "bike-outlet"), not the friendly name.
- If a tool returns "not found", re-check the name with `list_device_names`.
- `install_device_configuration` and `update_device` are destructive — they compile and \
flash firmware to a physical device. The device must be online for OTA upload.
"""

mcp = FastMCP(
    name="ESPHome MCP",
    instructions=INSTRUCTIONS,
)


async def _resolve_device(device_name: str) -> dict[str, Any] | str:
    """Resolve a device name to its entry dict.

    Returns the device dict on success, or an error string if not found.
    """
    logger.debug("Resolving device name=%r", device_name)
    devices = await get_client().get_configured_devices()
    name_lower = device_name.lower()
    for device in devices:
        if (
            device.get("name", "").lower() == name_lower
            or device.get("friendly_name", "").lower() == name_lower
        ):
            logger.debug("Resolved %r to device config=%r", device_name, device.get("name"))
            return device

    available = [d.get("name", "unknown") for d in devices]
    logger.warning("Device %r not found. Available: %s", device_name, available)
    return f"Device '{device_name}' not found. Available devices: {', '.join(available)}"


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
    }
)
async def list_devices() -> str:
    """List all devices configured in the ESPHome dashboard.

    Returns device names, versions, addresses, and online status.
    """
    logger.info("Listing all devices")
    try:
        devices = await get_client().get_configured_devices()
    except Exception as e:
        logger.error("Failed to fetch devices: %s", e)
        return f"Error fetching devices: {e}"

    if not devices:
        logger.info("No devices found")
        return "No devices found in the ESPHome dashboard."

    logger.info("Found %d device(s)", len(devices))

    lines: list[str] = []
    for d in devices:
        name = d.get("friendly_name") or d.get("name", "unknown")
        config = d.get("configuration", "")
        deployed = d.get("deployed_version", "n/a")
        current = d.get("current_version", "n/a")
        address = d.get("address", "n/a")
        platform = d.get("target_platform", "n/a")

        status = d.get("state") or d.get("status", "unknown")

        lines.append(
            f"- {name}\n"
            f"  Config: {config}\n"
            f"  Status: {status}\n"
            f"  Deployed version: {deployed}\n"
            f"  Current version: {current}\n"
            f"  Address: {address}\n"
            f"  Platform: {platform}"
        )

    version = "unknown"
    with contextlib.suppress(Exception):
        version = await get_client().get_version()

    header = f"ESPHome version: {version}\n{len(devices)} device(s):\n"
    return header + "\n".join(lines)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
    }
)
async def list_device_names() -> str:
    """List the names of all devices configured in the ESPHome dashboard.

    Returns only device names, one per line.
    """
    logger.info("Listing device names")
    try:
        devices = await get_client().get_configured_devices()
    except Exception as e:
        logger.error("Failed to fetch devices: %s", e)
        return f"Error fetching devices: {e}"

    if not devices:
        logger.info("No devices found")
        return "No devices found in the ESPHome dashboard."

    names = [d.get("name", "unknown") for d in devices]
    logger.info("Found %d device(s): %s", len(names), names)
    return "\n".join(names)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
    }
)
async def check_device_update(device_name: str) -> str:
    """Check if a firmware update is available for an ESPHome device.

    Args:
        device_name: The name of the device (as shown in list_devices).
    """
    logger.info("Checking update for device=%r", device_name)
    try:
        result = await _resolve_device(device_name)
    except Exception as e:
        logger.error("Failed to resolve device %r: %s", device_name, e)
        return f"Error: {e}"

    if isinstance(result, str):
        return result

    device = result
    name = device.get("friendly_name") or device.get("name", "unknown")
    deployed = device.get("deployed_version", "")
    current = device.get("current_version", "")

    if not deployed:
        logger.info("Device %r has no deployed version", name)
        return f"{name}: No deployed version found. The device may not have been flashed yet."

    if not current:
        logger.info("Device %r: cannot determine available version", name)
        return f"{name}: Running version {deployed}. Unable to determine if an update is available."

    if deployed != current:
        logger.info("Device %r: update available %s -> %s", name, deployed, current)
        return f"{name}: Update available! Running {deployed}, latest is {current}."

    logger.info("Device %r: up to date at %s", name, deployed)
    return f"{name}: Up to date at version {deployed}."


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
    }
)
async def get_device_status(device_name: str) -> str:
    """Check whether an ESPHome device is online or offline.

    Triggers a ping refresh and returns the current status.

    Args:
        device_name: The name of the device to check.
    """
    logger.info("Checking status for device=%r", device_name)
    try:
        with contextlib.suppress(Exception):
            await get_client().ping()
        result = await _resolve_device(device_name)
    except Exception as e:
        logger.error("Failed to get status for %r: %s", device_name, e)
        return f"Error: {e}"

    if isinstance(result, str):
        return result

    device = result
    name = device.get("friendly_name") or device.get("name", "unknown")
    status = device.get("state") or device.get("status", "unknown")
    address = device.get("address", "n/a")

    logger.info("Device %r status=%s address=%s", name, status, address)
    return f"{name}: {status} (address: {address})"


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
    }
)
async def get_device_version(device_name: str) -> str:
    """Get the ESPHome firmware version for a specific device.

    Returns the deployed version and the current (latest compiled) version.

    Args:
        device_name: The name of the device to check.
    """
    logger.info("Getting version for device=%r", device_name)
    try:
        result = await _resolve_device(device_name)
    except Exception as e:
        logger.error("Failed to resolve device %r: %s", device_name, e)
        return f"Error: {e}"

    if isinstance(result, str):
        return result

    device = result
    name = device.get("friendly_name") or device.get("name", "unknown")
    deployed = device.get("deployed_version", "")
    current = device.get("current_version", "")

    parts: list[str] = [f"{name}:"]
    if deployed:
        parts.append(f"  Deployed version: {deployed}")
    else:
        parts.append("  Deployed version: not yet flashed")
    if current:
        parts.append(f"  Current version: {current}")

    logger.info("Device %r deployed=%s current=%s", name, deployed, current)
    return "\n".join(parts)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
    }
)
async def get_esphome_schema(version: str, component: str | None = None) -> str:
    """Get the ESPHome configuration schema for a specific version.

    Returns the JSON schema used for validating ESPHome YAML configurations.
    If a component name is provided, returns only that component's schema.
    Otherwise returns the list of available component names.

    Args:
        version: ESPHome version (e.g. "2026.3.0").
        component: Optional component name (e.g. "sensor", "wifi", "esp32"). \
If omitted, returns the list of available components.
    """
    logger.info("Fetching schema version=%s component=%r", version, component)
    try:
        if component is None:
            schemas = await fetch_schema(version)
            assert isinstance(schemas, dict)
            names = sorted(schemas.keys())
            logger.info("Schema %s has %d components", version, len(names))
            return f"ESPHome {version} schema — {len(names)} components:\n" + "\n".join(names)
        else:
            schema_json = await fetch_schema(version, component)
            assert isinstance(schema_json, str)
            logger.info(
                "Returned schema for %s/%s (%d bytes)", version, component, len(schema_json)
            )
            return schema_json
    except KeyError as e:
        return str(e)
    except Exception as e:
        logger.error("Failed to fetch schema %s/%s: %s", version, component, e)
        return f"Error fetching schema: {e}"


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
    }
)
async def get_device_configuration(device_name: str, output_path: str | None = None) -> str:
    """View the YAML configuration for an ESPHome device.

    Args:
        device_name: The name of the device whose configuration to view.
        output_path: Optional local file path. When provided, the configuration
            is written to this file (creating parent directories as needed) and a
            confirmation is returned instead of the YAML content.
    """
    logger.info("Fetching configuration for device=%r", device_name)
    try:
        result = await _resolve_device(device_name)
    except Exception as e:
        logger.error("Failed to resolve device %r: %s", device_name, e)
        return f"Error: {e}"

    if isinstance(result, str):
        return result

    device = result
    filename = device.get("configuration", "")
    if not filename:
        logger.warning("Device %r has no configuration file", device_name)
        return f"No configuration file found for device '{device_name}'."

    try:
        logger.debug("Fetching config file=%r", filename)
        yaml_content = await get_client().get_configuration(filename)
    except Exception as e:
        logger.error("Failed to fetch configuration %r: %s", filename, e)
        return f"Error fetching configuration: {e}"

    if output_path is not None:
        try:
            dest = Path(output_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(yaml_content, encoding="utf-8")
        except OSError as e:
            logger.error("Failed to write configuration to %r: %s", output_path, e)
            return f"Error writing configuration to {output_path}: {e}"
        logger.info(
            "Wrote configuration for %r to %s (%d bytes)",
            device_name,
            output_path,
            len(yaml_content),
        )
        return (
            f"Configuration for {device_name} written to {output_path} ({len(yaml_content)} bytes)."
        )

    logger.info("Returned configuration for %r (%d bytes)", device_name, len(yaml_content))
    return yaml_content


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
    }
)
async def get_device_logs(device_name: str, duration: int = 10) -> str:
    """View recent logs from an ESPHome device.

    Connects to the device and collects log output for the specified duration.

    Args:
        device_name: The name of the device to get logs from.
        duration: How many seconds to collect logs (default: 10, max: 30).
    """
    duration = max(1, min(30, duration))
    logger.info("Fetching logs for device=%r duration=%ds", device_name, duration)

    try:
        result = await _resolve_device(device_name)
    except Exception as e:
        logger.error("Failed to resolve device %r: %s", device_name, e)
        return f"Error: {e}"

    if isinstance(result, str):
        return result

    device = result
    filename = device.get("configuration", "")
    if not filename:
        logger.warning("Device %r has no configuration file", device_name)
        return f"No configuration file found for device '{device_name}'."

    try:
        logger.debug("Connecting to log stream for %r via %r", device_name, filename)
        logs = await get_client().get_logs(filename, duration=float(duration))
    except Exception as e:
        logger.error("Failed to fetch logs for %r: %s", device_name, e)
        return f"Error fetching logs: {e}"

    if not logs.strip():
        logger.info("No log output from %r within %ds", device_name, duration)
        return (
            f"No log output received from '{device_name}' within {duration} seconds. "
            f"The device may be offline."
        )

    logger.info("Collected %d bytes of logs from %r", len(logs), device_name)
    return logs


async def _resolve_filename(device_name: str) -> tuple[dict[str, Any], str] | str:
    """Resolve a device name to its entry dict and configuration filename.

    Returns (device_dict, filename) on success, or an error string.
    """
    result = await _resolve_device(device_name)
    if isinstance(result, str):
        return result
    filename = result.get("configuration", "")
    if not filename:
        return f"No configuration file found for device '{device_name}'."
    return result, filename


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
    }
)
async def validate_device_configuration(device_or_path: str) -> str:
    """Validate an ESPHome configuration without modifying anything.

    Accepts either a device name or a path to a local YAML file:
    - Device name: validates the device's saved configuration on the dashboard.
    - Local file path: validates that file with the ESPHome validator (requires
      ``esphome`` to be installed in this server's environment).

    The argument is treated as a path when it contains a path separator or ends
    in ``.yaml``/``.yml``; otherwise it is treated as a device name.

    Args:
        device_or_path: A device name (as shown in list_device_names) or a path
            to a local ESPHome YAML configuration file.
    """
    logger.info("Validating configuration for %r", device_or_path)

    looks_like_path = (
        "/" in device_or_path
        or "\\" in device_or_path
        or device_or_path.endswith((".yaml", ".yml"))
    )

    if looks_like_path:
        if not Path(device_or_path).is_file():
            logger.warning("Configuration file not found: %r", device_or_path)
            return f"Configuration file not found: {device_or_path}"
        try:
            output, exit_code = await validate_local_configuration(device_or_path)
        except Exception as e:
            logger.error("Failed to validate local file %r: %s", device_or_path, e)
            return f"Error validating configuration: {e}"

        status = "VALID" if exit_code == 0 else "INVALID"
        logger.info("Local validation for %r: %s (exit_code=%d)", device_or_path, status, exit_code)
        return f"Validation result: {status}\n\n{output}"

    # Treat the argument as a device name and validate the saved dashboard config.
    try:
        resolved = await _resolve_filename(device_or_path)
    except Exception as e:
        logger.error("Failed to resolve device %r: %s", device_or_path, e)
        return f"Error: {e}"

    if isinstance(resolved, str):
        return resolved

    _device, filename = resolved

    try:
        output, exit_code = await get_client().validate_configuration(filename)
    except Exception as e:
        logger.error("Failed to validate %r: %s", device_or_path, e)
        return f"Error validating configuration: {e}"

    status = "VALID" if exit_code == 0 else "INVALID"
    logger.info("Validation for %r: %s (exit_code=%d)", device_or_path, status, exit_code)
    return f"Validation result: {status}\n\n{output}"


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
    }
)
async def edit_device_configuration(
    device_name: str,
    yaml_content: str | None = None,
    config_path: str | None = None,
) -> str:
    """Save a new YAML configuration for an ESPHome device.

    Provide the new configuration either inline via ``yaml_content`` or by
    pointing ``config_path`` at a local YAML file to read. Exactly one of the two
    must be supplied. The configuration is saved as the device's file and then
    automatically validated. The configuration is saved even if validation fails,
    so you can fix issues and re-save.

    **Workflow**: First read the current config with `get_device_configuration`,
    make your changes, then pass the complete modified YAML here (or a path to a
    file containing it). Ensure edits conform to the ESPHome schema (use
    `get_esphome_schema` to verify).

    Args:
        device_name: The name of the device whose configuration to edit.
        yaml_content: The complete YAML configuration content to save.
        config_path: Path to a local YAML file whose contents to save. Mutually
            exclusive with ``yaml_content``.
    """
    logger.info("Editing configuration for device=%r", device_name)

    if (yaml_content is None) == (config_path is None):
        return "Error: provide exactly one of 'yaml_content' or 'config_path'."

    if config_path is not None:
        try:
            yaml_content = Path(config_path).read_text(encoding="utf-8")
        except OSError as e:
            logger.error("Failed to read configuration file %r: %s", config_path, e)
            return f"Error reading configuration file {config_path}: {e}"
    assert yaml_content is not None

    try:
        resolved = await _resolve_filename(device_name)
    except Exception as e:
        logger.error("Failed to resolve device %r: %s", device_name, e)
        return f"Error: {e}"

    if isinstance(resolved, str):
        return resolved

    device, filename = resolved
    name = device.get("friendly_name") or device.get("name", "unknown")

    # Save the configuration
    try:
        await get_client().save_configuration(filename, yaml_content)
    except Exception as e:
        logger.error("Failed to save configuration for %r: %s", device_name, e)
        return f"Error saving configuration: {e}"

    logger.info("Configuration saved for %r, running validation", name)

    # Validate after saving
    try:
        output, exit_code = await get_client().validate_configuration(filename)
    except Exception as e:
        logger.warning("Configuration saved for %r but validation failed: %s", name, e)
        return f"Configuration saved for {name}.\n\nWarning: Could not run validation: {e}"

    status = "VALID" if exit_code == 0 else "INVALID"
    logger.info("Edit+validate for %r: %s (exit_code=%d)", name, status, exit_code)
    return f"Configuration saved for {name}.\n\nValidation result: {status}\n\n{output}"


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
    }
)
async def install_device_configuration(device_name: str) -> str:
    """Compile and flash the current configuration to an ESPHome device via OTA.

    This compiles the device's saved YAML configuration and uploads the firmware
    to the device over-the-air. The device must be online for OTA upload to succeed.
    This operation may take several minutes.

    Args:
        device_name: The name of the device to install the configuration on.
    """
    logger.info("Installing configuration for device=%r", device_name)
    try:
        resolved = await _resolve_filename(device_name)
    except Exception as e:
        logger.error("Failed to resolve device %r: %s", device_name, e)
        return f"Error: {e}"

    if isinstance(resolved, str):
        return resolved

    device, filename = resolved
    name = device.get("friendly_name") or device.get("name", "unknown")

    try:
        output, exit_code = await get_client().install_configuration(filename)
    except Exception as e:
        logger.error("Failed to install configuration for %r: %s", device_name, e)
        return f"Error installing configuration: {e}"

    if exit_code == 0:
        logger.info("Install for %r: SUCCESS", name)
        return f"Install result for {name}: SUCCESS"
    logger.info("Install for %r: FAILED (exit_code=%d)", name, exit_code)
    return f"Install result for {name}: FAILED\n\n{output}"


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
    }
)
async def update_device(device_name: str) -> str:
    """Update an ESPHome device to the latest firmware version.

    Recompiles the device's configuration with the current ESPHome version and
    flashes it via OTA. Use `check_device_update` first to verify an update is
    available. The device must be online for OTA upload to succeed.
    This operation may take several minutes.

    Args:
        device_name: The name of the device to update.
    """
    logger.info("Updating device=%r", device_name)
    try:
        resolved = await _resolve_filename(device_name)
    except Exception as e:
        logger.error("Failed to resolve device %r: %s", device_name, e)
        return f"Error: {e}"

    if isinstance(resolved, str):
        return resolved

    device, filename = resolved
    name = device.get("friendly_name") or device.get("name", "unknown")

    try:
        output, exit_code = await get_client().install_configuration(filename)
    except Exception as e:
        logger.error("Failed to update device %r: %s", device_name, e)
        return f"Error updating device: {e}"

    if exit_code == 0:
        logger.info("Update for %r: SUCCESS", name)
        return f"Update result for {name}: SUCCESS"
    logger.info("Update for %r: FAILED (exit_code=%d)", name, exit_code)
    return f"Update result for {name}: FAILED\n\n{output}"
