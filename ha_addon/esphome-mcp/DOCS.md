# ESPHome MCP — Home Assistant Add-on

An [MCP](https://modelcontextprotocol.io) server for the ESPHome 2026.6+ "Device
Builder" dashboard, packaged as a Home Assistant Supervisor add-on. See the
[main README](https://github.com/loryanstrant/esphome-mcp) for what the server can
do.

## Installation

1. In Home Assistant, go to **Settings → Apps → Install app**, then use the
   **⋮ → Repositories** menu in the top-right corner to add:

   ```
   https://github.com/loryanstrant/esphome-mcp
   ```

   (Home Assistant renamed "Add-ons" to "Apps" in the UI as of 2026.2 — this is
   still a Supervisor add-on under the hood, just presented under the Apps
   section now.)

2. Install **ESPHome MCP** from the app store.
3. `dashboard_url` already defaults to `http://homeassistant.local:6052` — correct
   out of the box if you're running the official **ESPHome Device Builder**
   app on this same Home Assistant host (the common case). Open the
   **Configuration** tab only if that's wrong for your setup, or your dashboard
   needs `dashboard_username`/`dashboard_password`. Everything is configured
   through this tab — no YAML editing needed.
4. Start the app.

## Connecting an MCP client

The ingress status page has no fixed URL — Home Assistant generates a random,
per-install token for it (`/api/hassio_ingress/<token>/...`) and only exposes it
through an authenticated session, so it can't be linked to directly. To reach it:
open this app's page (**Settings → Apps → ESPHome MCP**), go to its **Info**
tab, and click **Open Web UI**. That page shows dashboard connectivity and the
exact connect URL to give your MCP client, including the live port and secret
path, e.g.:

```
http://<your-home-assistant-ip-or-hostname>:8080/private_<random-token>
```

Replace `<your-home-assistant-ip-or-hostname>` with the address you use to reach
Home Assistant itself (same host, *without* the `:8123` — this add-on listens on
its own port, separate from the main HA web UI). The `8080` above is the default;
if you've remapped the add-on's port in its **Network** section, use that port
instead — the ingress panel always reflects the current value.

The path after the port (`/private_<random-token>`) is a high-entropy secret
generated on first start and persisted across restarts — it's the credential that
protects the endpoint, so treat the full URL like a password. It's also logged
once at startup (**Log** tab) if you need it without opening the status page. To
pin a specific value instead of the generated one, set the `secret_path` option
in the Configuration tab.

**To rotate it** (e.g. you suspect it leaked): turn on `regenerate_secret_path`
in the Configuration tab and restart the add-on. A fresh secret is generated,
the option is automatically switched back off so it won't regenerate again on
every subsequent restart, and any MCP client using the old URL will need the
new one from the status page or logs.

## Options

| Option | Required | Description |
| --- | --- | --- |
| `dashboard_url` | yes | Your ESPHome dashboard's base URL. Defaults to `http://homeassistant.local:6052` (correct for the common co-located setup) — change it if your dashboard runs elsewhere. |
| `dashboard_username` | no | Basic Auth user, only if the dashboard requires it. |
| `dashboard_password` | no | Basic Auth password. |
| `log_level` | no | `debug` / `info` / `warning` / `error` (default `info`). |
| `secret_path` | no | Pin the MCP endpoint's path instead of using the auto-generated one. Must start with `/` and be at least 8 characters. |
| `regenerate_secret_path` | no | Set `true` and restart to force a fresh secret path; automatically resets to `false` afterward. |

## Changing the port

The app listens on port `8080` by default. If that collides with something
else on your host, remap it in its **Info → Network** section — no YAML editing
required. The ingress status page always shows the port actually in use.

## Uninstalling

Stop and remove the app from the App Store as usual. Its persisted secret path
is stored under its own `/data` and is removed along with it.
