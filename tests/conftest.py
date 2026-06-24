from __future__ import annotations

import os

import pytest


@pytest.fixture
def live_dashboard_url() -> str:
    """URL of a real ESPHome 2026.6+ dashboard, or skip the test.

    Set ``ESPHOME_DASHBOARD_URL`` to run the live smoke tests, e.g.::

        ESPHOME_DASHBOARD_URL=https://esphome.example.com pytest -m live
    """
    url = os.environ.get("ESPHOME_DASHBOARD_URL")
    if not url:
        pytest.skip("ESPHOME_DASHBOARD_URL not set; skipping live dashboard test")
    return url
