# Changelog

Versions are dated (`YYYY.MM.N`) and track the ESPHome release they were tested against.

Two version numbers matter when reading these notes: **ESPHome** (2026.8.0) and the
**Device Builder** dashboard (1.12.x). They ship separately and move independently, so the
dashboard version is what actually decides which of this server's tools work. Your dashboard
reports both on the first frame of its WebSocket connection, and this server logs them at
startup:

```
Connected to ESPHome Device Builder 2026.8.0 (server 1.12.1, requires_auth=False)
                                    ^ESPHome         ^Device Builder
```

---

## 2026.08.1 — 2026-08-20

**Upgrade from 2026.08.0 before using `migrate_device_configuration` with `apply=True`.**

### Fixed

- **`migrate_device_configuration(apply=True)` could save a broken configuration.** The
  splice that applies the dashboard's migration diff was off by one line, so instead of
  replacing the old setting it inserted the new one *next to* it. Migrating a light from
  `rgb_order` to `channel_colors`, for example, produced a config containing both, which
  ESPHome rejects with `'channel_colors' cannot be combined with 'rgb_order'`.

  Nothing was silently destroyed — the tool validates after saving, so you would have seen
  the error, and no firmware is flashed as part of a migration. But you would have had to
  repair the file by hand. Dry runs (the default, `apply=False`) were never affected: they
  only ever displayed the diff.

- **Migration summaries were unreadable.** Every proposed change printed as a bare `- fold`.
  They now name the setting, where it lives, and when it changed:

  ```
  light.esp32_rmt_led_strip: rgb_order -> channel_colors (fold, since 2026.8.0b5, removed in 2027.3.0)
  ```

  Changes that the installed ESPHome *already rejects* — as opposed to ones it still accepts
  with a deprecation warning — are now flagged `REQUIRED`, so you can tell "must fix now"
  from "tidy up before 2027".

---

## 2026.08.0 — 2026-08-20

The big one. **If your device list shows every device as `unknown` with no deployed version,
this release fixes it.**

Tested against ESPHome 2026.8.0 / Device Builder 1.12.x, and against 2026.7.3 / 1.7.0.

### Fixed

- **Every device reported `unknown` status and `n/a` versions.** This affected anyone running
  ESPHome **2026.7 or newer** (Device Builder 1.5.0+), and it affected four tools at once:
  `list_devices`, `get_device_status`, `get_device_version` and `check_device_update`.

  The dashboard moved the live fields it observes over the network — online/offline state,
  deployed version, IP addresses — into a nested `runtime_state` object, and stopped
  publishing them at the top level. This server was still reading the old location, got
  nothing back, and dutifully reported "unknown" for a fleet that was perfectly healthy.
  There was no error message anywhere, which is why it went unnoticed for a while.

  The fix reads the new location and falls back to the old one, so **older dashboards keep
  working unchanged**.

- **`install_device_configuration` and `update_device` could report `SUCCESS` without
  flashing anything.** The dashboard splits an install into two jobs — compile, then upload —
  and only hands back the compile. This server watched the compile, saw it succeed, and
  called the whole thing done. If the upload then failed, you were told the opposite.

  Worse: when the target device is **offline**, the dashboard doesn't upload at all. It
  compiles the firmware and arms it to flash the next time the device checks in. That also
  reported `SUCCESS`, for a device that was never touched.

  Both now follow the upload job through to its real result. An offline device reports
  `COMPILED, FLASH DEFERRED` and explains what will happen, and a failure says which stage
  broke.

### Added

Four tools, all backed by dashboard features that already existed:

- **`migrate_device_configuration`** — ESPHome renames configuration keys between releases,
  and 2026.8.0 renamed a lot of them (`esp32_ble_id:` → `ble_hub_id:`, the sgp4x/sen5x/sen6x
  `voc`/`nox` keys, the addressable-light colour-order keys, the modbus throttle settings).
  This asks the dashboard to respell a device's YAML for the ESPHome you actually have
  installed. **It's a dry run by default** — it shows you the diff and saves nothing until
  you pass `apply=True`. *(Requires Device Builder 1.8.0+.)*

- **`search_device_configurations`** — search every device's YAML at once. Answers "which of
  my devices use `dallas`?" in one call instead of reading each config in turn, and it's how
  you'd check whether an ESPHome upgrade is going to touch anything of yours. Note it
  searches each device's main file, not `!include`d fragments.

- **`troubleshoot_device`** — a live connectivity probe: fresh DNS lookup, mDNS re-query and
  a ping, rather than the dashboard's cached view. For when a device shows offline and you
  want to know *which* part of the path is failing. *(Requires Device Builder 1.9.0+.)*

- **`decode_device_backtrace`** — paste a crash from `get_device_logs` and get source
  locations back, decoded against the build on your dashboard. Tells you honestly when it
  can't help — no build on disk, or a build newer than the firmware that crashed.

### Changed

- **`check_device_update` is more accurate.** It now uses the dashboard's own verdict instead
  of comparing version strings, which means it can finally tell "compiled against an older
  ESPHome" apart from "compiled but not yet flashed to the device".
- **`list_devices` shows more.** Each device can now be flagged with an available ESPHome
  update, unsaved config changes since its last compile, or an available YAML migration.
- **`get_device_status` reports the device's IP** and which channel the dashboard is hearing
  it on (mDNS, MQTT or ping).
- Tools that need a newer dashboard than you're running now say so, naming the version that
  added the feature, instead of failing with a bare `Unknown command`.
- Version lookups use the current dashboard command rather than a REST endpoint that ESPHome
  has marked deprecated.

### Compatibility

Nothing here requires you to upgrade ESPHome. The fixes above apply to dashboards you are
already running; the two tools marked above simply report that they're unavailable on older
ones. Python 3.13+, as before.

---

## 2026.06.0 — 2026-06-24

First release of this fork.

ESPHome **2026.6** replaced the dashboard's HTTP API with a single WebSocket command
protocol. Every existing ESPHome MCP server spoke the old one, so reading, editing and
validating configurations returned HTML instead of YAML against a 2026.6+ dashboard. This
release keeps the tool layer and rewrites the transport to speak the new protocol.

See [`DECISIONS.md`](DECISIONS.md) for the protocol details.
