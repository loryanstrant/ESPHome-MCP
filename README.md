# ESPHome MCP

An [MCP](https://modelcontextprotocol.io) server for the **ESPHome "Device Builder"**
dashboard (2026.6 and newer). It lets an MCP client (Claude, etc.) list devices,
read/edit/validate device YAML, stream logs, run diagnostics, and compile/flash firmware —
speaking the dashboard's WebSocket command protocol.

**Since ESPHome 2026.10 the dashboard has an MCP server of its own**, so this one is no
longer the only route. It is now best understood as the **diagnostic companion** to that
built-in server: six of its tools have no built-in equivalent, and it stays useful for
clients that cannot carry credentials. Read the next section before you wire either one up.

## Which server should I use?

ESPHome **2026.10.0** bundles Device Builder **1.25.0**, which serves its own MCP endpoint at
**`POST /api/mcp`** — always on, no flag, **21 tools**. Ten of this server's sixteen tools
overlap it, and on the overlap the two are equivalent: measured live against a 2026.10.0
dashboard, `get_config` and `get_device_configuration` returned **byte-for-byte identical**
YAML, the device facts matched field for field, and validation gave the same verdict.

So the choice is not about correctness. It is about these differences:

| | this server | the dashboard's built-in `/api/mcp` |
| --- | --- | --- |
| Tools | 16 | 21 |
| Full device list (38 devices) | **9.4 kB** — a summary written to be read | **42.7 kB** — raw JSON, 43 fields per device |
| Device names only | 0.6 kB (`list_device_names`) | no equivalent — the full list is the only option |
| Tool schemas per session | 12.9 kB | 16.9 kB |
| Overwrite protection on save | **none** — `edit_device_configuration` writes regardless | **yes** — `update_config` refuses unless you pass the text you read |
| Credentials | logs in itself from `.env` | the **client** must send HTTP Basic; a bare request gets `401` |
| Where it runs | its own container, own URL, own healthcheck | inside the dashboard |

**Use the built-in one for everyday device work** — listing, reading, editing, validating,
compiling, installing, plus everything in the "does better" list below. **Use this one for
diagnostics and bulk inspection**: logs, connectivity probes, crash decoding, YAML migration,
searching every config at once, and per-version schemas.

### What only this server does

None of these exist as built-in MCP tools, on 1.25.0 or on 1.26.0. All six are thin wrappers
over Device Builder commands the dashboard already implements — they are simply not wired to
its MCP surface:

| Tool | Dashboard command it wraps |
| --- | --- |
| `get_device_logs` | `devices/logs` |
| `troubleshoot_device` | `devices/troubleshoot` |
| `decode_device_backtrace` | `devices/decode_backtrace` |
| `migrate_device_configuration` | `editor/migrate_config` |
| `search_device_configurations` | `yaml/search` |
| `get_esphome_schema` | the `esphome-schema` release archive — the built-in `get_component` takes no ESPHome version, so it can only describe the version the dashboard itself runs |

The intended endgame is unchanged: offer these upstream and retire this repo, rather than
maintain a fork of tools the dashboard now owns.

### What the built-in one does better

- **`update_config` cannot clobber.** It requires an `expected` copy of the text you read and
  answers `precondition_failed` (with a diff) if the file moved under you. This server's
  `edit_device_configuration` has no such guard and will overwrite.
- **Component and board catalogues** — `search_components`, `get_component`,
  `get_config_components`, `search_boards`.
- **Editing automations** — `list_automations`, `get_available_automations`,
  `get_automation_docs`, `upsert_automation`, `delete_automation`, with a real YAML splice.
- **`add_component`**, **`create_device`**, **`set_secret`** / `list_secret_names`.
- **Separate compile and job control** — `compile`, `get_job`, `cancel_job`.

### A note on credentials

If your dashboard has a password set, the built-in endpoint returns **401** until the client
sends HTTP Basic auth — so a client that cannot attach headers cannot use it, which is one
reason this server (which logs in from `.env`) is still the easier thing to point such a
client at. The flip side: this server's own `:8080` has **no authentication of its own**,
so anything that can reach it can drive your dashboard, including firmware installs. Keep it
off untrusted networks.

> **Why this fork exists.** ESPHome **2026.6** replaced the dashboard's legacy HTTP API
> with a single WebSocket command protocol. The existing MCP servers
> ([`kdkavanagh/esphome-mcp`](https://github.com/kdkavanagh/esphome-mcp),
> [`b2un0/esphome-mcp`](https://github.com/b2un0/esphome-mcp),
> [`jrigling/esphome-mcp-integration`](https://github.com/jrigling/esphome-mcp-integration))
> all speak the old protocol, so config read/edit/validate return garbage against a 2026.6
> server. This project keeps the clean tool layer from `kdkavanagh/esphome-mcp` and rewrites
> the transport for the new protocol. See [`DECISIONS.md`](DECISIONS.md) for the details.

> **Dashboard versions.** The Device Builder ships from
> [`esphome/device-builder`](https://github.com/esphome/device-builder) on its own release
> cadence, so its `server_version` is independent of the ESPHome version — **2026.10.0 ships
> Device Builder 1.25.0**, 2026.9.x ships 1.14.9, 2026.8.0 ships 1.12.x, 2026.7.3 ships
> 1.7.0, and ESPHome's `dev` branch pins 1.26.0. (The pin is in ESPHome's own
> `docker/Dockerfile`, which is the only place it is written down — and the only reliable way
> to tell whether a given ESPHome release has the built-in MCP server, since ESPHome's own
> changelog does not mention it.) This server is written against the 1.14.x protocol, verified
> against 1.25.0, and falls back to the pre-1.5.0 device shape where they differ. Its protocol
> reference is that repo's `docs/API.md` and `models/devices.py`.

> **Upgrading from 2026.06.0?** On ESPHome 2026.7 or newer it reported every device as
> `unknown` with no deployed version, and could report a successful install for firmware it
> never flashed. Both are fixed in 2026.08.0 — see the [changelog](CHANGELOG.md).

## Tools

**●** the dashboard's built-in MCP server also does this (prefer it there) ·
**◆** only this server does this

| | Tool | What it does |
| --- | --- | --- |
| ● | `list_devices` / `list_device_names` | Inventory configured devices |
| ● | `check_device_update` | Is a firmware update available? |
| ● | `get_device_status` | Online/offline + address |
| ● | `get_device_version` | Deployed vs current version |
| ● | `get_device_configuration` | Read a device's YAML |
| ● | `edit_device_configuration` | Save YAML (then auto-validate) |
| ● | `validate_device_configuration` | Full ESPHome validation, no save |
| ◆ | `migrate_device_configuration` | Respell legacy YAML keys for the installed ESPHome (dry run by default) |
| ◆ | `search_device_configurations` | Search every device's YAML for a string |
| ◆ | `get_device_logs` | Stream recent device logs |
| ◆ | `troubleshoot_device` | Live connectivity probe (DNS, mDNS, ping) |
| ◆ | `decode_device_backtrace` | Decode a crash backtrace into source locations |
| ◆ | `get_esphome_schema` | Component schema for a version |
| ● | `install_device_configuration` | Compile + OTA flash (destructive) |
| ● | `update_device` | Recompile + OTA flash to latest (destructive) |

The four ● device-fact tools (`check_device_update`, `get_device_status`,
`get_device_version`, and the inventory) are all one built-in `list_devices` call upstream —
it returns those fields inline. `update_device` has no distinct built-in verb; re-running the
built-in `install` does the same thing.

> **Offline devices.** If a device is offline, the dashboard compiles the firmware and arms
> it to flash on the device's next check-in. `install_device_configuration` and
> `update_device` report that as `COMPILED, FLASH DEFERRED` — not success.

## Configuration

Config is via environment variables (12-factor). Copy [`.env.example`](.env.example) to
`.env`:

| Variable | Required | Description |
| --- | --- | --- |
| `ESPHOME_DASHBOARD_URL` | yes | Dashboard base URL, e.g. `https://esphome.example.com` or `http://host:6052`. REST and WebSocket URLs are derived from it. |
| `ESPHOME_DASHBOARD_USERNAME` | no | Dashboard user. **Required** if the dashboard reports `requires_auth=true` — without it every command fails with `not_authenticated`. |
| `ESPHOME_DASHBOARD_PASSWORD` | no | Dashboard password. |
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
image: ghcr.io/loryanstrant/esphome-mcp:latest
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
