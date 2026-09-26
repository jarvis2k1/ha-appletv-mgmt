# API — Apple TV Mgmt

This is the integration's contract with the outside world. Phase 1 surfaces are **HA services**, **entities**, and **events** (over the HA bus). The **REST API** is Phase 3 — its planned shape is documented here so consumers can be built against the contract before the implementation lands.

For the underlying data model and state machine see [SPEC.md](../SPEC.md). For the proxy addon's API see [its docs](https://github.com/jarvis2k1/ha-appletv-mgmt-adguard-proxy/blob/main/docs/API.md).

---

## Versioning

This API doc tracks the integration's `manifest.json` version. The shape rules:

- **Adding** an entity, a service, an event field, or an optional config key is a **MINOR** bump.
- **Renaming or removing** any service, entity unique_id, event name, or required field is a **MAJOR** bump and gets a migration note in [CHANGELOG.md](../CHANGELOG.md).
- Service argument validation is enforced by voluptuous; out-of-range / wrong-type values are rejected before they reach the integration code.

Current version: **0.7.0** — adds the full REST API + extension-request flow (Phase 3). The Phase-3 REST surface in [section 5](#5-rest-api-phase-3) is now LIVE; OpenAPI 3.1 spec served at `/api/appletv_mgmt/openapi.json`.

---

## 1. Services

All services are registered under the `appletv_mgmt` domain and callable from automations, scripts, the Developer Tools UI, or `POST /api/services/appletv_mgmt/<name>` via HA's HTTP API.

`profile_id` always equals the HA config entry ID — you can read it from the Devices & Services UI (URL fragment after editing an entry) or programmatically:

```python
{{ states.sensor | selectattr('entity_id', 'match', 'sensor\\..*_enforcement_state$')
   | map(attribute='entity_id')
   | list }}
```

(Entity unique_ids embed the profile id as the prefix.)

### `appletv_mgmt.force_block`

Immediately enforce — call AdGuard block + Apple TV sleep, set state to `ENFORCING`. Bypasses the state machine.

```yaml
service: appletv_mgmt.force_block
data:
  profile_id: 01HXYZ...  # the config entry ID
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `profile_id` | string | yes | Config entry ID. |

Returns: nothing. Emits `appletv_mgmt_enforcement_changed`.

### `appletv_mgmt.grant_extension`

Add (or subtract) minutes from today's effective budget. If the integration is currently `ENFORCING` and the new remaining is positive, the next tick drops to `OK` and unblocks within ~30 seconds (or call `homeassistant.update_entity` on a sensor to force an immediate refresh).

```yaml
service: appletv_mgmt.grant_extension
data:
  profile_id: 01HXYZ...
  minutes: 15
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `profile_id` | string | yes | Config entry ID. |
| `minutes` | integer | yes | −240..240. Negative subtracts. |

The extension counter resets at local midnight. Extensions added late in the day don't roll over.

### `appletv_mgmt.reset_usage`

Clear today's extension pool, close any open usage event, unblock AdGuard unconditionally, reset state to `OK`. **Does not delete** existing `UsageEvent` rows — `time_used_today` is still computed correctly from the open-event chain after a reset.

```yaml
service: appletv_mgmt.reset_usage
data:
  profile_id: 01HXYZ...
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `profile_id` | string | yes | Config entry ID. |

Use case: end-of-day manual override, or a panic-button when the integration glitched.

---

## 2. Entities

All entities are `SensorEntity` subclasses backed by the coordinator. They have `_attr_has_entity_name = True` so HA derives nice display names; entity IDs are slugified from those.

| Sensor key | Default entity ID | Unit | Device class | Value source |
|---|---|---|---|---|
| `time_used_today` | `sensor.<profile>_time_used_today` | minutes | `duration` | `round(used_seconds / 60, 1)` |
| `time_remaining_today` | `sensor.<profile>_time_remaining_today` | minutes | `duration` | `round(max(0, budget − used) / 60, 1)` (budget includes extensions) |
| `current_app` | `sensor.<profile>_current_app` | — | — | Open event's `bundle_id`, or `"none"`. Special value `"unknown"` when the device is on but pyatv reports no app and the idle-grace has elapsed. |
| `enforcement_state` | `sensor.<profile>_enforcement_state` | — | — | One of `ok`, `warning`, `grace`, `enforcing`. *(0.5.0)* Attribute `active_quiet_window` carries the label of the quiet window currently in effect (or `None`). |
| `extension_minutes_today` | `sensor.<profile>_extension_minutes_today` | minutes | `duration` | Sum of `grant_extension` calls today. Negative-only adjustments cap at 0. |
| `today_history` *(0.3.0)* | `sensor.<profile>_todays_app_usage` | — | — | State = number of distinct apps used today. Carries the rich per-app + per-event log as attributes — see below. |
| `<group>_time_used_today` *(0.6.0)* | `sensor.<profile>_<group>_time_used_today` | minutes | `duration` | Per-group used minutes today. **Disabled by default** — enable per group from the entity registry. Groups: `movies`, `tv_shows`, `gaming`, `other`. |
| `<group>_time_remaining_today` *(0.6.0)* | `sensor.<profile>_<group>_time_remaining_today` | minutes | `duration` | Per-group remaining minutes today. `None` when no budget is set for that group. Same disabled-by-default semantics. |

#### Switches

| Switch key | Default entity ID | When on | When off |
|---|---|---|---|
| `tv_shutdown` *(0.4.0)* | `switch.<profile>_shut_down_tv_on_enforcement` | When the integration enters `ENFORCING`, it additionally calls `media_player.turn_off(profile.tv_entity_id)` after the AdGuard block + Apple TV sleep. | No-op — only the AdGuard block + Apple TV sleep fire. |
| `adult_mode` *(0.6.0)* | `switch.<profile>_adult_mode` | ALL enforcement (budgets + groups + quiet windows) is bypassed for `profile.adult_mode_duration_min` minutes. Auto-turns-off when the timer expires; persists across HA restarts. Usage is still recorded. | Normal enforcement applies. |

Switch availability: `tv_shutdown` is `unavailable` until a `tv_entity_id` is configured. `adult_mode` is always available. Both carry diagnostic attributes (`tv_entity_id` / `until` + `duration_minutes` respectively).

### `today_history` attributes (0.3.0)

The `today_history` sensor is the main feed for any custom UI:

```json
{
  "apps": [
    {"bundle_id": "com.google.ios.youtube", "display_name": "YouTube", "total_minutes": 47.3, "sessions": 3},
    {"bundle_id": "com.netflix.Netflix",   "display_name": "Netflix", "total_minutes": 22.1, "sessions": 1}
  ],
  "events": [
    {
      "bundle_id": "com.google.ios.youtube",
      "display_name": "YouTube",
      "started_at": "2026-05-17T17:12:03+00:00",
      "ended_at":   "2026-05-17T17:34:17+00:00",
      "duration_minutes": 22.2,
      "open": false
    },
    {
      "bundle_id": "com.netflix.Netflix",
      "display_name": "Netflix",
      "started_at": "2026-05-17T17:34:17+00:00",
      "ended_at":   null,
      "duration_minutes": 8.7,
      "open": true
    }
  ]
}
```

- **`apps`** is sorted by `total_minutes` descending.
- **`events`** is sorted by `started_at` ascending and clipped to today's local-time window — events that started yesterday have their `started_at` clipped to local midnight.
- An open event (whatever app is playing right now) has `ended_at: null` and `open: true`. Its `duration_minutes` is computed against `now`.

### Device

All sensors of one profile group under a virtual HA device with identifiers `{(appletv_mgmt, profile_id)}` and model `"Profile"`. You can rename / regroup like any HA device.

---

## 3. Events on the HA bus

Consume from automations via the `event` trigger, or from Python via `bus.async_listen`.

### `appletv_mgmt_usage_updated`

Fired on **every** coordinator tick (~30 s). Use this for live dashboards or sub-minute alerting.

```json
{
  "profile_id": "01HXYZ...",
  "used_seconds_today": 3325,
  "remaining_seconds_today": 275,
  "budget_seconds_today": 3600,
  "extension_minutes_today": 0,
  "current_bundle_id": "com.google.ios.youtube",
  "enforcement_state": "warning",
  "is_blocked": false
}
```

### `appletv_mgmt_enforcement_changed`

Fired **only when** the state machine transitions (not every tick). Use this for "block lifted" / "block fell" notifications.

```json
{
  "profile_id": "01HXYZ...",
  "state": "enforcing",
  "is_blocked": true
}
```

### `appletv_mgmt_app_started` *(0.3.0)*

Fired when a new app starts being attributed time.

```json
{
  "profile_id": "01HXYZ...",
  "bundle_id": "com.google.ios.youtube",
  "display_name": "YouTube",
  "started_at": "2026-05-17T17:12:03+00:00"
}
```

### `appletv_mgmt_app_ended` *(0.3.0)*

Fired when an app stops (because the user switched apps or turned off the device).

```json
{
  "profile_id": "01HXYZ...",
  "bundle_id": "com.google.ios.youtube",
  "display_name": "YouTube",
  "started_at": "2026-05-17T17:12:03+00:00",
  "ended_at":   "2026-05-17T17:34:17+00:00",
  "duration_seconds": 1334,
  "duration_minutes": 22.2
}
```

The integration also fires `logbook_entry` events for these so the built-in **Settings → Logbook** panel shows them with friendly messages ("started YouTube" / "finished YouTube after 22.2 min"). Use the `appletv_mgmt_app_*` events (not `logbook_entry`) for automations — the logbook event shape is owned by HA and may change.

Example HA automation:

```yaml
- alias: Notify me when screen time is blocked
  trigger:
    - platform: event
      event_type: appletv_mgmt_enforcement_changed
      event_data:
        state: enforcing
  action:
    - service: notify.mobile_app_your_phone
      data:
        title: "Daily screen-time limit reached"
        message: "Last app: {{ states('sensor.living_room_current_app') }}"
```

---

## 4. Config entry schema

The integration's persistent config entry (`.storage/core.config_entries`) has this `data` shape, set by the UI config flow and editable via the options flow for a subset of fields:

```json
{
  "profile_name": "Living Room",
  "apple_tv_entity_id": "media_player.living_room",
  "adguard_url": "http://homeassistant.local:8101",
  "adguard_api_key": "<your-adguard-api-key>",
  "adguard_username": "",
  "adguard_password": "",
  "adguard_client_name": "Living Room Apple TV",
  "daily_budget_min": 60
}
```

Options-flow-editable subset (lives under `entry.options`, overrides `entry.data`):

```json
{
  "daily_budget_min": 60,
  "grace_seconds": 60,
  "idle_grace_minutes": 5,
  "tv_entity_id": "media_player.samsung_tv",
  "tv_shutdown_enabled": false,
  "quiet_windows": "20:30-07:00:Bedtime, 12:00-14:00:Lunch",
  "group_budgets": {"movies": 60, "tv_shows": 60, "gaming": 30, "other": 60},
  "adult_mode_duration_min": 120
}
```

- `tv_entity_id` + `tv_shutdown_enabled` (0.4.0) — `tv_shutdown_enabled` is also writable from the `switch.<profile>_shut_down_tv_on_enforcement` entity (changes persist into `entry.options`).
- `quiet_windows` (0.5.0) — comma-separated `HH:MM-HH:MM[:Label]` list of local-time enforcement windows. Each window can cross midnight by setting `start > end`. Empty string = no windows. Format validated by the options-flow on save; bad input is rejected with `invalid_quiet_windows`.
- `group_budgets` (0.6.0) — daily minutes budget per app group. Groups: `movies`, `tv_shows`, `gaming`, `other`. `0` or missing = unlimited.
- `adult_mode_duration_min` (0.6.0) — duration in minutes that the `switch.<profile>_adult_mode` override is active when flipped on. Default 120.

Also new in 0.6.0: the integration's own storage (`.storage/appletv_mgmt_data`) gains an `app_categories` field — a `{bundle_id: group}` cache of past Apple iTunes Search API lookups, populated lazily the first time the kids open an app the integration hasn't seen.

The integration's `unique_id` is the `apple_tv_entity_id`, so adding a second entry for the same Apple TV is rejected with `already_configured`.

---

## 5. REST API *(Phase 3 — shipped in v0.7.0)*

The integration will register `HomeAssistantView` subclasses to expose a small REST surface for external consumers (OpenClaw on a Mac mini being the driving use case). Auth via a dedicated `X-API-Key` header configured separately from HA's long-lived tokens.

### Base path

```
http://homeassistant.local:8123/api/appletv_mgmt
```

### Auth

Two layers, either is sufficient:

- **HA's standard bearer token** (long-lived access token from your HA user profile) — works automatically because the API lives under `/api/*`.
- **Integration-specific API key** — set in the options flow under `api_key`. Sent as `Authorization: Bearer <api_key>` OR `X-API-Key: <api_key>`.

If `api_key` is empty in options AND the request has no HA token, the request is allowed through (open on the LAN — fine when HA is firewalled, not recommended otherwise). Confirm by hitting `/health` and reading `auth_required`.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness, version, `auth_required` flag, current profile count. Public — no auth. |
| `GET` | `/openapi.json` | Full OpenAPI 3.1 spec. Public — no auth. |
| `GET` | `/profiles` | List configured profiles + a status summary for each. |
| `GET` | `/profiles/{id}/status` | Single-profile status snapshot. |
| `GET` | `/profiles/{id}/groups` | Per-group `{used_today_min, budget_today_min, remaining_today_min}`. |
| `GET` | `/profiles/{id}/usage` | Per-app minutes used today, sorted by usage. |
| `GET` | `/profiles/{id}/events` | Chronological today's `UsageEvent` log. |
| `POST` | `/profiles/{id}/adult_mode` | Enable adult mode. Body: `{"minutes": int?}` (default: profile setting). |
| `DELETE` | `/profiles/{id}/adult_mode` | Disable adult mode immediately. |
| `POST` | `/profiles/{id}/extension` | Direct minutes grant. Body: `{"minutes": -240..240}`. No approval needed (already authenticated). |
| `POST` | `/profiles/{id}/request_extension` | Kid-facing. Body: `{"minutes": 1..240, "reason": str?, "bundle_id": str?}`. Creates pending request + fires Companion push. Returns 201 with the `ExtensionRequest`. |
| `GET` | `/profiles/{id}/requests?status=pending\|approved\|denied\|expired` | List recent requests for a profile. |
| `GET` | `/requests/{request_id}` | Poll one request. |
| `POST` | `/requests/{request_id}/decide` | Decide a request via API (vs Companion notification). Body: `{"approve": bool, "minutes": int?}`. |

### Example responses

#### `GET /profiles/{profile_id}/status`

```json
{
  "profile_id": "01HXYZ...",
  "display_name": "Living Room",
  "state": "warning",
  "current_app": "com.google.ios.youtube",
  "current_app_display": "YouTube",
  "used_today_min": 55,
  "remaining_today_min": 5,
  "budget_today_min": 60,
  "extension_minutes_today": 0,
  "bedtime_until": null
}
```

#### `POST /profiles/{profile_id}/request_extension`

Request body:

```json
{
  "minutes": 15,
  "scope": "overall",
  "reason": "want to finish this episode"
}
```

Response:

```json
{
  "request_id": "01HABC...",
  "status": "pending",
  "auto_expires_at": "2026-05-17T20:42:00Z"
}
```

Triggers an actionable HA Companion notification on the parent's phone with `Approve` / `Approve 30` / `Deny` buttons.

#### `GET /requests/{request_id}`

```json
{
  "request_id": "01HABC...",
  "profile_id": "01HXYZ...",
  "requested_minutes": 15,
  "granted_minutes": 30,
  "scope": "overall",
  "reason": "want to finish this episode",
  "status": "approved",
  "requested_at": "2026-05-17T20:32:00Z",
  "decided_at": "2026-05-17T20:33:14Z",
  "decided_by": "marc"
}
```

### Error envelope

```json
{
  "error": "client_not_found",
  "message": "No profile with id 01HXYZ..."
}
```

| `error` code | HTTP | Meaning |
|---|---|---|
| `unauthorized` | 401 | Missing or wrong API key. |
| `client_not_found` | 404 | Unknown `profile_id` or `request_id`. |
| `invalid_payload` | 422 | Body failed schema validation. |
| `rate_limited` | 429 | Too many extension requests in a short window (planned). |
| `upstream_unavailable` | 503 | AdGuard/proxy unreachable; the integration is degraded. |

### OpenAPI spec

Served at `/api/appletv_mgmt/openapi.json` when Phase 3 ships. Designed for autonomous-agent consumption (OpenClaw can introspect at startup and self-generate a calling skill).

---

## 6. Proxy contract *(separate addon)*

The integration talks to AdGuard through the proxy addon. The proxy's surface:

```
GET  /health
GET  /control/*       → forwarded to discovered AdGuard
POST /control/*       → forwarded to discovered AdGuard
PUT  /control/*       → forwarded to discovered AdGuard
DELETE /control/*     → forwarded to discovered AdGuard
PATCH  /control/*     → forwarded to discovered AdGuard
```

All forwarded requests require `X-API-Key: <proxy api_key>` if the addon option `api_key` is set. The proxy strips and re-injects its own auth — it does not pass the integration's API key onward to AdGuard.

Full details in the [proxy API doc](https://github.com/jarvis2k1/ha-appletv-mgmt-adguard-proxy/blob/main/docs/API.md).

---

## 7. Stability promises

| Surface | Stability |
|---|---|
| Service names (`force_block`, `grant_extension`, `reset_usage`) | Stable — only ever added, never removed in a minor. |
| Service argument names | Stable. New optional args allowed in a minor; renames are major. |
| Sensor unique_ids (`<profile_id>_<key>`) | Stable. Entity *IDs* are slugified and can be renamed by the user — automations should reference `unique_id` via templating where possible. |
| Bus event names | Stable. New keys may be added to payloads in a minor; removals are major. |
| `manifest.json` `version` | Bumped per [SemVer](https://semver.org/) — see "Versioning" above. |
| REST endpoints (Phase 3) | API-versioned via `Accept: application/vnd.appletv_mgmt+json; v=1`. Breaking changes get a new `v=`. |

If you build automations against the events / sensors / services in this doc, this integration will not surprise you across patch and minor releases.
