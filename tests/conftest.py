from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

import httpx
import pytest
from fastmcp.client import Client

from esphome_mcp import client as client_module
from esphome_mcp.client import ESPHomeSettings
from esphome_mcp.server import mcp

BIKE_OUTLET_YAML = """\
esphome:
  name: bike-outlet
  friendly_name: Bike Outlet
  compile_process_limit: 3

esp8266:
  board: esp01_1m

logger:
  baud_rate: 0

api:

ota:
  platform: esphome
  password: "89ea2f8d94468d86a3afb754057074c6"

wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password

web_server:
  port: 80

uart:
  rx_pin: RX
  baud_rate: 4800
  parity: EVEN

binary_sensor:
  - platform: gpio
    pin:
      number: GPIO0
      mode: INPUT_PULLUP
      inverted: True
    name: "button"
    on_press:
      - switch.toggle: relay
  - platform: status
    name: "status"

sensor:
  - platform: wifi_signal
    name: "wifi_signal"
    update_interval: 60s
  - platform: cse7766
    current:
      name: "current"
      accuracy_decimals: 1
      filters:
        - or:
          - throttle: 60min
          - delta: 0.5
    voltage:
      name: "voltage"
      accuracy_decimals: 1
      filters:
        - or:
          - throttle: 60min
          - delta: 5
    power:
      name: "power"
      accuracy_decimals: 1
      id: power
      filters:
        - or:
          - throttle: 60min
          - delta: 5
  - platform: integration
    name: "energy"
    sensor: power
    time_unit: h
    unit_of_measurement: kWh
    filters:
      - multiply: 0.001

time:
  - platform: sntp
    id: the_time

switch:
  - platform: gpio
    name: "relay"
    pin: GPIO12
    id: relay
    restore_mode: ALWAYS_ON

status_led:
  pin: GPIO13
"""

GARAGE_SENSOR_YAML = """\
esphome:
  name: garage-sensor
  friendly_name: Garage Sensor

esp32:
  board: esp32dev

logger:

api:

ota:
  platform: esphome

wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password

sensor:
  - platform: dht
    pin: GPIO4
    temperature:
      name: "temperature"
    humidity:
      name: "humidity"
    update_interval: 60s
  - platform: wifi_signal
    name: "wifi_signal"
    update_interval: 60s

binary_sensor:
  - platform: gpio
    pin:
      number: GPIO5
      mode: INPUT_PULLUP
    name: "garage_door"
    device_class: garage_door
  - platform: status
    name: "status"

status_led:
  pin: GPIO2
"""

SECRETS_YAML = """\
wifi_ssid: "TestNetwork"
wifi_password: "testpassword123"
"""


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def esphome_config_dir() -> Generator[Path]:
    """Create a temporary directory with realistic ESPHome device configs."""
    with tempfile.TemporaryDirectory(prefix="esphome_test_") as tmpdir:
        config_dir = Path(tmpdir)
        (config_dir / "bike-outlet.yaml").write_text(BIKE_OUTLET_YAML)
        (config_dir / "garage-sensor.yaml").write_text(GARAGE_SENSOR_YAML)
        (config_dir / "secrets.yaml").write_text(SECRETS_YAML)
        yield config_dir


@pytest.fixture(scope="session")
def esphome_dashboard(esphome_config_dir: Path) -> Generator[str]:
    """Start an ESPHome dashboard subprocess and yield its base URL."""
    port = _find_free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "esphome",
            "dashboard",
            str(esphome_config_dir),
            "--port",
            str(port),
            "--address",
            "127.0.0.1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    base_url = f"http://127.0.0.1:{port}"

    # Wait for dashboard to become ready
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/version", timeout=2.0)
            if resp.status_code == 200:
                break
        except httpx.ConnectError:
            pass
        time.sleep(0.5)
    else:
        proc.kill()
        stdout, stderr = proc.communicate()
        raise RuntimeError(
            f"ESPHome dashboard did not start within 30s.\n"
            f"stdout: {stdout.decode()}\nstderr: {stderr.decode()}"
        )

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@pytest.fixture(scope="session")
def esphome_client(esphome_dashboard: str) -> Generator[None]:
    """Configure the shared client to point at the test dashboard."""
    settings = ESPHomeSettings(esphome_dashboard_url=esphome_dashboard)
    client_module.configure(settings)
    yield
    client_module.reset()


@pytest.fixture
async def mcp_client(esphome_client: None) -> AsyncGenerator[Client]:
    """Create a FastMCP in-memory test client."""
    async with Client(transport=mcp) as c:
        yield c
