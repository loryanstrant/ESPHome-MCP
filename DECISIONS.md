# Decisions & lessons

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
