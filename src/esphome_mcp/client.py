"""Async client for the ESPHome 2026.6+ "Device Builder" dashboard.

ESPHome 2026.6 replaced the dashboard's legacy HTTP/per-action-WebSocket API with a
single persistent WebSocket that carries namespaced JSON commands. This client speaks
that protocol. See ``DECISIONS.md`` for the full reverse-engineered protocol notes.

Protocol summary
----------------
* Connect to ``ws(s)://<host>/ws``. On connect the server pushes one **server_info**
  frame, e.g. ``{"server_version": "...", "esphome_version": "...", "requires_auth": false}``.
  If ``requires_auth`` is true, send ``auth/login`` and include the returned token.
* Request envelope: ``{"command": "<ns/action>", "message_id": <int>, "args": {...}}``.
* Responses correlate by ``message_id`` (returned as a **string**):
    - success:      ``{"message_id": "1", "result": <payload>}``
    - failure:      ``{"message_id": "1", "error_code": "...", "details": "..."}``
    - stream line:  ``{"message_id": "1", "event": "output", "data": "<line>"}``
    - stream end:   ``{"message_id": "1", "event": "result", "data": <final payload>}``
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
import sys
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
import websockets
from pydantic_settings import BaseSettings

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class ESPHomeSettings(BaseSettings):
    """Configuration loaded from environment variables."""

    esphome_dashboard_url: str
    esphome_dashboard_username: str = ""
    esphome_dashboard_password: str = ""


class DashboardError(Exception):
    """An error returned by the dashboard, or a transport failure."""

    def __init__(self, message: str, error_code: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class ESPHomeClient:
    """Persistent-WebSocket client for the ESPHome Device Builder dashboard.

    A single connection is shared across all tool calls. A background reader task
    dispatches incoming frames to per-``message_id`` futures (request/response) and
    stream handlers (``firmware/follow_job``, ``devices/logs``). The connection is
    established lazily and re-established automatically if it drops.
    """

    def __init__(self, settings: ESPHomeSettings) -> None:
        self._http_base = settings.esphome_dashboard_url.rstrip("/")
        self._username = settings.esphome_dashboard_username
        self._password = settings.esphome_dashboard_password
        self._ws_url = self._derive_ws_url(self._http_base)

        self._ws: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._message_id = 0
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._streams: dict[str, dict[str, Any]] = {}
        self._server_info: dict[str, Any] = {}
        self._auth_token: str | None = None
        self._connect_lock = asyncio.Lock()

        logger.info("ESPHome dashboard client: http=%s ws=%s", self._http_base, self._ws_url)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _derive_ws_url(http_base: str) -> str:
        parsed = urlparse(http_base)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        path = parsed.path.rstrip("/")
        return f"{scheme}://{parsed.netloc}{path}/ws"

    def _http_client(self) -> httpx.AsyncClient:
        auth = None
        if self._username and self._password:
            auth = httpx.BasicAuth(self._username, self._password)
        return httpx.AsyncClient(base_url=self._http_base, auth=auth, timeout=30.0)

    def _next_id(self) -> int:
        self._message_id += 1
        return self._message_id

    def _is_open(self) -> bool:
        ws = self._ws
        if ws is None:
            return False
        state = getattr(ws, "state", None)
        return getattr(state, "name", "") == "OPEN"

    # -------------------------------------------------------------- connection

    async def _connect(self) -> None:
        if self._is_open():
            return
        async with self._connect_lock:
            if self._is_open():
                return
            logger.debug("Connecting WebSocket %s", self._ws_url)
            ws = await websockets.connect(
                self._ws_url,
                max_size=None,
                open_timeout=15,
                ping_interval=20,
                ping_timeout=20,
            )
            # First frame is the unsolicited server_info.
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            self._server_info = json.loads(raw)
            logger.info(
                "Connected to ESPHome Device Builder %s (server %s, requires_auth=%s)",
                self._server_info.get("esphome_version"),
                self._server_info.get("server_version"),
                self._server_info.get("requires_auth"),
            )
            self._ws = ws
            self._pending.clear()
            self._streams.clear()
            self._reader_task = asyncio.create_task(self._reader())

            if self._server_info.get("requires_auth"):
                await self._login()

    async def _login(self) -> None:
        args: dict[str, Any] = {}
        if self._username:
            args["username"] = self._username
        if self._password:
            args["password"] = self._password
        if not args:
            raise DashboardError(
                "Dashboard requires authentication but no "
                "ESPHOME_DASHBOARD_USERNAME/PASSWORD were provided."
            )
        result = await self._send("auth/login", args)
        self._auth_token = result.get("token") if isinstance(result, dict) else None
        logger.info("Authenticated to dashboard")

    async def _reader(self) -> None:
        ws = self._ws
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                self._dispatch(msg)
        except (websockets.exceptions.WebSocketException, OSError) as e:
            logger.warning("WebSocket reader stopped: %s", e)
        finally:
            self._fail_all(ConnectionError("WebSocket connection closed"))
            if self._ws is ws:
                self._ws = None

    def _fail_all(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
        for stream in self._streams.values():
            done = stream["done"]
            if not done.done():
                done.set_exception(exc)
        self._streams.clear()

    def _dispatch(self, msg: dict[str, Any]) -> None:
        mid = msg.get("message_id")
        if mid is None:
            logger.debug("Unsolicited frame: %s", str(msg)[:200])
            return
        mid = str(mid)

        if "error_code" in msg:
            err = DashboardError(
                msg.get("details") or msg.get("error_code", "unknown error"),
                msg.get("error_code"),
            )
            fut = self._pending.pop(mid, None)
            if fut is not None and not fut.done():
                fut.set_exception(err)
                return
            stream = self._streams.pop(mid, None)
            if stream is not None and not stream["done"].done():
                stream["done"].set_exception(err)
            return

        if "event" in msg:
            stream = self._streams.get(mid)
            if stream is None:
                return
            event = msg.get("event")
            if event == "output":
                stream["on_line"](msg.get("data", ""))
            elif event == "result":
                self._streams.pop(mid, None)
                if not stream["done"].done():
                    stream["done"].set_result(msg.get("data"))
            return

        if "result" in msg:
            fut = self._pending.pop(mid, None)
            if fut is not None and not fut.done():
                fut.set_result(msg.get("result"))

    # --------------------------------------------------------- command / stream

    async def _send(
        self, command: str, args: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> Any:
        """Send a command and await its correlated result, raising on error."""
        await self._connect()
        mid = self._next_id()
        fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[str(mid)] = fut
        payload: dict[str, Any] = {"command": command, "message_id": mid}
        if args:
            payload["args"] = args
        logger.debug("WS -> %s (id=%d) args=%s", command, mid, list((args or {}).keys()))
        await self._ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            self._pending.pop(str(mid), None)
            raise DashboardError(f"Command {command!r} timed out after {timeout:.0f}s") from None

    async def _follow_job(
        self,
        command: str,
        args: dict[str, Any],
        on_line: Callable[[str], None] | None = None,
        timeout: float = 1200.0,
    ) -> tuple[str, int]:
        """Start a firmware job, follow its output stream, return (output, exit_code)."""
        job = await self._send(command, args, timeout=60)
        job_id = job.get("job_id") if isinstance(job, dict) else None
        if not job_id:
            raise DashboardError(f"{command}: no job_id in response: {str(job)[:200]}")

        lines: list[str] = []

        def collect(data: str) -> None:
            lines.append(data)
            if on_line is not None:
                on_line(data)

        await self._connect()
        mid = self._next_id()
        done: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._streams[str(mid)] = {"on_line": collect, "done": done}
        await self._ws.send(
            json.dumps(
                {"command": "firmware/follow_job", "message_id": mid, "args": {"job_id": job_id}}
            )
        )
        try:
            final = await asyncio.wait_for(done, timeout=timeout)
        except TimeoutError:
            self._streams.pop(str(mid), None)
            raise DashboardError(f"Firmware job {job_id} timed out after {timeout:.0f}s") from None

        exit_code: int | None = None
        if isinstance(final, dict):
            exit_code = final.get("exit_code")
            # If the job finished before we attached, its buffered output is in `final`.
            if not lines and isinstance(final.get("output"), list):
                lines.extend(str(x) for x in final["output"])
        if exit_code is None:
            with contextlib.suppress(Exception):
                polled = await self._send("firmware/get_job", {"job_id": job_id})
                if isinstance(polled, dict):
                    exit_code = polled.get("exit_code")

        output = _strip_ansi("".join(lines))
        return output, (exit_code if exit_code is not None else -1)

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._ws = None

    # ----------------------------------------------------------- high-level API

    async def get_version(self) -> str:
        """Fetch the ESPHome version (cheap REST call, also used for liveness)."""
        async with self._http_client() as client:
            resp = await client.get("/version")
            resp.raise_for_status()
            return resp.json().get("version", "unknown")

    async def get_configured_devices(self) -> list[dict[str, Any]]:
        """Fetch only configured devices via ``devices/list``."""
        result = await self._send("devices/list")
        configured: list[dict[str, Any]] = (result or {}).get("configured", [])
        logger.debug("devices/list -> %d configured", len(configured))
        return configured

    async def get_devices(self) -> list[dict[str, Any]]:
        """Fetch configured + importable devices."""
        result = await self._send("devices/list")
        result = result or {}
        return list(result.get("configured", [])) + list(result.get("importable", []))

    async def ping(self) -> None:
        """Lightweight keepalive / liveness command."""
        with contextlib.suppress(Exception):
            await self._send("ping", timeout=10)

    async def get_configuration(self, filename: str) -> str:
        """Read a device's YAML configuration via ``devices/get_config``."""
        if not filename.endswith((".yaml", ".yml")):
            raise ValueError(f"Invalid configuration filename: {filename}")
        result = await self._send("devices/get_config", {"configuration": filename})
        if not isinstance(result, str):
            raise DashboardError(f"Unexpected get_config result type: {type(result).__name__}")
        logger.debug("get_config %s -> %d bytes", filename, len(result))
        return result

    async def save_configuration(self, filename: str, yaml_content: str) -> None:
        """Save a device's YAML configuration via ``devices/update_config``."""
        if not filename.endswith((".yaml", ".yml")):
            raise ValueError(f"Invalid configuration filename: {filename}")
        await self._send(
            "devices/update_config",
            {"configuration": filename, "content": yaml_content},
        )
        logger.info("Saved configuration %s (%d bytes)", filename, len(yaml_content))

    @staticmethod
    def _format_validation(result: dict[str, Any]) -> tuple[str, int]:
        yaml_errors = result.get("yaml_errors") or []
        validation_errors = result.get("validation_errors") or []
        if not yaml_errors and not validation_errors:
            return "Configuration is valid.", 0

        parts: list[str] = []
        for e in yaml_errors:
            msg = e.get("message", e) if isinstance(e, dict) else e
            parts.append(f"YAML error: {msg}")
        for e in validation_errors:
            if isinstance(e, dict):
                rng = e.get("range") or {}
                loc = ""
                if "start_line" in rng:
                    loc = f" (line {rng['start_line']}, col {rng.get('start_col', '?')})"
                parts.append(f"Validation error{loc}: {e.get('message', e)}")
            else:
                parts.append(f"Validation error: {e}")
        return "\n".join(parts), 1

    async def validate_yaml(
        self, filename: str, yaml_content: str, timeout: float = 60.0
    ) -> tuple[str, int]:
        """Validate YAML content via ``editor/validate_yaml`` (full ESPHome validation).

        Returns (human-readable output, exit_code) where exit_code 0 means valid.
        """
        result = await self._send(
            "editor/validate_yaml",
            {"configuration": filename, "content": yaml_content},
            timeout=timeout,
        )
        if not isinstance(result, dict):
            raise DashboardError(f"Unexpected validate result: {str(result)[:200]}")
        return self._format_validation(result)

    async def validate_configuration(self, filename: str, timeout: float = 60.0) -> tuple[str, int]:
        """Validate a device's currently-saved configuration."""
        content = await self.get_configuration(filename)
        return await self.validate_yaml(filename, content, timeout=timeout)

    async def get_logs(self, filename: str, duration: float = 10.0) -> str:
        """Stream device logs for ``duration`` seconds via ``devices/logs``."""
        await self._connect()
        mid = self._next_id()
        lines: list[str] = []
        done: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._streams[str(mid)] = {"on_line": lines.append, "done": done}
        await self._ws.send(
            json.dumps(
                {
                    "command": "devices/logs",
                    "message_id": mid,
                    "args": {"configuration": filename, "port": "OTA"},
                }
            )
        )
        try:
            # Logs stream until stopped; bound it by duration. An early result/error
            # (e.g. device offline) resolves/raises sooner — we still return what we have.
            await asyncio.wait_for(done, timeout=duration)
        except (TimeoutError, DashboardError, ConnectionError):
            pass
        finally:
            self._streams.pop(str(mid), None)
            with contextlib.suppress(Exception):
                await self._ws.send(
                    json.dumps(
                        {
                            "command": "devices/stop_stream",
                            "message_id": self._next_id(),
                            "args": {"stream_id": mid},
                        }
                    )
                )
        return _strip_ansi("".join(lines))

    async def compile_configuration(
        self,
        filename: str,
        on_line: Callable[[str], None] | None = None,
        timeout: float = 1200.0,
    ) -> tuple[str, int]:
        """Compile a device's firmware via ``firmware/compile`` (no flash)."""
        logger.info("Compiling %s", filename)
        return await self._follow_job(
            "firmware/compile", {"configuration": filename}, on_line=on_line, timeout=timeout
        )

    async def install_configuration(
        self,
        filename: str,
        port: str = "OTA",
        on_line: Callable[[str], None] | None = None,
        timeout: float = 1800.0,
    ) -> tuple[str, int]:
        """Compile and flash a device via ``firmware/install`` (OTA by default)."""
        logger.info("Installing %s (port=%s)", filename, port)
        return await self._follow_job(
            "firmware/install",
            {"configuration": filename, "port": port, "force_local": False},
            on_line=on_line,
            timeout=timeout,
        )


async def validate_local_configuration(path: str, timeout: float = 120.0) -> tuple[str, int]:
    """Validate a local ESPHome YAML file with the ESPHome validator.

    Runs ``esphome config <path>`` as a subprocess using the current Python
    interpreter, so ``esphome`` must be installed in the same environment. The
    ``!secret`` references are resolved relative to the file's directory, as with
    the ``esphome`` CLI.

    Returns (validation output, exit code). Exit code 0 means valid.
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
    """Fetch and cache the ESPHome schema for a given version."""
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
        # Fields are populated from the environment by pydantic-settings.
        settings = _settings_override or ESPHomeSettings()  # ty: ignore[missing-argument]
        _client = ESPHomeClient(settings)
    return _client


def reset() -> None:
    """Clear the shared client (for test teardown)."""
    global _client, _settings_override
    _client = None
    _settings_override = None
    logger.info("Client reset")
