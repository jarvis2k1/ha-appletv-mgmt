# Apple TV Mgmt — Specification

**Status:** Phase 1 MVP shipped (v0.2.0). Phase 2/3/4 planned — see [the roadmap](#roadmap) at the bottom.

## 1. Goal

Give a Home Assistant operator (a parent) hard control over how long an Apple TV is usable per day, with:

- **Accurate per-app attribution** of time spent on the Apple TV.
- **Graceful enforcement** when the daily budget is exhausted (AdGuard block + sleep).
- **Self-service kid loop** where the kids can request more time and the parent approves from their phone.
- **Auditable, deterministic state** — every minute of usage is logged, every decision is reproducible.

## 2. Non-goals

- This is not a content filter. It blocks the device (or all of its DNS), not specific shows.
- It does not impersonate the user; it cannot defeat a determined kid with their own phone hotspot.
- It is not a multi-tenant service. One Apple TV → one profile (per config entry). v2 may add a "who's watching" picker for shared devices.

## 3. Architecture

```
┌────────────────────┐  state change events     ┌──────────────────────────┐
│  media_player.     │ ───────────────────────▶ │  AppleTVMgmtCoordinator  │
│  living_room_apple_tv       │       app_id, state      │  (DataUpdateCoordinator) │
│  (built-in Apple   │                          │                          │
│   TV integration,  │ ◀── service: turn_off ── │  - per-tick budget calc  │
│   pyatv-based)     │                          │  - midnight reset        │
└────────────────────┘                          │  - drives state machine  │
                                                └─────────────┬────────────┘
                                                              │ used_seconds, now
                                                              ▼
                                              ┌──────────────────────────────┐
                                              │  state.compute_next_state    │
                                              │  (pure function)             │
                                              │  OK → WARNING → GRACE →      │
                                              │  ENFORCING → OK              │
                                              └─────────────┬────────────────┘
                                                            │ StateDecision
                                                            ▼
                                              ┌──────────────────────────────┐
                                              │  EnforcementController       │
                                              │  - calls AdGuardClient       │
                                              │  - calls media_player.       │
                                              │    turn_off                  │
                                              │  - fires HA bus events       │
                                              └─────────────┬────────────────┘
                                                            │
                                          ┌─────────────────┴─────────────────┐
                                          ▼                                    ▼
                                ┌────────────────────┐              ┌────────────────────┐
                                │  AdGuardClient     │              │  HA Service Bus    │
                                │  (HTTP /control/*) │              │  - turn_off        │
                                └─────────┬──────────┘              └────────────────────┘
                                          │ X-API-Key
                                          ▼
                       ┌───────────────────────────────────────┐
                       │  appletv-adguard-proxy addon          │
                       │  (host_network, scans /proc/net/tcp,  │
                       │   forwards to AdGuard's localhost     │
                       │   ephemeral port)                     │
                       └─────────────────┬─────────────────────┘
                                         │ HTTP
                                         ▼
                       ┌───────────────────────────────────────┐
                       │  AdGuard Home addon                   │
                       │  bound 127.0.0.1:<ephemeral>          │
                       └───────────────────────────────────────┘
```

The proxy exists because the official AdGuard Home addon runs ingress-only and binds its REST API to `127.0.0.1` on a random port — unreachable from the HA Core container. See [the proxy repo](https://github.com/jarvis2k1/ha-appletv-mgmt-adguard-proxy) for its own spec.

## 4. Data model

All entities persist in HA's `.storage` under the key `appletv_mgmt_data`. Storage version `1`.

### `Profile`

One per config entry. Created from the config flow.

| Field | Type | Notes |
|---|---|---|
| `id` | str | Slug — equals the HA config entry ID. |
| `display_name` | str | Free-form, shown in UI. |
| `apple_tv_entity_id` | str | The `media_player.*` entity ID from the built-in Apple TV integration. |
| `adguard_client_name` | str | Must match an existing `Persistent client` name in AdGuard, case-sensitive. |
| `daily_budget_min` | int | Minutes per local day. Resets at local midnight. |
| `grace_seconds` | int | How long we stay in GRACE before flipping to ENFORCING. Default 60. |
| `warn_thresholds_min` | list[int] | Minute-remaining marks at which the WARNING state may be entered. Default `[5, 2, 0]`. |
| `idle_grace_minutes` | int | When the device is on but pyatv reports no app, keep attributing to the last-known bundle id for this long. Default 5. |
| `tv_entity_id` *(0.4.0)* | str \| None | Optional second target for hard shutdown — any HA `media_player.*` entity (Samsung, LG, Sony, generic IR …). Default `None`. |
| `tv_shutdown_enabled` *(0.4.0)* | bool | Toggle for the TV-shutdown behaviour. Default `False`. Backed by `switch.<profile>_tv_shutdown`. |
| `quiet_windows` *(0.5.0)* | str | Comma-separated `HH:MM-HH:MM[:Label]` list. During any window, enforcement is forced regardless of remaining budget. Crosses-midnight is supported (when `start > end`). Default `""` (disabled). |
| `group_budgets` *(0.6.0)* | `dict[str, int]` | Per-group daily budget in minutes. Keys: `movies`, `tv_shows`, `gaming`, `other`. Missing key = no limit for that group. |
| `adult_mode_duration_min` *(0.6.0)* | int | When `switch.<profile>_adult_mode` is turned on, all enforcement is bypassed for this many minutes. Default 120. |

### `UsageEvent` (append-only)

One per stretch of time the Apple TV was attributed to a single bundle id.

| Field | Type | Notes |
|---|---|---|
| `id` | str | `<unix-millis>-<profile_id>`. |
| `profile_id` | str | FK into `Profile`. |
| `bundle_id` | str | App bundle id (e.g. `com.google.ios.youtube`). The sentinel `"unknown"` is used when the device is on but pyatv reports no app *and* there's no last-known fallback. |
| `started_at` | datetime (UTC) | Inclusive. |
| `ended_at` | datetime (UTC) \| None | `None` means still open — exactly one event per profile may be open at any time. |

Retention: events older than 30 days are pruned at every store load/save. Open events are never pruned.

### `extensions_granted_today`

Mapping `profile_id -> minutes`. Add to today's effective budget. Cleared at local midnight.

### *(planned, Phase 2/3)*

- `AppPolicy` — per-app limits, per-profile.
- `ExtensionRequest` — kid-initiated request with parent decision.

## 5. State machine (enforcement)

Defined in [`state.py`](custom_components/appletv_mgmt/state.py) as a **pure function** with no I/O — fully unit-tested. All states act per-profile.

```
                  remaining > warn_threshold
            ┌──────────────────────────────────┐
            │                                  │
            ▼                                  │
    ┌───────────────┐    remaining ≤ warn      │
    │     OK        │ ───────────────────────▶ │   WARNING
    └───────┬───────┘                          │ ┌───────────┐
            │ remaining ≤ 0                    │ │           │
            │ (skip WARNING)                   │ │  (extension granted)
            ▼                                  │ │  remaining > 0 ─▶ OK
    ┌───────────────┐    remaining ≤ 0         │ │           │
    │    GRACE      │ ◀────────────────────────┘ └───────────┘
    │  (≤ grace_s)  │
    └───────┬───────┘
            │ now - grace_started_at ≥ grace_seconds
            ▼
    ┌───────────────┐
    │  ENFORCING    │
    │  - AdGuard    │
    │  - sleep      │
    └───────┬───────┘
            │ remaining > 0 (extension OR midnight reset)
            ▼
           OK
```

### App groups *(0.6.0)*

Every recorded `UsageEvent` carries a `bundle_id`. The integration maps each bundle id to one of four groups via three layers, in order:

1. **`categorize.CURATED`** — hardcoded dict for the top Apple TV apps. Authoritative.
2. **`AppleTVMgmtStore.app_categories`** — cached results from past iTunes lookups (filled lazily, persisted in `.storage/appletv_mgmt_data`).
3. **Apple iTunes Search API** — looked up once per new `bundle_id` (`https://itunes.apple.com/lookup?bundleId=<id>&country=US&media=software`). The `primaryGenreName` is mapped via `categorize.ITUNES_GENRE_TO_GROUP` and cached. Lookup failures fall back to group `"other"`.

Each group can carry its own daily minutes budget via `Profile.group_budgets`. The state machine's `effective_used` becomes the **maximum** of the overall used and the current-group's "budget hit" projection — so enforcement triggers on whichever runs out first.

When the user is in app X (group Y) and group Y's budget is gone, the state machine enters `ENFORCING`. If the user app-switches to app Z (group W, fine), the next tick re-evaluates and drops back to `OK`. AdGuard block toggles accordingly.

### Adult mode *(0.6.0)*

`switch.<profile>_adult_mode` flips on → `AppleTVMgmtStore.set_adult_mode_until(profile_id, now + duration)` is set. The coordinator passes `adult_mode_active=True` to `enforcer.evaluate()`, which short-circuits the state machine to `StateDecision(STATE_OK, None)` regardless of budget, group, or quiet windows. Usage continues to be recorded.

The store's `is_adult_mode_active()` auto-expires the value on next read after the `until` timestamp, so the switch reads `False` automatically after timeout — no separate timer needed.

### Quiet windows *(0.5.0)*

Before evaluating the state machine, the enforcer computes whether any of the profile's `quiet_windows` is currently active (local time). If yes:

- `effective_used = max(used, budget)` — feed the state machine an "over budget" used value. This makes `remaining ≤ 0`, which trips the normal `OK / WARNING → GRACE → ENFORCING` cascade. Same grace period as a real budget-exhaustion path.
- The window's label is published on the coordinator snapshot as `active_quiet_window` and as an attribute on `sensor.<profile>_enforcement_state`, so dashboards can show *why* the device is blocked.
- When the window ends and `remaining > 0`, the state machine transitions out of `ENFORCING` normally (AdGuard unblocks, the TV — if configured — does NOT auto-power-on).

Windows are parsed at `EnforcementController.__init__` from the comma-separated string in `Profile.quiet_windows`. Bad input is logged at ERROR and the windows list defaults to empty (fail-open).

### Inputs to a tick

| Input | Source |
|---|---|
| `current_state` | Controller's in-memory state. |
| `used_seconds` | `store.used_seconds_today(profile)` minus `extension_minutes_today * 60`. |
| `budget_seconds` | `profile.daily_budget_min * 60`. |
| `warn_threshold_seconds` | `max(profile.warn_thresholds_min) * 60`. |
| `grace_seconds` | `profile.grace_seconds`. |
| `grace_started_at` | Set when the machine first enters GRACE; carried forward inside GRACE. |
| `now` | UTC at evaluation time. |

### Transitions (canonical)

| From | Condition | To | Notes |
|---|---|---|---|
| any | `remaining > warn_threshold` | `OK` | Cheap exit; clears `grace_started_at`. |
| `OK` | `0 < remaining ≤ warn_threshold` | `WARNING` | |
| `WARNING` | `remaining ≤ 0` | `GRACE` | Sets `grace_started_at = now`. |
| `OK` | `remaining ≤ 0` (skipped WARNING) | `GRACE` | |
| `GRACE` | `now - grace_started_at < grace_seconds` | `GRACE` | Stays. |
| `GRACE` | `now - grace_started_at ≥ grace_seconds` | `ENFORCING` | Side effects fire. |
| `ENFORCING` | `remaining > 0` | `OK` | Side effects undo. |
| `ENFORCING` | `remaining ≤ 0` | `ENFORCING` | Idempotent. |
| `WARNING` | `remaining > warn_threshold` | `OK` | Extension shortcut. |
| `WARNING` / `OK` | `0 < remaining ≤ warn_threshold` | `WARNING` | |

`remaining = budget_seconds - used_seconds`.

### Side effects on entry / exit

| Transition | Side effect |
|---|---|
| `→ ENFORCING` | `AdGuardClient.set_blocked(client, True)` then `media_player.turn_off(apple_tv_entity_id)`. *(0.4.0)* If `profile.tv_shutdown_enabled and profile.tv_entity_id`, also `media_player.turn_off(tv_entity_id)`. Fires `appletv_mgmt_enforcement_changed`. |
| `ENFORCING →` (any other state) | `AdGuardClient.set_blocked(client, False)`. The TV is NOT auto-powered back on — users get out of the lock by resuming the Apple TV (which usually wakes the TV via CEC) or by hand. Fires `appletv_mgmt_enforcement_changed`. |
| Any state change | Fires `appletv_mgmt_enforcement_changed` on the HA bus. |
| Every tick | Fires `appletv_mgmt_usage_updated` on the HA bus. |

If AdGuard returns an error, we log a `_LOGGER.error` and continue — the integration never breaks because AdGuard is down. The `media_player.turn_off` call is also wrapped in a broad `except`.

## 6. Usage attribution

The coordinator subscribes to state-change events on `media_player.<apple_tv>`. On every event (and every 30 s tick) it calls `_sync_open_event` with the current `(media_state, app_id)`:

### Effective bundle id

```
if media_state in {"off", "standby", "unavailable", "unknown", None}:
    effective = None              # device off → close open event

if media_state in {"playing", "paused", "buffering", "on", "idle"}:
    if app_id is set:
        effective = app_id
        remember (app_id, now) as last_known
    elif last_known is recent (within idle_grace_minutes):
        effective = last_known     # pyatv hides bundle id during pause
    else:
        effective = "unknown"      # device on but no idea what app
```

### Sync rules

| Current open event | New effective | Action |
|---|---|---|
| None | None | Nothing. |
| None | bundle X | Open event with bundle X. |
| Open with bundle X | None | Close. |
| Open with bundle X | bundle X | Nothing (same). |
| Open with bundle X | bundle Y | Close X, open Y. |

### Aggregation

`used_seconds_today(profile, now)` sums `UsageEvent.duration_seconds` clipped to the local-today window:

```
start_of_day_local = floor(now in local tz, "day")
start_of_day_utc   = start_of_day_local.as_utc()

for each UsageEvent of profile:
    overlap = max(0, min(event.ended_at or now, now) - max(event.started_at, start_of_day_utc))
    total  += overlap.seconds
```

Events spanning midnight are correctly clipped — kids playing at 23:59 → 00:01 contribute 1 min to "yesterday" and 1 min to "today".

## 7. AdGuard contract

The integration only ever calls two AdGuard endpoints, via the [proxy addon](https://github.com/jarvis2k1/ha-appletv-mgmt-adguard-proxy):

### `GET /control/clients`

Read the existing persistent clients. We pick the one whose `name` matches `profile.adguard_client_name`. The full client object is preserved on update so we don't accidentally clear `ids`, `tags`, etc.

### `POST /control/clients/update`

```json
{
  "name": "Living Room Apple TV",
  "data": {
    "name": "Living Room Apple TV",
    "ids": ["192.168.1.50", "aa:bb:cc:dd:ee:ff"],
    "use_global_blocked_services": false,
    "blocked_services": ["*"],
    "...": "..."
  }
}
```

**Known quirk:** `name` must appear both at the envelope level AND inside the `data` object. Our `AdGuardClient` codifies this.

- **Block:** `blocked_services: ["*"]` + `use_global_blocked_services: false`.
- **Unblock:** `blocked_services: []` + `use_global_blocked_services: false`.

`"*"` is AdGuard's wildcard for all known services. The Apple TV gets `NXDOMAIN` for everything → app shows "no connection".

### Auth modes (in `AdGuardClient.__init__`)

| Modes set | Header sent |
|---|---|
| `api_key="..."` | `X-API-Key: <key>` (proxy mode — recommended) |
| `username + password` | `Authorization: Basic <b64>` (direct AdGuard with HTTP auth) |
| Neither | No auth header (direct AdGuard without HTTP auth) |

If both `api_key` and `username/password` are provided, `api_key` wins.

## 8. Services (HA service registry)

All services take a `profile_id` matching the config entry ID.

| Service | Args | Effect |
|---|---|---|
| `appletv_mgmt.force_block` | `profile_id` | Skip the state machine, immediately call AdGuard block + Apple TV sleep, set state to `ENFORCING`. |
| `appletv_mgmt.grant_extension` | `profile_id`, `minutes` (int, −240..240) | Add minutes to today's pool. If state was `ENFORCING` and remaining now > 0, the next tick (≤ 30 s) drops back to `OK` and unblocks. |
| `appletv_mgmt.reset_usage` | `profile_id` | Clear extension pool, close any open event, unblock unconditionally. Today's previously-logged usage is **not** deleted (only the extension counter). |

## 9. Sensors (entities)

All sensors have `unique_id` of the form `<profile_id>_<sensor_key>`.

| `entity_id` (default) | Unit | Source |
|---|---|---|
| `sensor.<profile>_time_used_today` | min | Rounded `used_seconds_today / 60`. |
| `sensor.<profile>_time_remaining_today` | min | Rounded `max(0, budget - used) / 60` where budget includes extensions. |
| `sensor.<profile>_current_app` | — | Current `bundle_id` of the open event, or `"none"`. |
| `sensor.<profile>_enforcement_state` | — | `ok` / `warning` / `grace` / `enforcing`. |
| `sensor.<profile>_extension_minutes_today` | min | Total minutes granted today. |

## 10. Events on the HA bus

| Event | Payload | Fires when |
|---|---|---|
| `appletv_mgmt_usage_updated` | `{profile_id, used_seconds_today, remaining_seconds_today, budget_seconds_today, extension_minutes_today, current_bundle_id, enforcement_state, is_blocked}` | Every coordinator tick (~30 s). |
| `appletv_mgmt_enforcement_changed` | `{profile_id, state, is_blocked}` | Whenever the state machine transitions. |
| `appletv_mgmt_app_started` *(0.3.0)* | `{profile_id, bundle_id, display_name, started_at}` | When a new app starts being attributed. |
| `appletv_mgmt_app_ended` *(0.3.0)* | `{profile_id, bundle_id, display_name, started_at, ended_at, duration_seconds, duration_minutes}` | When an app stops (user switched OR device off). |
| `logbook_entry` *(0.3.0)* | Standard HA logbook payload | Alongside each `app_started`/`app_ended` so they show in the Logbook panel. |

## 11. Configuration flow

Single-step form. Validates that AdGuard (or the proxy) responds and the named client exists before saving.

| Field | Required? | Notes |
|---|---|---|
| `profile_name` | yes | Becomes the integration entry's title. |
| `apple_tv_entity_id` | yes | Must be a `media_player.*` entity, ideally from the built-in `apple_tv` platform. |
| `adguard_url` | yes | Either `http://homeassistant.local:8101` (proxy, recommended) or `http://<host>:3000` (direct). |
| `adguard_api_key` | optional | Proxy `X-API-Key`. |
| `adguard_username` | optional | Direct AdGuard Basic auth. |
| `adguard_password` | optional | Direct AdGuard Basic auth. |
| `adguard_client_name` | yes | Case-sensitive, must already exist as a Persistent Client in AdGuard. |
| `daily_budget_min` | yes | 1–1440 minutes. |

**Options flow** lets you adjust `daily_budget_min`, `grace_seconds`, `idle_grace_minutes`, `tv_entity_id` *(0.4.0)*, and `tv_shutdown_enabled` *(0.4.0)* later without re-installing. The TV-shutdown toggle is also a `switch` entity for quick toggling from dashboards.

## 12. Failure modes & guardrails

| Scenario | Behavior |
|---|---|
| AdGuard returns 5xx or times out | Log error, leave state machine in current state. Next tick retries. |
| `media_player.turn_off` raises | Log error, continue. State machine still transitions; the device just doesn't sleep. |
| pyatv reports no bundle id during pause | Fall back to last known for `idle_grace_minutes`. After that, attribute to `"unknown"`. |
| Device unplugged mid-event | Open event stays open until either device comes back OR midnight pruning runs (≥ 30 days later). Aggregate `used_seconds_today` keeps growing — by design; we don't want kids to "save" time by yanking the cable. |
| HA restart with an open event | On `async_unload_entry` we close the event with `now`. Aggregation is preserved. |
| Local-midnight rollover | The 1-minute timer detects `local.hour == 0 and local.minute == 0` and clears `extensions_granted_today`. The aggregator clips by local midnight on each tick, so usage counters effectively reset without any explicit "reset" step. |
| User edits a Profile field via options flow | Config entry reloads → coordinator is recreated. Open event is closed first; usage history is preserved. |

## 13. Security model

| Surface | Risk | Mitigation |
|---|---|---|
| AdGuard write access | A compromised integration could lock the LAN out of DNS | Limited to the one configured `adguard_client_name`. The proxy doesn't expose other AdGuard endpoints (only `/control/*` matching the configured prefix). |
| Proxy on the LAN | Anyone on the LAN can hit `:8101` | `X-API-Key` header required if the addon option `api_key` is set. Recommend a 24+ byte random key. |
| HA service calls | A compromised HA scripting layer could `force_block` | Same trust boundary as any other HA service. Out of scope here. |
| pyatv credentials | If the Apple TV pairing leaks, an attacker can sleep the device | Pairing lives in the built-in Apple TV integration's storage — not duplicated here. |

The proxy is intentionally a **transport**, not a gatekeeper. The integration knows what AdGuard operations it's allowed to perform. The proxy doesn't try to filter requests.

## 14. Roadmap

| Phase | Status | Scope |
|---|---|---|
| 1 — Monitoring + single overall daily budget + AdGuard enforcement | ✅ shipped (v0.2.0) | Done. |
| 1.5 — Today's history sensor + Lovelace timeline card + Logbook integration | ✅ shipped (v0.3.x) | Done. |
| 1.6 — Optional TV hard-shutdown + transient-disconnect stitching | ✅ shipped (v0.3.2 + v0.4.0) | Done. |
| 2 — Quiet windows (time-of-day enforcement) | ✅ shipped (v0.5.0) | Done. |
| 2.5 — App groups + per-group budgets + iTunes auto-categorize + adult-mode override | ✅ shipped (v0.6.0) | Done. |
| 3 — REST API + OpenAPI + extension-request flow with Companion approval | 🚧 in progress (v0.7.0) | `HomeAssistantView` subclasses at `/api/appletv_mgmt/*`, Bearer-token auth, actionable Companion notifications. OpenClaw on the Mac mini becomes the kid-facing voice surface. |
| 4 — Configuration UI as a Lovelace dashboard view | planned | Replace the options-flow modal as the daily-driver surface. Per-group budget edits, adult-mode toggle with countdown, quiet-window editing, request-approval inline. Single-file vanilla Lit; no build step. |
| 5 — HA Assist voice intent integration (parent override by voice) | external | the owner's Mac mini AI agent will hit the Phase-3 REST API directly. No work needed inside this integration. |
| 6 — HACS public listing + curated mapping refresh | future | On-TV warning overlays via `play_media`; HACS submission. |

## 15. Versioning

We follow [SemVer](https://semver.org/) with the integration's `manifest.json` `version` as the source of truth.

- **MAJOR**: breaking change to config entry schema, state machine semantics, or service signatures.
- **MINOR**: new capability (per-app limits, new service, new entity).
- **PATCH**: bug fix or doc fix with no behavior change.

The proxy addon is versioned independently; its tags live in [jarvis2k1/ha-appletv-mgmt-adguard-proxy](https://github.com/jarvis2k1/ha-appletv-mgmt-adguard-proxy).
