from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import sys
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class ESPHomeSettings(BaseSettings):
    """Configuration loaded from environment variables."""

    esphome_dashboard_url: str
    esphome_dashboard_username: str = ""
    esphome_dashboard_password: str = ""


class ESPHomeClient:
    """Async client for the ESPHome dashboard web API."""

    def __init__(self, settings: ESPHomeSettings) -> None:
        self._base_url = settings.esphome_dashboard_url.rstrip("/")
        self._auth: httpx.BasicAuth | None = None
        self._ws_auth_header: dict[str, str] = {}

        if settings.esphome_dashboard_username and settings.esphome_dashboard_password:
            self._auth = httpx.BasicAuth(
                settings.esphome_dashboard_username,
                settings.esphome_dashboard_password,
            )
            credentials = base64.b64encode(
                f"{settings.esphome_dashboard_username}:{settings.esphome_dashboard_password}".encode()
            ).decode()
            self._ws_auth_header = {"Authorization": f"Basic {credentials}"}
            logger.info("Configured Basic Auth for dashboard at %s", self._base_url)
        else:
            logger.info("Configured dashboard client at %s (no auth)", self._base_url)

    def _http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            auth=self._auth,
            timeout=30.0,
        )

    def _ws_url(self, path: str) -> str:
        parsed = urlparse(self._base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/{path.lstrip('/')}"

    async def get_devices(self) -> list[dict[str, Any]]:
        """Fetch all devices from the dashboard."""
        logger.debug("GET /devices (all)")
        async with self._http_client() as client:
            resp = await client.get("/devices")
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            configured: list[dict[str, Any]] = data.get("configured", [])
            importable: list[dict[str, Any]] = data.get("importable", [])
            logger.debug(
                "Got %d configured, %d importable devices", len(configured), len(importable)
            )
            return configured + importable

    async def get_configured_devices(self) -> list[dict[str, Any]]:
        """Fetch only configured devices from the dashboard."""
        logger.debug("GET /devices (configured only)")
        async with self._http_client() as client:
            resp = await client.get("/devices")
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            result: list[dict[str, Any]] = data.get("configured", [])
            logger.debug("Got %d configured devices", len(result))
            return result

    async def get_version(self) -> str:
        """Fetch the ESPHome version from the dashboard."""
        logger.debug("GET /version")
        async with self._http_client() as client:
            resp = await client.get("/version")
            resp.raise_for_status()
            version: str = resp.json().get("version", "unknown")
            logger.debug("ESPHome version: %s", version)
            return version

    async def ping(self) -> None:
        """Request a ping status update for all devices."""
        logger.debug("GET /ping")
        async with self._http_client() as client:
            resp = await client.get("/ping")
            resp.raise_for_status()

    async def get_configuration(self, filename: str) -> str:
        """Fetch the YAML configuration for a device."""
        if not filename.endswith((".yaml", ".yml")):
            raise ValueError(f"Invalid configuration filename: {filename}")
        logger.debug("GET /edit?configuration=%s", filename)
        async with self._http_client() as client:
            resp = await client.get("/edit", params={"configuration": filename})
            resp.raise_for_status()
            logger.debug("Got configuration for %s (%d bytes)", filename, len(resp.text))
            return resp.text

    async def _ws_spawn(
        self,
        path: str,
        message: dict[str, Any],
        timeout: float,
        done_pattern: str | None = None,
    ) -> tuple[str, int]:
        """Run a WebSocket command and collect output.

        Connects to the given WS path, sends the spawn message, and collects
        line events until an exit event, a done pattern match, or timeout.

        Args:
            path: WebSocket endpoint path (e.g. "/logs", "/validate").
            message: JSON message to send after connecting.
            timeout: Maximum seconds to wait for the command to complete.
            done_pattern: Optional substring to match in line data. When a line
                contains this string, collection stops and exit code 0 is returned.
                Useful for commands like ``esphome run`` that never exit on their
                own because they transition into log-tailing mode after OTA.

        Returns:
            Tuple of (collected output text, exit code). Exit code is -1 on timeout.
        """
        ws_url = self._ws_url(path)
        lines: list[str] = []
        exit_code = -1

        logger.debug("WS %s: connecting (timeout=%.1fs)", path, timeout)
        try:
            async with websockets.connect(
                ws_url,
                additional_headers=self._ws_auth_header,
            ) as ws:
                await ws.send(json.dumps(message))
                logger.debug("WS %s: sent %r", path, message)

                try:
                    async with asyncio.timeout(timeout):
                        async for raw_msg in ws:
                            msg = json.loads(raw_msg)
                            if msg.get("event") == "line":
                                data = msg.get("data", "")
                                lines.append(data)
                                if done_pattern and done_pattern in data:
                                    exit_code = 0
                                    logger.debug(
                                        "WS %s: done pattern matched: %r",
                                        path,
                                        done_pattern,
                                    )
                                    break
                            elif msg.get("event") == "exit":
                                exit_code = msg.get("code", -1)
                                logger.debug("WS %s: exited with code %s", path, exit_code)
                                break
                except TimeoutError:
                    logger.debug(
                        "WS %s: timed out after %.1fs (%d lines)", path, timeout, len(lines)
                    )
        except (websockets.exceptions.WebSocketException, OSError) as e:
            if lines:
                logger.warning("WS %s: closed with %d lines collected: %s", path, len(lines), e)
                lines.append(f"\n[Connection closed: {e}]")
            else:
                logger.error("WS %s: connection failed: %s", path, e)
                raise

        logger.debug("WS %s: collected %d lines", path, len(lines))
        return "\n".join(lines), exit_code

    async def get_logs(self, filename: str, duration: float = 10.0) -> str:
        """Connect to the logs WebSocket and collect output for a duration.

        Args:
            filename: The device configuration filename.
            duration: Seconds to collect logs (default 10).

        Returns:
            Collected log lines joined by newlines.
        """
        logger.debug("Fetching logs for %s (duration=%.1fs)", filename, duration)
        output, _exit_code = await self._ws_spawn(
            "/logs",
            {"type": "spawn", "configuration": filename, "port": "OTA"},
            timeout=duration,
        )
        return output

    async def save_configuration(self, filename: str, yaml_content: str) -> None:
        """Save a YAML configuration file to the dashboard.

        Args:
            filename: The configuration filename (e.g. "bike-outlet.yaml").
            yaml_content: The complete YAML content to save.
        """
        if not filename.endswith((".yaml", ".yml")):
            raise ValueError(f"Invalid configuration filename: {filename}")
        logger.debug("POST /edit?configuration=%s (%d bytes)", filename, len(yaml_content))
        async with self._http_client() as client:
            resp = await client.post(
                "/edit",
                params={"configuration": filename},
                content=yaml_content.encode("utf-8"),
            )
            resp.raise_for_status()
        logger.info("Saved configuration %s", filename)

    async def validate_configuration(
        self, filename: str, timeout: float = 120.0
    ) -> tuple[str, int]:
        """Validate a device configuration via the dashboard.

        Args:
            filename: The configuration filename.
            timeout: Maximum seconds to wait (default 120).

        Returns:
            Tuple of (validation output, exit code). Exit code 0 means valid.
        """
        logger.info("Validating configuration %s", filename)
        return await self._ws_spawn(
            "/validate",
            {"type": "spawn", "configuration": filename},
            timeout=timeout,
        )

    async def run_configuration(self, filename: str, timeout: float = 600.0) -> tuple[str, int]:
        """Compile and upload a configuration to a device via OTA.

        Uses the /run endpoint which compiles the firmware first, then uploads it.

        Args:
            filename: The configuration filename.
            timeout: Maximum seconds to wait (default 600).

        Returns:
            Tuple of (compile/upload output, exit code). Exit code 0 means success.
        """
        logger.info("Running configuration %s (compile + upload)", filename)
        return await self._ws_spawn(
            "/run",
            {"type": "spawn", "configuration": filename, "port": "OTA"},
            timeout=timeout,
            done_pattern="OTA successful",
        )


async def validate_local_configuration(path: str, timeout: float = 120.0) -> tuple[str, int]:
    """Validate a local ESPHome YAML file with the ESPHome validator.

    Runs ``esphome config <path>`` as a subprocess using the current Python
    interpreter, so ``esphome`` must be installed in the same environment. The
    ``!secret`` references are resolved relative to the file's directory, as with
    the ``esphome`` CLI.

    Args:
        path: Path to a local ESPHome YAML configuration file.
        timeout: Maximum seconds to wait for validation (default 120).

    Returns:
        Tuple of (validation output, exit code). Exit code 0 means valid.

    Raises:
        FileNotFoundError: If the path does not point to an existing file.
    """
    config_path = Path(path)
    if not config_path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    cmd = [sys.executable, "-m", "esphome", "config", str(config_path)]
    logger.info("Validating local configuration %s", config_path)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("Local validation of %s timed out after %.0fs", config_path, timeout)
        return f"Validation timed out after {timeout:.0f} seconds", -1

    output = stdout.decode(errors="replace")
    exit_code = proc.returncode if proc.returncode is not None else -1
    logger.info("Local validation of %s exited with code %s", config_path, exit_code)
    return output, exit_code


SCHEMA_URL_TEMPLATE = (
    "https://github.com/esphome/esphome-schema/releases/download/{version}/schema.zip"
)

# Cache: version -> {component_name: json_string}
_schema_cache: dict[str, dict[str, str]] = {}


async def fetch_schema(version: str, component: str | None = None) -> dict[str, str] | str:
    """Fetch and cache the ESPHome schema for a given version.

    Args:
        version: ESPHome version (e.g. "2026.3.0").
        component: Optional component name to return a single schema.

    Returns:
        If component is specified, returns the JSON string for that component.
        Otherwise returns a dict mapping component names to JSON strings.
    """
    if version not in _schema_cache:
        url = SCHEMA_URL_TEMPLATE.format(version=version)
        logger.info("Downloading ESPHome schema for version %s", version)
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()

        schemas: dict[str, str] = {}
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            for name in zf.namelist():
                if name.endswith(".json"):
                    # Strip "schema/" prefix and ".json" suffix
                    component_name = name.rsplit("/", 1)[-1].removesuffix(".json")
                    schemas[component_name] = zf.read(name).decode()

        logger.info("Cached schema for version %s (%d components)", version, len(schemas))
        _schema_cache[version] = schemas

    cached = _schema_cache[version]
    if component is not None:
        if component not in cached:
            available = sorted(cached.keys())
            raise KeyError(
                f"Component '{component}' not found in schema {version}. "
                f"Available components ({len(available)}): {', '.join(available)}"
            )
        return cached[component]
    return cached


_client: ESPHomeClient | None = None
_settings_override: ESPHomeSettings | None = None


def configure(settings: ESPHomeSettings) -> None:
    """Set a custom settings override (e.g. for tests). Resets any existing client."""
    global _settings_override, _client
    _settings_override = settings
    _client = None
    logger.info("Client configured with override URL=%s", settings.esphome_dashboard_url)


def get_client() -> ESPHomeClient:
    """Return the shared client, creating it on first access."""
    global _client
    if _client is None:
        settings = _settings_override or ESPHomeSettings()  # type: ignore[call-arg]
        _client = ESPHomeClient(settings)
    return _client


def reset() -> None:
    """Clear the shared client (for test teardown)."""
    global _client, _settings_override
    _client = None
    _settings_override = None
    logger.info("Client reset")
