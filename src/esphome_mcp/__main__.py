import asyncio
import logging
import os
import threading
import time

from esphome_mcp import addon
from esphome_mcp.client import get_client
from esphome_mcp.server import mcp

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

_MAX_RETRIES = 5
_RETRY_DELAY = 5  # seconds between retries

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Idempotent: safe to call again after LOG_LEVEL changes (e.g. once options
    are merged from a Home Assistant add-on's /data/options.json), unlike a bare
    ``logging.basicConfig()`` call, which is a no-op once handlers exist."""
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(format=LOG_FORMAT, level=level)
    logging.root.setLevel(level)


def _check_connectivity(fatal: bool = True) -> None:
    """Verify connectivity to the ESPHome dashboard, retrying on failure.

    Uses the cheap REST ``/version`` endpoint so the pre-flight check does not open
    (and leave dangling) a WebSocket on a throwaway event loop — the persistent WS
    connection is established lazily within the server's own loop on first tool call.

    With ``fatal=True`` (stdio, via ``main()``), exhausting retries raises
    ``SystemExit(1)`` -- there's no other way for that transport to signal trouble.
    With ``fatal=False`` (HTTP, via ``main_web()``), it logs and returns instead:
    the HTTP server needs to come up regardless, so the add-on's ingress status
    page (and the Docker ``HEALTHCHECK``) can report the dashboard as unreachable
    rather than nothing being reachable at all.
    """
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            version = asyncio.run(get_client().get_version())
            logger.info("Connected to ESPHome dashboard (ESPHome %s)", version)
            return
        except Exception as e:
            logger.error(
                "Failed to connect to ESPHome dashboard (attempt %d/%d): %s",
                attempt,
                _MAX_RETRIES,
                e,
            )
            if attempt < _MAX_RETRIES:
                logger.info("Retrying in %d seconds...", _RETRY_DELAY)
                time.sleep(_RETRY_DELAY)
    if fatal:
        raise SystemExit(1)
    logger.error(
        "Starting anyway -- check the status page or dashboard_url and restart "
        "once the dashboard is reachable."
    )


def _check_connectivity_background() -> None:
    """Run the (non-fatal) connectivity pre-flight check in a background thread.

    ``mcp.run()`` blocks forever once called, so running the check inline before
    it -- even non-fatally -- would still delay the HTTP server (and the add-on's
    ingress status page) coming up for the whole retry window. A background
    thread lets ``mcp.run()`` start immediately while the check keeps logging its
    own progress; it uses the cheap REST endpoint (see ``_check_connectivity``),
    not the WebSocket the server's own event loop manages, so the two don't race.
    """
    threading.Thread(target=_check_connectivity, kwargs={"fatal": False}, daemon=True).start()


def main() -> None:
    _configure_logging()
    _check_connectivity()
    mcp.run()


def main_web() -> None:
    # Logging must be configured *before* resolve_mcp_path(), or anything it logs
    # (options merge, secret-path resolution) is silently dropped -- Python's
    # no-handler-yet fallback only surfaces WARNING+, unformatted. Reconfigured
    # again afterward in case options set LOG_LEVEL.
    _configure_logging()
    mcp_path = addon.resolve_mcp_path()
    _configure_logging()
    _check_connectivity_background()
    mcp.run(transport="http", host="0.0.0.0", port=8080, path=mcp_path)


if __name__ == "__main__":
    main()
