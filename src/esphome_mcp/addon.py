"""Home Assistant Supervisor add-on integration.

Translates Supervisor's ``/data/options.json`` into the environment variables the
rest of the app already reads, and manages the persisted, high-entropy path used to
mount the MCP endpoint (instead of a fixed, guessable ``/mcp``) when running as the
add-on. Every entry point here is a no-op when ``/data/options.json`` is absent, so
plain Docker/``docker compose`` usage is unaffected.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

DATA_DIR = Path("/data")
OPTIONS_PATH = DATA_DIR / "options.json"
SECRET_PATH_FILE = DATA_DIR / "secret_path.txt"

_SUPERVISOR_SELF_OPTIONS_URL = "http://supervisor/addons/self/options"

_OPTION_ENV_MAP = {
    "dashboard_url": "ESPHOME_DASHBOARD_URL",
    "dashboard_username": "ESPHOME_DASHBOARD_USERNAME",
    "dashboard_password": "ESPHOME_DASHBOARD_PASSWORD",
    "log_level": "LOG_LEVEL",
}

# Same shape as the add-on secret path convention this follows: leading slash, no
# embedded scheme, at least 8 characters so a stray "/x" typo can't produce a
# trivially guessable mount point.
_SECRET_PATH_RE = re.compile(r"^/(?!.*://)\S{7,}$")


def load_addon_options(options_path: Path = OPTIONS_PATH) -> dict[str, object]:
    """Load Supervisor's options file, or ``{}`` if absent or unparseable."""
    if not options_path.exists():
        return {}
    try:
        return json.loads(options_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read add-on options (%s): %s", options_path, e)
        return {}


def apply_addon_options_to_env(options: dict[str, object]) -> None:
    """Set env vars from add-on options, without overriding ones already set."""
    for key, env_var in _OPTION_ENV_MAP.items():
        value = options.get(key)
        if value and env_var not in os.environ:
            os.environ[env_var] = str(value)


def _generate_secret_path() -> str:
    return "/private_" + secrets.token_urlsafe(16)


def _is_valid_secret_path(path: str) -> bool:
    return bool(_SECRET_PATH_RE.match(path))


def _persist_secret_path(secret_file: Path, path: str) -> None:
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    secret_file.write_text(path)


def _reset_regenerate_flag(options: dict[str, object], client: httpx.Client | None = None) -> None:
    """Best-effort: flip ``regenerate_secret_path`` back to False in Supervisor's
    stored options after honoring it, so it doesn't regenerate again on every
    subsequent restart. Any failure is logged and swallowed — the add-on keeps
    running with the new secret path either way; the user just has to toggle the
    option back off manually in the Configuration tab if this write fails.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=5)
    try:
        resp = client.post(
            _SUPERVISOR_SELF_OPTIONS_URL,
            json={"options": {**options, "regenerate_secret_path": False}},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
    except Exception as e:
        logger.warning(
            "Could not reset regenerate_secret_path after regenerating (%s). "
            "Toggle it off manually in the Configuration tab, or it will "
            "regenerate again on the next restart.",
            e,
        )
    finally:
        if owns_client:
            client.close()


def get_or_create_secret_path(
    options: dict[str, object], secret_file: Path = SECRET_PATH_FILE
) -> str:
    """Resolve the MCP mount path: a valid option override, else (unless
    ``regenerate_secret_path`` is set) the persisted path, else a freshly
    generated one (persisted for reuse on restart)."""
    override = options.get("secret_path")
    if isinstance(override, str) and override.strip():
        path = override.strip()
        if not path.startswith("/"):
            path = "/" + path
        if _is_valid_secret_path(path):
            _persist_secret_path(secret_file, path)
            return path
        logger.warning("Configured secret_path %r is invalid; ignoring.", path)

    force_regenerate = bool(options.get("regenerate_secret_path"))
    if not force_regenerate and secret_file.exists():
        stored = secret_file.read_text().strip()
        if _is_valid_secret_path(stored):
            return stored
        logger.warning("Stored secret path %r is invalid; regenerating.", stored)

    new_path = _generate_secret_path()
    _persist_secret_path(secret_file, new_path)
    if force_regenerate:
        logger.info("MCP secret path regenerated (regenerate_secret_path was set).")
        _reset_regenerate_flag(options)
    return new_path


def resolve_mcp_path(
    options_path: Path = OPTIONS_PATH, secret_file: Path = SECRET_PATH_FILE
) -> str:
    """Merge add-on options into the environment and return the MCP mount path.

    Outside the add-on (no options file), this just returns ``MCP_PATH`` from the
    environment, defaulting to ``/mcp`` — the existing behavior. An explicit
    ``MCP_PATH`` always wins over a generated secret path.
    """
    options = load_addon_options(options_path)
    if options:
        apply_addon_options_to_env(options)
        if "MCP_PATH" not in os.environ:
            secret_path = get_or_create_secret_path(options, secret_file)
            os.environ["MCP_PATH"] = secret_path
            logger.info(
                "MCP endpoint mounted at %s (the full connect URL, including port, "
                "is shown on this add-on's ingress status page)",
                secret_path,
            )
    return os.environ.get("MCP_PATH", "/mcp")
