from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from esphome_mcp.client import (
    InstallOutcome,
    fetch_schema,
    get_client,
    runtime_field,
    validate_local_configuration,
)

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
   - `search_device_configurations` — search every device's YAML for a string, \
to answer "which devices use X?" without reading each config
   - `troubleshoot_device` — live connectivity probe (DNS, mDNS, ping) when a \
device shows as offline
   - `decode_device_backtrace` — turn a crash backtrace from the logs into \
source locations

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
   - ESPHome renames configuration keys between releases. If `list_devices` or \
`check_device_update` reports a migration is available, run \
`migrate_device_configuration` (a dry run by default) and apply it **before** \
installing — otherwise the compile may fail on legacy spellings
   - Then use `update_device` to recompile and flash

## ESPHome documentation
- Components: https://esphome.io/components/
- Guides: https://esphome.io/guides/
- Cookbook (example configs): https://esphome.io/cookbook/
- Changelog: https://esphome.io/changelog

## Important notes
- Device names are the ESPHome `name` field (e.g. "bike-outlet"), not the friendly name.
- If a tool returns "not found", re-check the name with `list_device_names`.
- `install_device_configuration` and `update_device` are destructive — they compile and \
flash firmware to a physical device. If the device is offline, the dashboard compiles the \
firmware and arms it to flash on the device's next check-in; the tool reports that as \
"FLASH DEFERRED" rather than success.
- A device's status and deployed version are observed over the network (mDNS/ping), so \
they read as "unknown" for a device the dashboard has not seen yet, even if it is fine.
"""

mcp = FastMCP(
    name="ESPHome MCP",
    instructions=INSTRUCTIONS,
)


def _unsupported(exc: Exception, feature: str, since: str) -> str | None:
    """Turn the dashboard's ``unknown_command`` into a version explanation.

    The Device Builder ships on its own release cadence, so a dashboard can be
    perfectly healthy and simply predate a command. Say which version added it
    rather than surfacing a bare "Unknown command".
    """
    if getattr(exc, "error_code", None) != "unknown_command":
        return None
    return (
        f"This ESPHome dashboard does not support {feature} — it was added in "
        f"{since}. Upgrade the dashboard to use this tool."
    )


def _format_migration_change(change: Any) -> str:
    """Render one ``MigrationChange`` from ``editor/migrate_config``.

    Fields are ``kind`` (``key`` / ``field`` / ``fold`` / ``convert`` / ``action``),
    ``scope``, ``old``, ``new``, and optionally ``since`` / ``removed_in`` /
    ``required``. ``required`` means the installed ESPHome already rejects the old
    spelling, so it is not merely tidy-up.
    """
    if not isinstance(change, dict):
        return str(change)

    old, new = change.get("old", ""), change.get("new", "")
    text = f"{old} -> {new}" if old and new else (new or old or change.get("kind", "change"))
    if scope := change.get("scope"):
        text = f"{scope}: {text}"

    notes = []
    if kind := change.get("kind"):
        notes.append(kind)
    if since := change.get("since"):
        notes.append(f"since {since}")
    if removed_in := change.get("removed_in"):
        notes.append(f"removed in {removed_in}")
    if change.get("required"):
        notes.append("REQUIRED — the installed ESPHome rejects the old spelling")
    return text + (f" ({', '.join(notes)})" if notes else "")


def _apply_yaml_diff(content: str, diff: dict[str, Any]) -> str:
    """Apply a ``YamlDiff`` splice to ``content``.

    ``fromLine`` / ``toLine`` are **1-indexed line numbers in the old text**, and the
    dashboard's own splice is ``lines[fromLine - 1 : toLine]`` replaced by
    ``replacement``. Two shapes share that one formula:

    * **replace** — ``fromLine <= toLine``: that inclusive line range is replaced.
    * **pure insert** — ``toLine == fromLine - 1``: nothing is replaced and
      ``replacement`` lands before ``fromLine``.

    Getting the off-by-one wrong does not fail loudly — it leaves the old line in
    place *next to* its replacement, which the dashboard then rejects with e.g.
    "'channel_colors' cannot be combined with 'rgb_order'".
    """
    from_line = diff.get("fromLine")
    to_line = diff.get("toLine")
    replacement = diff.get("replacement", "")
    if not isinstance(from_line, int) or not isinstance(to_line, int):
        raise ValueError(f"non-integer line range: fromLine={from_line!r} toLine={to_line!r}")

    lines = content.split("\n")
    if from_line < 1 or to_line < from_line - 1 or to_line > len(lines):
        raise ValueError(
            f"line range {from_line}-{to_line} is outside the {len(lines)}-line configuration"
        )

    # Trailing newline on the replacement would otherwise introduce a blank line,
    # since the surrounding lines are already newline-separated by the join.
    body = replacement[:-1] if replacement.endswith("\n") else replacement
    return "\n".join([*lines[: from_line - 1], *body.split("\n"), *lines[to_line:]])


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
        deployed = runtime_field(d, "deployed_version") or "n/a"
        current = d.get("current_version") or "n/a"
        address = d.get("address") or "n/a"
        platform = d.get("target_platform") or "n/a"
        status = runtime_field(d, "state") or "unknown"

        # Flags the dashboard computes itself — more reliable than comparing
        # version strings, and the only source for the migration hint.
        flags = []
        if d.get("update_available"):
            flags.append("ESPHome update available")
        if d.get("has_pending_changes"):
            flags.append("config changed since last compile")
        if d.get("migration_available"):
            flags.append("YAML migration available")

        entry = (
            f"- {name}\n"
            f"  Config: {config}\n"
            f"  Status: {status}\n"
            f"  Deployed version: {deployed}\n"
            f"  Current version: {current}\n"
            f"  Address: {address}\n"
            f"  Platform: {platform}"
        )
        if flags:
            entry += f"\n  Flags: {'; '.join(flags)}"
        lines.append(entry)

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
    deployed = runtime_field(device, "deployed_version")
    current = device.get("current_version", "")

    # The dashboard's own verdict: compiled against an older ESPHome than the
    # server runs. It distinguishes that from "compiled but not yet flashed",
    # which a deployed-vs-current string compare cannot.
    if device.get("update_available"):
        detail = f" Running {deployed}, latest is {current}." if deployed and current else ""
        logger.info("Device %r: update available (%s -> %s)", name, deployed, current)
        return f"{name}: Update available!{detail}"

    extra = []
    if device.get("has_pending_changes"):
        extra.append("its config has changed since the last compile — install to apply")
    if device.get("migration_available"):
        extra.append("its YAML uses legacy spellings — run migrate_device_configuration")
    suffix = f" Note: {'; '.join(extra)}." if extra else ""

    if not deployed:
        logger.info("Device %r has no deployed version", name)
        return (
            f"{name}: Up to date on ESPHome version, but no deployed version is known "
            f"— the device may not have been flashed yet, or has not been seen on the "
            f"network since the dashboard started.{suffix}"
        )

    logger.info("Device %r: up to date at %s", name, deployed)
    return f"{name}: Up to date at version {deployed}.{suffix}"


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
    status = runtime_field(device, "state") or "unknown"
    address = device.get("address") or "n/a"
    ips = runtime_field(device, "ip_addresses", []) or []
    source = runtime_field(device, "active_source")

    detail = f"(address: {address}"
    if ips:
        detail += f", IP: {', '.join(str(ip) for ip in ips)}"
    if source and source != "unknown":
        detail += f", seen via: {source}"
    detail += ")"

    logger.info("Device %r status=%s address=%s ips=%s", name, status, address, ips)
    hint = ""
    if status != "online":
        hint = " Use troubleshoot_device for a live connectivity probe."
    return f"{name}: {status} {detail}{hint}"


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
    deployed = runtime_field(device, "deployed_version")
    current = device.get("current_version", "")

    parts: list[str] = [f"{name}:"]
    if deployed:
        parts.append(f"  Deployed version: {deployed}")
    else:
        # The deployed version is monitor-observed, so "unknown" also covers a
        # device the dashboard has not seen on the network yet.
        parts.append("  Deployed version: unknown (not yet flashed, or not seen on the network)")
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


def _format_install_outcome(action: str, name: str, outcome: InstallOutcome) -> str:
    """Report a compile+flash chain honestly.

    A successful compile is not a successful flash: the OTA upload is a separate
    job, and an offline device gets a deferred install where nothing is flashed
    at all.
    """
    if outcome.deferred:
        logger.info("%s for %r: DEFERRED (device offline)", action, name)
        return (
            f"{action} result for {name}: COMPILED, FLASH DEFERRED\n\n"
            f"The device is offline, so the dashboard compiled the firmware and armed it "
            f"to flash automatically the next time the device checks in. Nothing has been "
            f"written to the device yet."
        )
    if outcome.exit_code == 0:
        logger.info("%s for %r: SUCCESS", action, name)
        return f"{action} result for {name}: SUCCESS (compiled and flashed)"

    stage = "compile" if outcome.stage == "compile" else "OTA upload"
    logger.info("%s for %r: FAILED at %s (exit_code=%d)", action, name, stage, outcome.exit_code)
    return f"{action} result for {name}: FAILED during {stage}\n\n{outcome.output}"


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
        "destructiveHint": False,
    }
)
async def migrate_device_configuration(device_name: str, apply: bool = False) -> str:
    """Bring a device's YAML up to date with the installed ESPHome's spellings.

    ESPHome renames configuration keys between releases (2026.8, for example,
    renamed `esp32_ble_id:` to `ble_hub_id:`, the sgp4x/sen5x/sen6x `voc`/`nox`
    keys to `voc_index`/`nox_index`, and consolidated the addressable-light
    `rgb_order`/`is_rgbw`/`is_wrgb` keys into `channel_colors`). This asks the
    dashboard to apply every migration it knows about, in one pass.

    Runs as a **dry run by default**: it reports what would change and saves
    nothing. Call again with `apply=True` to write the migrated YAML.

    Args:
        device_name: The name of the device whose configuration to migrate.
        apply: When True, save the migrated YAML. When False (the default),
            only report the proposed changes.
    """
    logger.info("Migrating configuration for device=%r (apply=%s)", device_name, apply)
    try:
        resolved = await _resolve_filename(device_name)
    except Exception as e:
        logger.error("Failed to resolve device %r: %s", device_name, e)
        return f"Error: {e}"

    if isinstance(resolved, str):
        return resolved

    device, filename = resolved
    name = device.get("friendly_name") or device.get("name", "unknown")

    client = get_client()
    try:
        content = await client.get_configuration(filename)
        result = await client.migrate_yaml(content)
    except Exception as e:
        logger.error("Failed to migrate %r: %s", device_name, e)
        return (
            _unsupported(e, "one-click config migration", "Device Builder 1.8.0 (ESPHome 2026.7)")
            or f"Error migrating configuration: {e}"
        )

    diff = result.get("yaml_diff")
    changes = result.get("changes") or []
    if not diff:
        logger.info("Migration for %r: nothing to do", name)
        return f"{name}: no migration needed — the configuration already uses current spellings."

    summary = "\n".join(f"- {_format_migration_change(c)}" for c in changes)
    replacement = diff.get("replacement", "")
    detail = (
        f"Lines {diff.get('fromLine')}-{diff.get('toLine')} would be replaced with:\n\n"
        f"{replacement}"
    )

    if not apply:
        logger.info("Migration for %r: %d change(s), dry run", name, len(changes))
        return (
            f"{name}: {len(changes)} migration(s) available (dry run — nothing saved).\n\n"
            f"{summary}\n\n{detail}\n\n"
            f"Re-run with apply=True to save, then validate and install."
        )

    try:
        migrated = _apply_yaml_diff(content, diff)
    except ValueError as e:
        logger.error("Unusable diff range for %r: %s", device_name, e)
        return f"Error: dashboard returned an unusable diff for {name}: {e}"

    try:
        await client.save_configuration(filename, migrated)
        output, exit_code = await client.validate_configuration(filename)
    except Exception as e:
        logger.error("Failed to save/validate migrated config for %r: %s", device_name, e)
        return f"Error saving migrated configuration: {e}"

    status = "VALID" if exit_code == 0 else "INVALID"
    logger.info("Migration for %r applied: %s", name, status)
    return (
        f"{name}: applied {len(changes)} migration(s).\n\n{summary}\n\n"
        f"Validation result: {status}\n\n{output}"
    )


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
    }
)
async def search_device_configurations(
    query: str, context_lines: int = 2, max_results: int = 50, case_sensitive: bool = False
) -> str:
    """Search every device's YAML configuration for a string.

    Answers "which devices use X?" in one call, instead of reading each device's
    configuration in turn. Useful for auditing the fleet before an ESPHome upgrade
    (e.g. searching for `rgb_order` or `esp32_ble_id` before moving to 2026.8).

    Args:
        query: Substring to search for. Not a regular expression.
        context_lines: Lines of surrounding context per match (0-10, default 2).
        max_results: Maximum matching lines to return overall (default 50).
        case_sensitive: Whether the search is case sensitive (default False).
    """
    logger.info("Searching device configurations for %r", query)
    if not query.strip():
        return "Provide a non-empty search query."

    try:
        results = await get_client().search_yaml(
            query,
            max_results=max_results,
            context_lines=max(0, min(10, context_lines)),
            case_sensitive=case_sensitive,
        )
    except Exception as e:
        logger.error("Failed to search configurations for %r: %s", query, e)
        return (
            _unsupported(e, "fleet-wide YAML search", "Device Builder 1.5.0 (ESPHome 2026.7)")
            or f"Error searching configurations: {e}"
        )

    if not results:
        logger.info("No matches for %r", query)
        return f"No device configuration contains {query!r}."

    blocks: list[str] = []
    for entry in results:
        name = entry.get("friendly_name") or entry.get("device_name") or entry.get("configuration")
        matches = entry.get("matches") or []
        total = entry.get("total_matches", len(matches))
        header = f"{name} ({entry.get('configuration')})"
        if total > len(matches):
            # The dashboard caps matches at 5 per file — say so rather than
            # letting the caller read a truncated list as complete.
            header += f" — showing {len(matches)} of {total} matches"
        lines = [header]
        for m in matches:
            for before in m.get("before") or []:
                lines.append(f"      {before}")
            lines.append(f"  {m.get('line_number')}: {m.get('line_text')}")
            for after in m.get("after") or []:
                lines.append(f"      {after}")
        blocks.append("\n".join(lines))

    logger.info("Found matches in %d device(s) for %r", len(results), query)
    return f"{len(results)} device(s) match {query!r}:\n\n" + "\n\n".join(blocks)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
    }
)
async def troubleshoot_device(device_name: str) -> str:
    """Run a live connectivity probe against a device.

    Unlike `get_device_status` (which reports the dashboard's cached view), this
    drops the cached DNS entry and performs a fresh DNS resolve, an mDNS re-query
    and an ICMP ping. Use it when a device shows as offline and you want to know
    which part of the path is failing.

    Args:
        device_name: The name of the device to probe.
    """
    logger.info("Troubleshooting device=%r", device_name)
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
        r = await get_client().troubleshoot(filename)
    except Exception as e:
        logger.error("Failed to troubleshoot %r: %s", device_name, e)
        return (
            _unsupported(e, "the connectivity probe", "Device Builder 1.9.0 (ESPHome 2026.7)")
            or f"Error running connectivity probe: {e}"
        )

    def verdict(ok: object, inconclusive: object, detail: str) -> str:
        if inconclusive:
            return (
                f"inconclusive (the probe itself failed) — {detail}" if detail else "inconclusive"
            )
        return f"yes — {detail}" if ok else "no"

    dns_addrs = ", ".join(r.get("dns_addresses") or [])
    mdns_addrs = ", ".join(r.get("mdns_addresses") or [])
    ping_target = r.get("ping_target") or "n/a"
    rtt = r.get("ping_rtt_ms")

    if not r.get("ping_attempted"):
        ping = "not attempted (no target to ping)"
    elif rtt is None:
        ping = f"no reply from {ping_target} (source: {r.get('ping_target_source') or 'unknown'})"
    else:
        ping = f"{rtt} ms from {ping_target} (source: {r.get('ping_target_source') or 'unknown'})"

    lines = [
        f"Connectivity probe for {name} (address: {r.get('address') or 'n/a'}):",
        f"  DNS resolved: {verdict(r.get('dns_resolved'), r.get('dns_inconclusive'), dns_addrs)}",
        f"  mDNS seen: {verdict(bool(mdns_addrs), r.get('mdns_inconclusive'), mdns_addrs)}",
        f"  Ping: {ping}",
    ]
    if r.get("icmp_available") is False:
        lines.append("  Note: this dashboard cannot send ICMP, so the ping leg proves nothing.")
    if not r.get("zeroconf_running"):
        lines.append("  Note: mDNS discovery is not running on the dashboard.")
    if r.get("dns_had_cached_failure"):
        lines.append(
            "  Note: DNS had a cached failure before this probe; the resolve above is live."
        )

    logger.info("Troubleshoot %r: ping=%s dns=%s", name, ping, r.get("dns_resolved"))
    return "\n".join(lines)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
    }
)
async def decode_device_backtrace(device_name: str, backtrace: str) -> str:
    """Decode a crash backtrace from a device's logs into source locations.

    Paste the crash region from `get_device_logs` (the lines carrying `0x...`
    addresses). Decoding runs against the build on the dashboard's disk, so the
    device must have been compiled there.

    Args:
        device_name: The device the crash came from.
        backtrace: The crash log excerpt containing the backtrace addresses.
    """
    logger.info("Decoding backtrace for device=%r", device_name)
    try:
        resolved = await _resolve_filename(device_name)
    except Exception as e:
        logger.error("Failed to resolve device %r: %s", device_name, e)
        return f"Error: {e}"

    if isinstance(resolved, str):
        return resolved

    device, filename = resolved
    name = device.get("friendly_name") or device.get("name", "unknown")

    lines = [line for line in backtrace.splitlines() if line.strip()]
    if not lines:
        return "Provide the crash log lines containing the backtrace addresses."

    try:
        result = await get_client().decode_backtrace(filename, lines)
    except Exception as e:
        logger.error("Failed to decode backtrace for %r: %s", device_name, e)
        return (
            _unsupported(e, "backtrace decoding", "Device Builder 1.5.0 (ESPHome 2026.7)")
            or f"Error decoding backtrace: {e}"
        )

    reason = result.get("unavailable_reason")
    if reason:
        explanation = {
            "no_backtrace": "no address-shaped tokens were found in the text you provided",
            "no_build": "this device has never been compiled on the dashboard, "
            "or its build directory was wiped",
            "elf_only": "the build's ELF is present but the decoder could not run against it",
        }.get(reason, reason)
        logger.info("Backtrace decode for %r unavailable: %s", name, reason)
        return f"{name}: could not decode — {explanation}."

    decoded = result.get("decoded") or []
    if not decoded:
        return f"{name}: the dashboard returned no decoded frames."

    body = "\n".join(f"  #{d.get('index')}: {d.get('text')}" for d in decoded)
    warning = ""
    if result.get("stale_build"):
        warning = (
            "\n\nWARNING: the device is running different firmware than the build on disk "
            "(config hashes differ), so these symbols are confident but probably wrong. "
            "Decode against the build the device is actually running."
        )
    logger.info("Decoded %d frame(s) for %r", len(decoded), name)
    return f"Decoded backtrace for {name}:\n{body}{warning}"


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
        outcome = await get_client().install_configuration(filename)
    except Exception as e:
        logger.error("Failed to install configuration for %r: %s", device_name, e)
        return f"Error installing configuration: {e}"

    return _format_install_outcome("Install", name, outcome)


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
        outcome = await get_client().install_configuration(filename)
    except Exception as e:
        logger.error("Failed to update device %r: %s", device_name, e)
        return f"Error updating device: {e}"

    return _format_install_outcome("Update", name, outcome)
