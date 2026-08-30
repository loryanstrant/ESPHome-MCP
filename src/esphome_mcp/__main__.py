import asyncio
import logging
import os
import time

from esphome_mcp.client import get_client
from esphome_mcp.server import mcp

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

_MAX_RETRIES = 5
_RETRY_DELAY = 5  # seconds between retries

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(format=LOG_FORMAT, level=getattr(logging, level, logging.INFO))


def _check_connectivity() -> None:
    """Verify connectivity to the ESPHome dashboard, retrying on failure.

    Uses the cheap REST ``/version`` endpoint so the pre-flight check does not open
    (and leave dangling) a WebSocket on a throwaway event loop — the persistent WS
    connection is established lazily within the server's own loop on first tool call.
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
    raise SystemExit(1)


def main() -> None:
    _configure_logging()
    _check_connectivity()
    mcp.run()


def main_web() -> None:
    _configure_logging()
    _check_connectivity()
    # stateless_http: a gateway like MetaMCP opens a NEW MCP session per burst of calls
    # and never sends DELETE /mcp, and fastmcp 3.4.x only evicts a session once its
    # server task ends — so in stateful mode every session ever served stays resident
    # (measured at ~57 KB each on a sibling wrapper, which reached 1 GB in two weeks).
    # None of the tools here use Context, progress or server-initiated notifications, so
    # sessions buy nothing. Stateless drops GET /mcp (405), which MCP clients handle per
    # spec as "this server offers no SSE stream".
    mcp.run(transport="http", host="0.0.0.0", port=8080, stateless_http=True)


if __name__ == "__main__":
    main()
