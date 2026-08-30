# Decisions & lessons

## 2026-08-30 — the HTTP transport runs stateless (it was leaking every session)

**Context.** `esphome-mcp` served its Streamable HTTP transport in fastmcp's default
**stateful** mode. A gateway (MetaMCP, here) opens a fresh MCP session per burst of tool
calls and never sends `DELETE /mcp`; fastmcp 3.4.x only evicts a session from
`StreamableHTTPSessionManager._server_instances` when that session's server task ends,
which never happens for a connection that is simply dropped. Every session ever served
stays resident.

Measured across the homelab's FastMCP wrappers — it tracks the library, not the workload:

| Server | fastmcp / mcp | sessions served | memory | per session |
|---|---|---|---|---|
| GPT-Researcher-MCP | 3.3.1 / 1.27.2 | 90,372 | 200 MiB | 2.3 KB (flat) |
| SnapOtter-MCP | 3.4.4 / 1.28.1 | 28,096 | 1000 MiB | **36 KB** |
| CaddyUI-MCP | 3.4.5 / 1.29.0 | 51,682 | 2.7 GiB | **55 KB** |
| esphome-mcp | 3.4.7 / 1.29.0 | 9,203 | 174 MiB | growing |

Driving 500 fresh sessions at the sibling wrapper added 27,804 kB — **57 KB leaked per
session**, and only 221 MB of its 1 GB was `Referenced`; the rest was cold, dead session
state.

**Decision.** `main_web()` runs `mcp.run(..., stateless_http=True)`. No tool here uses
`Context`, progress, or server-initiated notifications, so per-session state buys nothing.

**Consequence.** Stateless mode drops `GET /mcp` and returns **405**. That is correct and
expected: the MCP spec says a server MAY answer the GET stream with 405, and clients
handle it — the TypeScript SDK treats 405 as "server offers no SSE stream" and continues
(`client/streamableHttp.js:96`). `tests/test_tools.py::test_http_app_is_stateless` pins
this, with a paired test so the 405 cannot pass for a broken route. The stdio entrypoint
(`main()`) is unaffected — stdio has no sessions.

**Also.** `fastmcp>=2.0.0` was open-ended, so an image rebuild silently picked up whatever
was current — which is how this regression arrived. Both `fastmcp` and `mcp` are now
bounded to a minor line. Bump them deliberately, and re-measure with a session-churn probe
when you do.

**Lesson (reusable).** Any MCP server behind a gateway should run stateless unless it
genuinely needs server-push. A leak like this is invisible in every functional test — the
server keeps answering correctly, it just never gives the memory back — so the check that
finds it is "drive N sessions, read RSS twice", not a status code.

## 2026-08-20 — The dashboard is versioned separately, and its wire shape moved

**Context.** The transport below was reverse-engineered against a dashboard reporting
`server_version 1.0.12` / `esphome_version 2026.6.2`. Those are **two independent version
numbers**: the Device Builder ships from its own repo,
[`esphome/device-builder`](https://github.com/esphome/device-builder), on its own release
cadence. ESPHome 2026.8.0 ships Device Builder **1.12.x**; a dashboard on ESPHome 2026.7.3
reports **1.7.0**. Tracking the ESPHome changelog alone will miss dashboard-protocol changes
entirely — 2026.8.0's changelog says nothing about either change below.

That repo now publishes **`docs/API.md`** (every WebSocket command, args and response) and
**`esphome_device_builder/models/devices.py`** (the exact `Device` wire shape). Those are the
authoritative reference — read them instead of reverse-engineering the SPA bundle. Note
`docs/API.md` is not exhaustive: `editor/validate_yaml` is live in `controllers/editor.py`
but absent from the doc, so check the source before concluding a command is gone.

**Two breaking changes were found already live, both silent.**

1. **`Device.runtime_state` (Device Builder 1.5.0).** The monitor-observed fields — `state`,
   `active_source`, `ip_addresses`, `deployed_version`, `deployed_config_hash`,
   `queued_update`, `api_encryption_active`, `deployed_identity_live` — moved off the flat
   `Device` into a nested `runtime_state` object. **There is no flat alias on the WebSocket
   wire.** Only the deprecated legacy REST `GET /devices` still flattens it, for Home
   Assistant's `esphome-dashboard-api`. Reading them flat returns nothing, so
   `list_devices`, `get_device_status`, `get_device_version` and `check_device_update`
   reported "unknown" / "not yet flashed" for *every* device — plausible-looking output, no
   error anywhere. Handled by `runtime_field()` in `client.py`, which prefers the nested
   value and falls back to the flat one for pre-1.5.0 dashboards.

   The same reshuffle added first-class flags that are better than inferring from version
   strings: `update_available`, `has_pending_changes` (+ `pending_changes_via_hash`) and
   `migration_available`. `check_device_update` now uses `update_available`, which
   distinguishes "compiled against an older ESPHome" from "compiled but not yet flashed" —
   a `deployed != current` comparison cannot.

2. **`firmware/install` returns the COMPILE job, not the install.** The OTA upload is a
   *separate* job on a separate lane, chained via `depends_on` (`enqueue_install_chain` in
   `controllers/firmware/factories.py`). Following only the returned job reports success as
   soon as compilation finishes, for firmware that may never have reached the device. Worse:
   if the device is **offline** and `port` is `"OTA"`, the dashboard queues a compile-only
   job flagged `is_deferred_install` and arms it to flash on the device's next check-in — so
   the old code reported `SUCCESS` for a device that was never touched. `install_configuration`
   now returns an `InstallOutcome` carrying the upload's exit code, the failing stage, and a
   `deferred` flag reported as "COMPILED, FLASH DEFERRED".

**Lessons (reusable).**
- **A silent shape change is worse than a broken endpoint.** The 2026.6 breakage announced
  itself (HTML where YAML was expected); this one just returned "unknown" forever. The live
  test `test_live_device_carries_runtime_state` pins the shape so the next one fails loudly.
- **Version-check the dashboard, not just ESPHome.** `server_version` is in the `server_info`
  frame we already log at INFO on connect.
- **A queued job is not a finished job.** Where a backend splits work across a chain, follow
  the chain — the first job's exit code is not the operation's outcome.

**Commands added in this pass** (all from `docs/API.md`): `editor/migrate_config`
(2026.8 renames a lot of keys — `esp32_ble_id:`→`ble_hub_id:`, `voc`/`nox`→`voc_index`/
`nox_index`, `rgb_order`/`is_rgbw`/`is_wrgb`→`channel_colors`), `yaml/search`,
`devices/troubleshoot`, `devices/decode_backtrace`. `config/version` replaces the legacy
REST `GET /version`, which is on the dashboard's deprecated list.

## 2026-06-24 — ESPHome 2026.6 "Device Builder" replaced the dashboard API

**Context.** The original `kdkavanagh/esphome-mcp` (and the `b2un0` fork that publishes
an image) spoke the *legacy* ESPHome dashboard API: REST `GET/POST /edit?configuration=`
to read/write YAML and per-action WebSocket "spawn" endpoints (`/validate`, `/compile`,
`/run`, `/logs`). Against an ESPHome **2026.6** server those tools return garbage:

- `GET /edit?configuration=<f>` now returns the SPA **HTML shell** (HTTP 200), not YAML.
- `GET /json-config?...` → HTTP 500.
- `GET /validate` → SPA HTML; the legacy spawn WebSockets are gone.
- `GET /devices` and `GET /version` still work (plain REST).

This is why `get_device_configuration`, `edit_device_configuration` and
`validate_device_configuration` were broken — not a fork bug, an upstream API replacement.
`jrigling/esphome-mcp-integration` is affected too (it speaks the same legacy protocol).

**Decision.** Keep the clean FastMCP tool layer from `kdkavanagh/esphome-mcp` and rewrite
**only the transport** (`client.py`) to speak the new protocol. The 12-tool surface is
unchanged.

**The new protocol (reverse-engineered from the SPA bundle + verified live).**
A single persistent WebSocket carries all commands.

- URL: `ws(s)://<host>/ws` (the dashboard base href is `/`). Works directly and through a
  reverse proxy (Caddy) over HTTP/1.1.
- On connect the server pushes one **server_info** frame, e.g.
  `{"server_version":"1.0.12","esphome_version":"2026.6.2","requires_auth":false}`.
  If `requires_auth` is true, send `auth/login` and use the returned token.
- Request: `{"command":"<ns/action>","message_id":<int>,"args":{...}}`.
- Responses correlate by `message_id` — **returned as a string**:
  - success: `{"message_id":"1","result":<payload>}`
  - failure: `{"message_id":"1","error_code":"not_found","details":"..."}`
  - stream line: `{"message_id":"1","event":"output","data":"<line>"}`
  - stream end:  `{"message_id":"1","event":"result","data":<final payload>}`

Commands used by this server:

| Purpose | Command | Result |
| --- | --- | --- |
| List devices | `devices/list` | `{configured:[...], importable:[...]}` |
| Read YAML | `devices/get_config {configuration}` | raw YAML string |
| Save YAML | `devices/update_config {configuration, content}` | — |
| Validate | `editor/validate_yaml {configuration, content}` | `{yaml_errors:[], validation_errors:[]}` (empty = valid; does full ESPHome validation) |
| Logs | `devices/logs {configuration, port}` (stream) | output frames |
| Compile | `firmware/compile {configuration}` → `firmware/follow_job {job_id}` | job object w/ `exit_code` |
| Install (OTA) | `firmware/install {configuration, port:"OTA", force_local:false}` → `firmware/follow_job` | job object w/ `exit_code` |
| Keepalive | `ping` | `{pong:true}` |

**Lessons (reusable).**
- `message_id` echoes back as a **string** — correlate with `str(id)`, not the int.
- `editor/validate_yaml` does full component-level validation (caught "Platform missing"),
  so it is a complete replacement for the old `/validate` spawn — no local `esphome` needed.
- Long jobs (compile/install) are async: the command returns a `job_id` immediately; stream
  output via `firmware/follow_job` until the terminal `event:"result"` frame, whose `data`
  carries the final job object (incl. `exit_code`). `firmware/get_job` is a poll fallback.
- Verify against the real 2026.6 server, not a proxy — an HTTP 200 from `/edit` is the SPA
  shell, which silently looks "fine" while returning HTML instead of YAML.
- The MCP Streamable HTTP endpoint is `/mcp` (no trailing slash); `/mcp/` returns 307.
