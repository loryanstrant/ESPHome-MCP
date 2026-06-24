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
    """Verify connectivity to the ESPHome dashboard, retrying on failure."""
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            devices = asyncio.run(get_client().get_configured_devices())
            names = [d.get("name", "unknown") for d in devices]
            logger.info("Connected to ESPHome dashboard. Found %d device(s): %s", len(names), names)
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
    mcp.run(transport="http", host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
