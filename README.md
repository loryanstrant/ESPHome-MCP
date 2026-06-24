# ESPHome MCP

An [MCP](https://modelcontextprotocol.io) server for the **ESPHome 2026.6+ "Device
Builder"** dashboard. It lets an MCP client (Claude, etc.) list devices, read/edit/validate
device YAML, stream logs, and compile/flash firmware — speaking the dashboard's new
WebSocket command protocol.

> **Why this fork exists.** ESPHome **2026.6** replaced the dashboard's legacy HTTP API
> with a single WebSocket command protocol. The existing MCP servers
> ([`kdkavanagh/esphome-mcp`](https://github.com/kdkavanagh/esphome-mcp),
> [`b2un0/esphome-mcp`](https://github.com/b2un0/esphome-mcp),
> [`jrigling/esphome-mcp-integration`](https://github.com/jrigling/esphome-mcp-integration))
> all speak the old protocol, so config read/edit/validate return garbage against a 2026.6
> server. This project keeps the clean tool layer from `kdkavanagh/esphome-mcp` and rewrites
> the transport for the new protocol. See [`DECISIONS.md`](DECISIONS.md) for the details.

## Tools

| Tool | What it does |
| --- | --- |
| `list_devices` / `list_device_names` | Inventory configured devices |
| `check_device_update` | Is a firmware update available? |
| `get_device_status` | Online/offline + address |
| `get_device_version` | Deployed vs current version |
| `get_device_configuration` | Read a device's YAML |
| `edit_device_configuration` | Save YAML (then auto-validate) |
| `validate_device_configuration` | Full ESPHome validation, no save |
| `get_device_logs` | Stream recent device logs |
| `get_esphome_schema` | Component schema for a version |
| `install_device_configuration` | Compile + OTA flash (destructive) |
| `update_device` | Recompile + OTA flash to latest (destructive) |

## Configuration

Config is via environment variables (12-factor). Copy [`.env.example`](.env.example) to
`.env`:

| Variable | Required | Description |
| --- | --- | --- |
| `ESPHOME_DASHBOARD_URL` | yes | Dashboard base URL, e.g. `https://esphome.example.com` or `http://host:6052`. REST and WebSocket URLs are derived from it. |
| `ESPHOME_DASHBOARD_USERNAME` | no | Basic Auth user (only if the dashboard reports `requires_auth=true`). |
| `ESPHOME_DASHBOARD_PASSWORD` | no | Basic Auth password. |
| `LOG_LEVEL` | no | `DEBUG`/`INFO`/`WARNING`/`ERROR` (default `INFO`). |

## Run with Docker

```bash
cp .env.example .env       # then edit ESPHOME_DASHBOARD_URL
docker compose up -d --build
docker compose ps          # STATUS should become "healthy"
```

The server listens on `:8080` and serves MCP over **Streamable HTTP** at
`http://<host>:8080/mcp`. The container `HEALTHCHECK` performs a full MCP handshake and
calls `list_device_names`, so it only reports healthy when the dashboard is actually
reachable.

Once the registry image is published, pin it in `compose.yaml`:

```yaml
image: ghcr.io/loryans/esphome-mcp:latest
```

## Connect an MCP client

Point your client at the Streamable HTTP endpoint:

```json
{
  "mcpServers": {
    "esphome": { "type": "http", "url": "http://<host>:8080/mcp" }
  }
}
```

For a stdio client, run `esphome-mcp` (instead of the web entrypoint) with the same env.

## Develop

```bash
make install-dev   # venv + deps
make check         # lint + format-check + typecheck + test

# live tests against a real 2026.6 dashboard:
ESPHOME_DASHBOARD_URL=https://esphome.example.com .venv/bin/pytest -m live
```

## Credits

This project stands on the work of others (all MIT-licensed):

- **[kdkavanagh/esphome-mcp](https://github.com/kdkavanagh/esphome-mcp)** — the original
  ESPHome MCP server. This fork keeps its FastMCP tool layer, schema handling, packaging
  and CI almost verbatim; the transport rewrite is the main change here.
- **[b2un0/esphome-mcp](https://github.com/b2un0/esphome-mcp)** — for publishing a prebuilt
  image and surfacing the healthcheck / config-tool breakage that motivated this work.
- **[jrigling/esphome-mcp-integration](https://github.com/jrigling/esphome-mcp-integration)**
  — a Home Assistant integration referenced while mapping the ESPHome dashboard protocol.

The new 2026.6 WebSocket protocol was reverse-engineered from the ESPHome Device Builder
front-end and verified against a live 2026.6 dashboard.

## License

[MIT](LICENSE).
