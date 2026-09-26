# Changelog

All notable changes to the **Apple TV Mgmt** integration. Format roughly follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

The companion proxy addon is versioned independently — see [its changelog](https://github.com/jarvis2k1/ha-appletv-mgmt-adguard-proxy/blob/main/CHANGELOG.md).

---

## [Unreleased]

### Fixed (v0.23.2 — `day_rollover_hour` silently reverted on every HA restart)

**The 05:00 rollover never survived a restart.** Set to `5` on 2026-09-02, found
back at `0` on 2026-09-12 — with **no audit-log entry**, because nothing had
written it.

`async_setup_entry` rebuilds the Profile from `entry.data` / `entry.options` on
every setup, then overlays a hand-maintained list of PATCH-able fields from the
store so they survive a restart. v0.23.0 added `day_rollover_hour` to the
PATCH-able set in `api.py` — and to the JSON schema, and to the coercion
dispatch — but **not to that overlay list**. So a parent could set it, watch it
take effect, and have it silently return to midnight accounting at the next
restart.

This is a verbatim repeat of the v0.15.9 bug documented three comments above the
list itself, where `daily_budget_min` reverted the same way and a child heard
"Achtung, noch 60 Minuten" after the owner had set 2.

Two tests in `tests/test_restart_survival.py`, both confirmed failing before the
fix:

- `day_rollover_hour` must be in the overlay list.
- A **canary** that diffs the PATCH-able set in `api.py` against the overlay list
  in `__init__.py`, with an explicit `KNOWN_NOT_OVERLAID` allowlist. A newly
  PATCH-able field that forgets the list now fails CI, instead of surfacing weeks
  later as a parent's setting mysteriously reverting.

The canary also documents what is still not overlaid — `weekday_budgets_min`,
`weekday_group_budgets_min`, `weekday_quiet_windows`, `stale_session_minutes`,
`warn_in_monitor_mode`, `voice_on_mode_change`, `notify_volume`,
`notify_tts_language`. These are the next candidates if a parent reports one
reverting; they are left alone here rather than changed unasked.


### Fixed (v0.23.1 — the day-rollover helper was unreachable from one sensor)

**`sensor.<profile>_today_s_app_usage` was dead on every v0.23.0 install.**
Found live 2026-09-12 sitting at `unavailable`.

`AppleTVTodayHistorySensor` calls `self._rollover_hour()` in both
`native_value` and `extra_state_attributes`, but v0.23.0 defined that helper
only on `AppleTVMgmtSensor` — a sibling class it does not inherit from. Every
state read raised `AttributeError: 'AppleTVTodayHistorySensor' object has no
attribute '_rollover_hour'`, and Home Assistant surfaces that as `unavailable`
rather than as an error, so it failed silently.

The helper now lives on a `_ProfileRolloverMixin` shared by all three
day-scoped sensor classes, instead of being copied into the one that broke.

**Also fixed, the quiet half of the same mistake:** `api.py`'s
`/profiles/{id}/usage` endpoint called `store.app_totals_today()` **without**
`rollover_hour=`. Because that argument defaults to `0`, nothing raised — the
endpoint just reported a midnight-to-midnight day while every other
today-scoped reader used the profile's rollover hour. A household with a 05:00
rollover got two different answers depending on which one it asked.

Four regression tests in `tests/test_sensor_rollover_helper.py`. Two reproduce
the `AttributeError` directly; the other two are static and guard the *class*
of bug — every sensor class that reaches for `_rollover_hour` must be able to
resolve it, and no call to a rollover-aware store method may omit the hour.
The 804-test suite missed this because nothing ever read that entity.


### Added (v0.23.0 — configurable day rollover; the household day does not end at midnight)

Live-reported 2026-08-30: an adult watching until ~02:00 had that time charged
to the **next** calendar day, so the children's budget was already partly spent
before they woke up. Enforcement then fired against the wrong day entirely.

New per-profile `day_rollover_hour` (0-23, PATCHable via
`/profiles/{id}/limits`). With `5`, the usage day runs 05:00 → 04:59, so a late
film is billed to the evening it began.

**Default stays `0` (midnight)** — existing installs must not silently
re-account their history on upgrade.

Two pure helpers in `schedule.py` — `day_start()` and `logical_date()` — are
now used by every day-boundary calculation. Seven sites previously computed
midnight independently:

- `storage.used_seconds_today` / `events_today` / `app_totals_today` /
  `group_totals_today` — the usage windows.
- `coordinator._handle_midnight_check` — the daily reset now fires at the
  rollover hour, so a session running past midnight is not handed a fresh
  budget mid-view.
- `coordinator._compute_gap_minutes_today` — the DNS-gap window.
- **`coordinator`'s weekday resolution** — the subtle one. Keyed on the
  calendar date, 02:00 on a Saturday applied *Saturday's* budget and quiet
  windows to Friday-night viewing, bringing weekend rules five hours early.
  It now keys on the logical date.

Boundaries are computed in local time, so the rollover tracks the wall clock
across DST rather than drifting with UTC.

### Security — BREAKING (v0.22.0 — the REST API now requires Home Assistant auth)

Raised by [@frenck](https://github.com/frenck) reviewing the HACS submission
([hacs/default#9134](https://github.com/hacs/default/pull/9134), 2026-08-28).

`_Base` set `requires_auth = False`, so **every** endpoint bypassed Home
Assistant's bearer check. The custom `api_key` gate meant to compensate opened
with `if not key: return None` — a no-op — and `CONF_API_KEY` was offered only
in the *options* flow, never at initial setup. A freshly installed entry
therefore had no key and no protection on the endpoints that exist to enforce
limits: `PATCH /profiles/{id}/limits`, `POST`/`DELETE /adult_mode`,
`POST /extension` and `POST /requests/{id}/decide`.

The practical consequence: a parent installs this to cap their child's screen
time, and the child on the same wifi can lift the limit with one
unauthenticated request — no credential, no notification. Where the instance
is reachable through Nabu Casa, a reverse proxy or a forwarded port, that
reaches past the LAN. `/health` and `/openapi.json` skipped the check
unconditionally *even when a key was set*, publishing the version, profile
count and full endpoint schema to anonymous callers.

- **Every endpoint now requires HA's own authentication.** `requires_auth`
  is no longer overridden, so all views inherit `HomeAssistantView`'s default.
  `/health` and `/openapi.json` are included.
- **The integration-specific `api_key` is gone** — `_check_api_key()`,
  `_resolved_api_key()` and `CONF_API_KEY` are removed, along with the options
  -flow field and its translations. A second home-grown scheme added no
  security and disguised the fact that a default install had none.
- `/health` reports `"auth_required": true` unconditionally.

**Upgrading:** callers now send `Authorization: Bearer <HA long-lived access
token>` (Profile → Security → Long-lived access tokens). Add-ons with
`homeassistant_api: true` — including the companion panel from v0.11.0 — are
handled by the Supervisor proxy and need no change. Any script or shortcut
that called this API unauthenticated, or with the old integration key, must be
updated.

Guarded by source-level tests (`tests/test_api_auth.py`) that assert no view
reintroduces `requires_auth = False` — a property no behavioural test would
reliably catch.

### Changed (v0.22.0)

- `hacs.json` raises the Home Assistant floor to **2026.3.0**. The release
  ships `brand/icon.png`, which needs 2026.3; users on 2025.x installed
  cleanly, got no icon and had nothing telling them why.
- `manifest.json` drops `"requirements": ["aiohttp>=3.8.0"]` — aiohttp ships
  with Home Assistant core.
- README's sample `/health` response no longer shows a v0.7.0 payload.

### Fixed (v0.21.3 — Live TV escaped its own cap behind a sibling group's reason)

Live-reported 2026-08-17: Live TV ran **158 min against a 30 min cap** and was
never powered off. The room was correctly `state=enforcing` / `is_blocked=true`,
yet `enforcement_failed` stayed `false` — the TV was being *deliberately spared*,
silently.

Two independently-reasonable pieces combined into a hole:

1. **The binding-reason picker broke on an arbitrary group.** The
   group-exhaustion scan iterated `group_totals_seconds` (an unordered dict) and
   `break`-ed on the *first* exhausted group. With `linear_tv` (158/30) **and**
   `other` (204/60) both spent, it pinned `group:other` while the kid was sitting
   in `linear_tv`. Order-dependent, hence intermittent.
2. **The native-TV gate trusted that reason string.** `_native_tv_should_enforce()`
   read `group:other`, concluded "an unrelated category ran out — don't black out
   Live TV" (the v0.21.1 rule that stops a spent *movies* budget from cutting off
   a parent watching the tuner), and suppressed the kill. Because sparing counts
   as a successful enforcement, nothing was logged and no alert fired.

Fixes:

- The exhaustion scan now evaluates the **current group first**, so watching an
  exhausted Live TV reports `group:linear_tv` rather than a sibling.
- The gate no longer reads the reason string as authoritative. It checks whether
  `linear_tv` **actually has budget left** (`_native_group_exhausted`, snapshotted
  each evaluation). Sibling exhaustion spares the TV only while Live TV is
  genuinely in credit; an exhausted own cap — or any room-wide limit — enforces.

Note this also unblocked the `force_block` service, which had inherited the same
hole: it keeps any existing `_enforce_reason`, so a stale `group:other` used to
gate it out on a native source.

Regression tests reproduce the live totals and fail on the pre-fix code with the
exact observed symptom (`got 'group:other'`).

### Added (v0.21.2 — in-integration brand icon)

- **Ships its own brand icon** in `custom_components/appletv_mgmt/brand/` (`icon.png` 256×256, `icon@2x.png` 512×512). As of Home Assistant 2026.3 custom integrations provide brand images directly rather than via the central `home-assistant/brands` repo (which now auto-declines custom-integration icons); HA serves these through its local brands proxy and they take priority over the CDN. No behavior change.

### Fixed (v0.21.1 — native-TV adversarial-review fixes)

Five defects found by an adversarial review of the v0.21.0 native-TV feature (still ships disabled by default):

1. **Native-TV kill now respects *why* the room is enforcing.** Previously the TV-off (both `_enter_enforcing` and the reassert Job 4 watchdog) fired purely on `_native_tv_active()`, so a **sibling** category running out (e.g. `group:movies`) would power off an *unlimited* Live TV — contradicting "unlimited `linear_tv` only tracks" and able to shut off a parent. A new `_native_tv_should_enforce()` gate only kills the TV for **room-wide** reasons (daily budget / quiet / sleep) or Live TV's **own** cap (`group:linear_tv`); a sibling group's exhaustion never does. The pure `_native_tv_active()` mirror (used by the resolver/attribution) is unchanged.
2. **Source matching is now case- and whitespace-insensitive, and misconfig is visible.** `native_tv_is_active` casefolds + strips both sides, so a TV reporting `hdmi1` / ` HDMI1 ` / CEC-renamed inputs no longer reads the Apple-TV input as *native* during a pyatv-freeze window (which could mis-book or turn the TV off mid-movie). Internal spacing (`HDMI 1` vs `HDMI1`) is deliberately *not* normalized; instead the coordinator logs a **one-time warning** when none of the configured `native_tv_excluded_sources` appear in the TV entity's `source_list` (new pure predicate `excluded_sources_match_source_list`).
3. **Native-TV opt-in is now panel/`PATCH /limits`-authoritative only.** `track_native_tv` + `native_tv_excluded_sources` were in *both* the Options flow and the "stored wins" selective-merge loops, so an Options-UI edit was a silent no-op in both directions — and the *disable* direction was dangerous (UI showed off while the TV still got killed). Both fields are **removed from the Options flow + translations** (same model as `secondary_devices` / `enforcement_switch_entity_id`); storage, PATCH validators, and restart-survival are unchanged. `budget_linear_tv` stays in the Options flow (normal per-group-budget pattern).
4. **Native sessions have a liveness backstop.** Native events were fully exempt from `_check_for_stale_session`; a TV entity stuck at `on` (some Samsungs miss a power-off) would over-count `linear_tv` forever. The TV-state fast path still closes native events, but the existing **runaway ceiling** (`STALE_RUNAWAY_CEILING_S`, 2.5 h) now also force-closes a native event that has been open past the cap — bounded, no new timer.
5. Resolved by fix 3 (the "options-edit ignored" footgun disappears once the field leaves the Options flow).

A **second** adversarial review caught that fixes 1 and 4 were only half-complete; both are now finished:

6. **Fix 1 completion — the tv_shutdown fall-through no longer blacks out Live TV.** When the native gate correctly declines to enforce (sibling-group reason), control fell through to the generic `tv_shutdown` path, whose target resolves to the *same* Samsung TV — so a `group:movies` exhaustion still powered off Live TV whenever `tv_shutdown_target`/`tv_shutdown_enabled` was set (the owner's config). `_enter_enforcing` now suppresses the `tv_shutdown` turn-off while native TV is active; the Apple-TV/AdGuard path (harmless while the Apple TV is idle) still runs, and normal `tv_shutdown` still fires when the room is **not** on native TV.
7. **Fix 4 completion — the runaway close is no longer undone by the 60 s event-stitch.** A native event force-closed by the runaway ceiling was re-opened on the *same* tick by `_sync_open_event`'s stitch (gap ≈ 0, restoring the original `started_at`), so the ceiling was neutralized and the event re-closed+re-stitched (audit + save spam) every 30 s. Native events now **never stitch** — after any close they open a fresh event, bounding each segment to the ceiling. Regression tests now exercise the **full tick** (stale-check → sync), not the stale-check in isolation. Also hardened `native_tv_is_active` against a non-string `source` attribute.

A **third** adversarial review confirmed the above and caught one cosmetic leftover:

8. **The intentional Live-TV spare no longer reads as `enforcing_failed`.** When fix 6 suppresses the `tv_shutdown` turn-off, a native profile with no secondary device and AdGuard off would otherwise leave `_last_enforcement_failed = True` (Apple-TV `turn_off` reports "already off", nothing else acted), rendering a sustained false `enforcing_failed` on the `effective_state` sensor. The intentional spare now counts as a successful no-op in the failure-flag math — no safety or voice impact, purely the sensor/automation signal.

### Added (v0.21.0 — native TV watching / "Live TV" as a tracked activity)

- **Time spent watching the TV *natively* — tuner/broadcast, SCART, or a smart-TV app — is now tracked and enforced as a new `linear_tv` ("Live TV") category** under the same room budget. Native TV = the display is on but the input is NOT one of the tracked devices (Apple TV on HDMI1, Xbox on HDMI2/DVI). Detection lives in the coordinator's room resolver with precedence **apple_tv > secondaries (Xbox) > native TV**: native TV only claims the room when nothing tracked is active AND the TV is `on` with a `source` outside `native_tv_excluded_sources` (a missing source fails closed — no booking).
- **Ships disabled by default** (`track_native_tv = False`) — zero behavior change for existing installs. Two new profile fields, settable via the panel / `PATCH /limits` (see the v0.21.1 fixes — they are intentionally *not* in the Options dialog): `track_native_tv` (bool) and `native_tv_excluded_sources` (list of TV source names owned by a tracked device; default `["HDMI1", "HDMI2/DVI"]`). Uses the same `tv_entity_id` the TV-on accumulator already reads.
- **Enforcement for native TV turns the TV off unconditionally** via `media_player.turn_off` — it is *not* gated on the `tv_shutdown` switch (that switch means "also kill the TV when enforcing the Apple TV"; for native TV the TV *is* the device). AdGuard + Apple-TV-sleep paths are skipped. The periodic reassert job gained a native-TV anti-defeat watchdog: while enforcing, if the TV comes back on with a native source it is turned off again (a return to HDMI1/HDMI2 is left alone).
- **`linear_tv` is a first-class group** picked up by all generic per-group machinery: weekday overrides, per-group extensions (incl. the v0.20.4 most-used fallback), the opt-in per-group sensors (`Live TV time used/remaining today`), and group-budget validation. Its budget defaults to unlimited (opt-in) — set `group_budgets.linear_tv` or the options-flow `budget_linear_tv` field to cap it.

### Changed (v0.20.4 — "+minutes" reliably relieves the nagging category)

- **A direct "+X min" grant (panel buttons / `grant_extension` service) now falls back to today's MOST-USED capped category** when nothing is playing at grant time, instead of landing on the daily pool only. Reported live 2026-07-05: a parent granted +120 min against a 30-min movies cap, but only +60 reached movies — the rest was granted during tvOS-26 freeze gaps (Disney+ momentarily stale/closed), so the active-group auto-tag had nothing to attach to and silently went daily-only, leaving movies capped and still nagging. Resolution order is now: explicit group → binding group → active group → **most-used capped group (new)** → daily-only. The new step is opt-in (`fallback_most_used`) so the request-approval path still defers to the *requested* app's group. 0-budget ("unlimited") groups are never chosen.

### Fixed (v0.20.3 — `/groups` ignored per-group extensions)

- **The `GET /groups` payload now folds in each group's tagged extension**, matching what the enforcer actually applies (`enforcer.eff_group_budgets`). Previously the rows reported the *base* group cap, so a parent's "+60 min" granted against (say) the movies cap lifted the real limit but the panel's category card still showed the base cap maxed-out (`0m left`, red) while the profile sat at **OK** — a confusing contradiction reported live 2026-07-05. Rows now expose `budget_today_min` (effective = base + tagged extension), plus new `base_budget_today_min` and `extension_today_min` fields. `0`-budget ("unlimited") groups stay unlimited. Logic extracted to a pure `_effective_group_rows()` helper with regression tests.

### Fixed (v0.20.2 — HACS / hassfest validation)

- **Config-flow labels no longer embed URLs.** hassfest rejects URLs inside translation strings, and the `adguard_url` field label carried example proxy/direct URLs — failing HACS's hassfest check. The label is now a short `AdGuard URL (optional)` and the guidance moved to a URL-free `data_description` helper (English + German). No behavior change; unblocks HACS validation.

### Changed (v0.20.1 — publish-readiness: portability for other families)

Groundwork for a public release so the integration works for households that aren't the author's. No behavior change for an existing single-profile, TV-equipped install.

- **AdGuard Home is now OPTIONAL in the config flow.** The AdGuard URL + client name were `vol.Required` and a live probe blocked entry creation — so a family without AdGuard couldn't finish setup at all, even though `enable_adguard_block` defaults OFF and the DNS layer is supplementary. Both fields are now optional; validation is skipped when the URL is blank (a client name is required only when a URL is given). Runtime AdGuard calls were already guarded.
- **Shared store singleton per HA boot.** Each config entry created its own `AppleTVMgmtStore` bound to the *same* storage key, so a family with two profiles got last-writer-wins clobbering of usage/extensions/runtime state on save. The store is now created once and shared across entries (sibling `appletv_mgmt_store` key), released when the last entry unloads.
- **Voice language defaults to Home Assistant's configured language** instead of a hardcoded `"de"`. `notify_tts_language` defaults to empty and falls back to `hass.config.language` at speak time; a per-profile override still wins (an existing profile keeps its stored value via the restart-survival merge).
- **No-TV installs no longer under-count.** Without a TV liveness entity, a stale Apple TV session previously closed at `last_updated` on every tvOS-26 push-quiet gap (~5 min), recording 0 min for real playback. It now keeps the session open through push gaps, bounded by the existing 2.5 h runaway ceiling (`decide_stale_action` rule 6).
- **Sensor `samsung_tv_on_minutes_today` → `tv_on_minutes_today`** so the `unique_id` doesn't bake a TV brand into every install. A one-time, idempotent entity-registry migration moves any existing entity to the new id (no-op on fresh installs).
- **Metadata / CI hygiene:** removed the invalid `homeassistant` key from `manifest.json` (the min-HA floor lives in `hacs.json`); single `iot_class` (`local_push`); dropped removed keys from `hacs.json`; fixed the CI manifest check; added **hassfest** + **HACS** validation jobs.
- **`deploy/ha/appletv_self_heal.yaml`** genericized (author entity_ids → `YOUR_*` placeholders), flagged optional/superseded by the built-in reload.
- **Config-entry diagnostics** (`diagnostics.py`) — the "Download diagnostics" button now returns a redacted snapshot (entry data/options, profile, coordinator snapshot, enforcer state, store counts) with AdGuard creds/URL, `api_key`, `apple_tv_ip`, and the parent-notify target redacted. Gives other parents a one-click, safe bug report.
- **Privacy scrub** across docs + test fixtures ahead of the public mirror.

**Tests:** suite 696 green. Config-flow / multi-entry-store first-run paths still need the `pytest-homeassistant-custom-component` harness (tracked); verified here by compile + reasoning.

### Added (v0.20.0 — ONE system, not two: the unified Living Room profile)

**Background (user-reported 2026-06-27).** "We have now two different segments: xbox and appleTV + TV. I want one system, not two!" v0.19.0 added the Xbox as a *separate* profile with its own budget. But the Xbox and the Apple TV share one room, one TV, and — in the user's mind — one screen-time allowance. Two profiles meant two budgets, two panel cards, two sets of sensors. This release folds them into ONE.

**Model — `secondary_devices` on the primary profile.** A profile gains one back-compat field:
- **`secondary_devices: list[dict]`** (default `[]`) — devices folded into THIS profile's single shared budget. Each item: `{entity_id, device_kind, enforcement_switch_entity_id, bundle_id}`. The PRIMARY stays `apple_tv_entity_id` (the Apple TV); the Living Room now carries one secondary: the Xbox (`device_tracker.xboxone` + `switch.xboxone_internet_access`, bundle `xbox.console` → gaming). Empty list ⇒ a profile behaves **byte-identically** to pre-v0.20.0. Round-trips automatically via `asdict` / the `from_dict` known-field filter.

**Accounting — room-as-unit, never double-counted.** A new `coordinator._resolve_room_activity` folds the primary + secondaries into ONE effective bundle for the single open `UsageEvent`, with **Apple-TV-wins priority**: `_effective_bundle_id` is consulted exactly once (authoritative, includes idle-grace); if the Apple TV is effectively active it owns the room (real per-app attribution); otherwise the first active secondary claims it (`xbox.console`). Because the two are mutually exclusive, the single event holds *either* an Apple TV app *or* `xbox.console` at any instant — overlap collapses to one, so `used_seconds_today` counts the room exactly once even when both devices are physically on. The Apple TV's idle-grace memory is never polluted with the synthetic Xbox bundle. So Apple TV minutes + Xbox minutes both bite the **same daily budget** (60 min) and the **gaming group** sub-cap (30 min) covers Xbox + Apple TV gaming together.

**Enforcement — one transition, both devices.** On budget exhaustion, the single `_enter_enforcing` now fans out: AdGuard/`media_player.turn_off` on the Apple TV + `turn_off` on the Samsung TV (unchanged) **plus** `switch.turn_off` on every secondary's switch (cuts Xbox internet). `_exit_enforcing` restores them symmetrically. Anti-defeat: `reassert`'s new **Job 3** re-asserts a secondary switch the kid flips back ON mid-block (the AdGuard drift-heal's equivalent for the FRITZ switch) — runs even when the Apple TV is off, so the Xbox can't be un-blocked by toggling the switch.

**Heads-up voices reach the Xbox-only room.** `someone_could_be_watching` gains a `secondary_active` signal so the warn + countdown voices still fire when the kid is on the Xbox with the Apple TV off and the Samsung reporting off/standby. And the Apple-TV staleness machinery (`_check_for_stale_session`, the `_sync_open_event` open-gate) is correctly scoped to the **primary**: it no longer refuses to open or wrongly closes a secondary's `xbox.console` event when the Apple TV is off/frozen — the single most important fix (without it, Xbox minutes were never tracked while the Apple TV was off ≥ `stale_session_minutes`).

**Live migration (one REST call).** `POST /limits {"secondary_devices":[…]}` attaches the Xbox to the Living Room profile — validated, persisted, and **restart-survivable** via the selective-merge tuple (empty-list-is-unset). The PATCH also re-wires the coordinator's state listener immediately (no second restart needed). Then the standalone Xbox config entry is deleted. The store is backed up first; the Xbox's prior history is keyed to the old profile id and is accepted-reset (it had ~0 min today).

**Adversarial review.** Two workflows: a grounded design + 3-lens code-grounded review of the plan (caught 7 real blockers — staleness gate blocking the Xbox event, the REST validator 422, the warn-voice gap, etc., all folded into the implementation before a line shipped), then a 3-lens review of the actual diff (all **ship**, zero blockers; surfaced 4 non-blocking polish items that were also fixed: false `enforcing_failed` on Xbox-only enforcement, `_exit_enforcing` secondary-unblock symmetry, seed back-compat, live-attach re-subscribe).

**Tests:** +34 — `tests/test_merge_unified_profile.py` (26: storage round-trip, the resolver union/Apple-TV-wins/Xbox-fold-in/back-compat/no-grace-pollution, the staleness-gate bypass + scope, the stale-session secondary guard, `someone_could_be_watching` secondary, enforcer fan-out enter/exit/failure-isolation/adult-bypass, reassert Job 3, plus the 4 review-fix regressions) + 8 in `tests/test_api_validators.py` (the `secondary_devices` REST validator + the no-`unknown-field` migration guard). Suite **695/695** green.

### Fixed (v0.19.3 — pyatv freeze MID-PLAYBACK now self-heals (was stuck for hours))

**Background (live-reported 2026-06-20).** "Again stale?" — the Apple TV `media_player` was frozen at `playing` for **73 minutes** (pyatv's push channel silently died mid-session, the recurring tvOS 26 bug `pyatv#2845`), while the Samsung TV was on and the kid was genuinely watching. Reloading the apple_tv config entry instantly recovered it — but nothing did that automatically.

**Why nothing auto-healed it.** The proactive in-integration reload (v0.18.0) deliberately scoped itself to stuck `idle`/`on` and left `playing` to the v0.17.3 stale-session path — which keeps *counting* usage correctly (via the Samsung-on liveness gate) but never *reloads* pyatv, so the mirror lies (frozen "current app") for hours. The YAML self-heal Case B only catches stuck-`playing` after **6 hours**. So a `playing` freeze in the 5 min–6 h window had no auto-recovery.

**Fix.** `decide_pyatv_reload` gains a second trigger shape for `playing`/`paused`/`buffering`:
- Threshold **20 min** of frozen `last_updated` (vs 10 min for `idle`/`on`) — normal tvOS 26 steady-playback push gaps run ~5 min, so 20 min is unambiguously a dead channel, not a long buffer.
- **Samsung-on is the liveness proof; the DNS corroborator is NOT required** for this branch. A steady stream is long-lived TCP that makes almost no new DNS, so the 60s DNS gate (kept for the `idle`/`on` branch) is blind to it. The reload is wake-safe + rate-limited (1/10 min), so the worst case (device actually off, Samsung stuck on) is a harmless reconnect that simply un-sticks the mirror.
- `idle`/`on` branch unchanged (10 min + DNS required).

Complementary to the stale-session path (which still counts the usage); the reload only refreshes HA's pyatv connection, never the Apple TV's playback. Reviewed adversarially — no over-reload/storm, rate-limit holds, phantom-reload bounded to a harmless wake-safe reconnect.

**Also:** restored the `apple_tv_ip` profile field on the live install (it had gone empty, which had disabled the DNS-gated `idle`/`on` reload + corroboration entirely).

**Tests:** +12 in `tests/test_decide_pyatv_reload.py` (stuck-playing past threshold without DNS → reload; just-under → no; idle-threshold-doesn't-apply-to-playing; Samsung off/standby → no; rate-limit; DNS ignored for playing in both directions). Suite 661/661 green.

### Fixed (v0.19.2 — heads-up voices nagged an empty room after adult-mode expiry)

**Background (live-reported 2026-06-19).** "TV and Apple TV are switched off — I still get an audio message?!" From the audit log: the movies group was exhausted (91.3 min vs the 30 min Friday cap). Adult mode (which had been masking the over-budget state) **expired at 21:26:33**. The instant it lapsed, the enforcer re-evaluated, saw movies still over budget, and walked a fresh **WARN (21:27) → GRACE → "30 seconds left" → ENFORCING (21:28)** cycle — speaking a voice on `media_player.dining_room` at each step — even though the Apple TV had been `idle` since 20:32 and the Samsung TV `off` since 20:32. Nobody was watching; the speaker nagged anyway.

**Root cause.** The **enforce** voice was already gated (v0.15.8 — it only fires after a `turn_off` verifies successful, so it stayed silent tonight). But the **warn** voice ([`audit.py`](custom_components/appletv_mgmt/audit.py)) and the **countdown** voice ([`enforcer.py`](custom_components/appletv_mgmt/enforcer.py) `_fire_countdown`) had **no "is anyone watching?" gate** — they fired on the state transition regardless of whether the screen was on.

**Fix.** New pure predicate `media_attribution.someone_could_be_watching(device_kind, primary_state, tv_state)`: fire a heads-up voice only if EITHER the primary device is actively consuming (`playing`/`paused`/`buffering`; for `xbox_presence`, `home`) OR the configured TV is `on`. Only suppress when BOTH say off — **fail-safe toward firing**, so a pyatv-stale `idle` (the tvOS 26 bug) while the TV is genuinely on still warns. The warn voice (audit.py) and the countdown voice (enforcer.py) now both consult it; `idle`/`off`/`standby`/`unavailable`/`on`-home-screen no longer trigger a voice when the TV is also off.

**Tests:** 16 new — `tests/test_someone_could_be_watching.py` (14: apple_tv playing/paused/buffering fire; idle/off/missing/home-screen suppressed; idle-but-TV-on fail-safe fires; xbox home vs not_home) + 2 countdown-suppression cases in `test_countdown_voice.py` (idle+TV-off → silent; idle+TV-on → fires). Suite 649/649 green.

### Fixed (v0.19.1 — "Add minutes" was a no-op when a GROUP limit was binding)

**Background (live-reported 2026-06-19).** The parent's kid was watching Netflix (movies group, Friday cap 30 min). At ~25 min the "5 min left" warning fired against the **movies group** budget — correct. The parent hit **+60**, then **+30** (today total +90) on the panel. Five minutes later `grace_start` fired on `group:movies` anyway, then "30 seconds left", and the parent had to fall back to **adult mode** to stop the nagging. From the audit log, daily usage at that moment was 33 of 150 effective minutes — nowhere near exhausted.

**Root cause.** Extensions only ever credited the **daily** budget pool (`extension_minutes_today` → `budget_s`). The binding constraint was the **movies GROUP** budget (30 min), which extensions never touched — [`enforcer.py`](custom_components/appletv_mgmt/enforcer.py) computed `group_remaining = group_budget − group_used` with no extension term. So "Add 30 min" was structurally a no-op exactly when a group sub-limit was what's biting (the most common real case), and the only escape was adult mode (which bypasses all budgets).

**Fix — group-tagged extensions.** A manual extension is now tagged to a group and the enforcer adds that group's extension to its budget:

- **Storage** gains a per-group extension pool (`_group_extensions_today: {profile_id: {group: minutes}}`) alongside the daily pool. `add_extension_minutes(profile_id, minutes, group=...)` credits the daily pool **always** and the group pool when a group is given. New readers `group_extension_minutes_today` / `group_extensions_today`. Serialized, reset at midnight, pruned per-profile. Back-compat: absent key → `{}`, malformed dump coerced/skipped.
- **Enforcer** `evaluate()` gains `group_extensions_seconds`. It builds `eff_group_budgets = {group: base + extension}` and uses it in the WARN/GRACE latch, the per-group binding check, and the v0.16.5 exhaustion-pin loop. **Daily ceiling is untouched** — the group extension only relaxes the group sub-limit, never the daily cap.
- **Grant resolution** (`audit.resolve_extension_target_group`) auto-tags a "+min" to the group currently being nagged (`enforce_reason` `group:X`), else the active group (`current_group`), else daily-only. Callers may force a group, or `"daily"` for daily-only. Wired into the REST `POST /extension`, the `grant_extension` HA service (new optional `group` field), and both request-approval paths in `notify.py` (which also fall back to the requested app's group).
- **Anti-defeat.** An extension is tagged to ONE group, so granting "+30 movies" cannot relieve a `gaming` cap — switching apps can't transfer the credit. The v0.17.0 close-reopen latch and v0.16.5 exhaustion pin are unweakened.

**Adversarial review caught one blocker before commit:** a group with budget `0` ("unlimited" by convention) would have become `0 + extension` — silently converting unlimited → capped-at-the-extension, turning a "+time" grant into a *denial*. Reachable because the active-group auto-tagging can route a grant to an unlimited group. Fixed: only **positive** group budgets receive the extension (a regression test pins this).

**Tests:** 13 new in `tests/test_group_extension.py` — the live-bug replay (movies exhausted + no extension → binding at 0; +90 → not binding, OK), anti-defeat (movies extension doesn't lift gaming), exhaustion-pin respects extension, partial extension, the zero-budget regression, daily-ceiling-holds, plus storage round-trip / midnight-reset / negative-floor. Suite 625/625 green.

### Added (v0.19.0 — Xbox MVP: track + enforce on a second device kind)

**Background.** The integration was Apple TV-only through v0.18.x. v0.19.0 adds a second device kind — **Xbox** — that reuses the existing profile/coordinator/REST/panel infrastructure without requiring any additional HA addons. This is the MVP: track Xbox usage time + enforce daily/quiet-window limits via a router switch flip. No Microsoft / Xbox Live account, no HACS install, no OAuth. Built on the FRITZ!Box-driven `device_tracker.*` (presence) + `switch.*` (internet-access) entities that are already present for any Xbox visible on the LAN.

**Profile model.** Two new fields, both back-compat:
- **`device_kind: str`** — `"apple_tv"` (default, every pre-v0.19.0 profile) or `"xbox_presence"`. The Profile.from_dict path defaults the key to `"apple_tv"` when missing, so live installs that load v0.19.0 for the first time keep their existing dispatch.
- **`enforcement_switch_entity_id: str | None`** — Xbox-only. The `switch.*` entity the enforcer turns OFF to cut Xbox internet access (typically `switch.<host>_internet_access` from the FRITZ!Box integration). For `device_kind=apple_tv` this is unused and ignored. PATCH /limits accepts it.

**Coordinator dispatch.** A new `_extract_activity_signal` helper translates the entity state into the `(media_state, app_id)` pair `_sync_open_event` consumes. For Apple TV profiles it's a pass-through (entity state + `app_id` attribute). For Xbox profiles it maps `device_tracker.state == "home"` → `("playing", "xbox.console")` and anything else → `(state, None)`, so the existing event-open/close pipeline treats the Xbox as a single foreground app named "xbox.console" routed to the **gaming** group via `CURATED` in `categorize.py`. Per-group budgets cover Xbox + Apple TV gaming together — a kid with `budget_gaming=30` can split that 30 min across both consoles.

**Pyatv-only hot paths short-circuit.** `_check_for_stale_session` (v0.17.3), `_check_pyatv_reload` (v0.18.0), and `_check_dns_corroboration` (v0.18.0) early-return for non-apple_tv profiles. None of those failure modes apply to a FRITZ!Box-driven `device_tracker` (which rolls presence every ~10 s reliably), and the AdGuard query log isn't being read against a non-Apple-TV IP anyway. The v0.18.1 TV-on accumulator + the generic state machine / budget enforcement code remain unchanged — those are device-agnostic.

**Enforcer dispatch.** The network-block primitive dispatches on `device_kind`. Apple TV profiles route to the v0.15.x AdGuard `set_blocked(True/False)` path (unchanged byte-for-byte). Xbox profiles route to `switch.turn_off(enforcement_switch_entity_id)` to block + `switch.turn_on(...)` to release. The pyatv `media_player.turn_off` watchdog (v0.15.4) is also skipped for Xbox profiles — there's no pyatv to retry and the entity_id is a `device_tracker`, not a `media_player`, so the call would fail. The Samsung-TV-shutdown fallback (v0.4.0 + v0.15.4) still applies if `tv_shutdown_target` is configured — pulling the TV power is a valid escalation for Xbox too.

**Config flow.** A new first step asks for the device kind (radio: Apple TV / Xbox). Apple TV path is the v0.1.x — v0.18.x form, unchanged. Xbox path is a smaller form (no AdGuard fields — the Xbox enforcement primitive doesn't need them):
- Profile name (default "Xbox")
- Xbox presence sensor (`device_tracker.*` entity selector)
- Xbox internet-access switch (`switch.*` entity selector)
- Daily budget minutes

Existing config entries (every pre-v0.19.0 profile) have no `device_kind` key in their entry.data; the `__init__.py` `Profile()` constructor defaults to `DEVICE_KIND_APPLE_TV` so HA-side reload is silent.

**REST API.** `PATCH /limits` accepts `enforcement_switch_entity_id` (null or string ≤ 128 chars). `GET /limits` surfaces both `device_kind` and `enforcement_switch_entity_id` so the panel can show "this is an Xbox profile" without poking at the storage internals. The `device_kind` field is read-only via REST — it's a config-flow-time decision that determines the wiring of the coordinator/enforcer, not a tunable.

**Live setup notes.** The Xbox is already on the LAN as `device_tracker.xboxone` (FRITZ!Box, MAC `aa:bb:cc:dd:ee:ff`) with `switch.xboxone_internet_access` driving the router-side internet block. Add the second profile via Settings → Devices → Apple TV Mgmt → ADD INTEGRATION → pick "Xbox" — set the entities + daily budget and the integration creates a parallel coordinator + sensor stack. The kid gets the same enforcement banner and the parent gets the same audit log, just with `xbox.console` as the bundle_id.

**What this MVP does NOT do (deferred to v0.20.0 if/when the owner installs the official HA `xbox` integration):**
- Per-game tracking (Halo vs Minecraft vs Forza). Without Xbox Live API access the integration can't see what app is foreground; every minute is bucketed against `xbox.console` → `gaming`.
- Remote Xbox power-off. The MVP blocks at the network layer (kid sees the dashboard freeze + the in-game multiplayer drop) and optionally chains the Samsung TV power-off via `tv_shutdown_target`, but the console itself stays powered until the kid hits the controller button.
- Account-level limits (e.g. "only this Microsoft account, not the other one"). MAC-level only.

**Commits.** TBD (this commit). Tests: 612/612 green (602 baseline + 10 new in `tests/test_xbox_mvp.py`: bundle-id constant, CURATED routing, Profile back-compat default, Xbox round-trip, 4 `_extract_activity_signal` cases, DEVICE_KINDS const surface).

### Added (v0.18.1 — track TV-on minutes today (observability only, no enforcement))

A new diagnostic sensor `samsung_tv_on_minutes_today` (EntityCategory.DIAGNOSTIC, `UnitOfTime.MINUTES`, `DURATION`/`MEASUREMENT`) accrues seconds while the configured `tv_entity_id` reports an on-ish state across the local day. States counted: `on` / `playing` / `paused` / `buffering`. States excluded: `off` / `standby` / `unavailable` / `unknown` / `idle`. Per-tick (~30 s) accumulator credits the gap between consecutive on-ticks, paused when state moves to off, reset at local midnight by the existing `_handle_midnight_check` hook. Sanity-capped at 10 min per tick delta so a clock jump (DST, host sleep+wake) can't silently dump hours into the counter. Pure local-state update — no I/O, no enforcement, no event mutation. Not persisted across HA restart (acceptable for a pure-diagnostic sensor; in-day total restarts at 0). Useful because the Samsung's `samsungtv_encrypted` legacy HACS platform only reports on/off — HA's history graph shows transitions but no daily total; now the parent dashboard gets one glanceable minutes-today number that's true even when the kid wasn't on the Apple TV.

**Commit.** `6cd30b9`. Tests: 9 new in `tests/test_diag_sensors.py` covering no-tv-id short-circuit, accrual, on→off→on pause, `playing`/`paused`/`buffering` treated as on, exclusion of `unavailable`/`unknown`/`idle`, sanity cap, entity-missing fail-safe, sensor description registered, value_fn seconds→minutes conversion.

### Added (v0.18.0 — DNS-corroborated attribution for pyatv-silent streaming sessions)

**Background.** On 2026-06-14 06:27-08:33 the owner's install booked **126.7 min as Disney+** on a single open `UsageEvent` — but AdGuard's DNS log shows the kid actually watched ~9 min of Disney+ movies followed by ~117 min of **KooApps gaming** (LEGO Star Wars Castaways, served from `*.execute-api.us-west-2.amazonaws.com` behind hash `09nzmxy3h5`). pyatv emits push events on the Disney+ app-launch and on chunk boundaries, but **when a second app launches over a still-playing Disney+ session the companion protocol stays silent** — the `media_player` mirror keeps reporting `playing com.disney.disneyplus` while the foreground app is gaming. v0.17.x has no signal at all that anything changed. The kid silently spent ~2h of gaming budget booked against movies.

**The mechanism (truth-preserving — no close-and-reopen).** Two new pure modules cross-check the live `UsageEvent` against AdGuard's per-IP DNS log every ~30 s coordinator tick:

- **`dns_classifier.py`** — fetches DNS queries from the AdGuard proxy filtered to the configured Apple TV IP (`apple_tv_ip` Profile field) and scores them against a curated seed list into a four-level confidence ladder: `NONE < AMBIENT_ONLY < GROUP_ONLY < BUNDLE`. `BUNDLE` = a hit on a known app's signature domain (e.g. `disney-plus.net`, `nflxvideo.net`, `09nzmxy3h5.execute-api.us-west-2.amazonaws.com`); `GROUP_ONLY` = a group-correlated heartbeat (Game Center, App Store reachability) that names a group but not a bundle; `AMBIENT_ONLY` = generic Apple background keepalive / iCloud / Sonos discovery that tells us the device is on but says nothing about what's playing.

- **`media_attribution.decide_attribution`** (pure, HA-free) — combines pyatv freshness, the classifier confidence, and a curated streaming-bundle safelist into a 13-rule decision table returning one of three `AttributionAction`s:
  - **`PRESERVE`** — pyatv is talking (push within `pyatv-fresh = 90 s`) → trust pyatv absolutely, ignore DNS.
  - **`ANNOTATE_GROUP`** — pyatv has been silent and DNS evidence crosses `MIN_BUNDLE_CROSS_GROUP_HITS = 3` BUNDLE hits in a different curated group → append a `GroupSegment(start_at, end_at, group)` annotation to the open event's `group_segments` list. **The booked bundle id stays Disney+**; only the per-group totals (`group_seconds_breakdown`) reflect the correction. Append-only — no close, no reopen, no churn.
  - **`CLOSE_AT_LAST_UPDATED`** — sustained `AMBIENT_ONLY` for ≥ 4 consecutive ticks (~2 min) AND the open bundle is NOT in `CURATED_STREAMING_BUNDLES` → close the event at the entity's `last_updated` (visible-gap close, parallels v0.17.1 stale-pruner; protects the kid from being charged for "the kid walked away while Twitch was still on the home screen").

**Why append-only.** Closing-and-reopening UsageEvents on every group correction would (a) churn `app_started`/`app_ended` audit rows by the thousands during a long Disney+ session, (b) destroy the v0.17.3 stale-session contract (`last_updated` semantics expect one open event per backing app), and (c) lose the "what was the foreground bundle actually" signal. `GroupSegment` is a 3-tuple appended to a list field on the existing `UsageEvent` — the event keeps its single `bundle_id`, the dashboard's per-group totals read from segments when present, and legacy events with `group_segments=[]` map to the curated group exactly as v0.17.x (bit-identical, serialization omits the empty list for backwards compat).

**Coordinator integration.** `_check_dns_corroboration` runs every tick (~30 s) AFTER `_check_for_stale_session` and BEFORE `_sync_open_event`. AdGuard queries are TTL-cached for 25 s and keyed by `(event.id, apple_tv_ip)`; a per-event `asyncio.Lock` single-flights concurrent ticks so we never issue duplicate queries. Hot path is zero-network when the feature is off OR `apple_tv_ip` is empty. **Fail-open**: any AdGuard error (proxy down, timeout, malformed response) returns `[]` and the classifier degrades to `NONE` — the integration silently reverts to v0.17.x pyatv-only behavior, no audit spam, no enforcement disruption.

**Configuration.** Two new `Profile` fields, both opt-in:

- **`apple_tv_ip: str | None`** (default `None`) — the LAN IP AdGuard sees the Apple TV at. Exact-IP filter closes the sibling-device leak: a parent's iPhone resolving `disney-plus.net` from `192.168.1.244` will not be misattributed to the kid's Apple TV at `192.168.1.24`. Empty `apple_tv_ip` forces the feature OFF regardless of mode (safe default).
- **`dns_corroboration_mode: Literal['off','monitor','correct']`** (default `'off'`):
  - `off` — disabled, no AdGuard query, behaviour identical to v0.17.x.
  - `monitor` — queries AdGuard, writes `app_group_corrected_proposed` audit rows describing what *would* have changed, but **does NOT mutate `UsageEvent`s or sensors**. Designed as a 1-2 week verification phase before flipping to `correct`.
  - `correct` — queries AdGuard, appends `GroupSegment` to live events, per-group sensors reflect the corrected totals.

**REST API.** `PATCH /limits` accepts both new fields (`apple_tv_ip` is free-form string ≤ 45 chars, `dns_corroboration_mode` is an enum-validated `off|monitor|correct`). `GET /limits` surfaces both in the response payload alongside the existing v0.17.x fields. Live-verified on the owner's Pi.

**Behaviour matrix**

| Scenario | `off` (default) | `monitor` | `correct` |
|---|---|---|---|
| AdGuard query per tick | no | yes | yes |
| `app_group_corrected_proposed` audit row | n/a | yes | yes |
| `UsageEvent.group_segments` mutation | n/a | no | yes |
| Per-group sensors reflect DNS correction | n/a | no | yes |
| `apple_tv_ip` empty | identical to v0.17.x | feature forced OFF | feature forced OFF |
| AdGuard proxy unreachable | n/a | fail-open, no audit | fail-open, no mutation |
| Sustained AMBIENT close (kid walked away) | no | proposal audit only | event closed at `last_updated` |
| `CURATED_STREAMING_BUNDLES` safelist (23 entries — major streamers + DE broadcast catch-ups + music apps + AirPlay) | n/a | safelist blocks downgrade + close proposals | safelist blocks downgrade + close mutations |

**Safety nets.**

- **`CURATED_STREAMING_BUNDLES` safelist (23 entries)** — Disney+, Netflix, Prime Video, YouTube, Twitch, Plex, Jellyfin (3 bundle variants), Paramount+, Hulu, the major German broadcast catch-ups (ARD-Mediathek, ZDF-Mediathek, RTL, ProSieben/7TV, Joyn, Sky Go/Ticket, DAZN), Spotify, Apple Music + TVMusic, and AirPlay. Each blocks BOTH `ANNOTATE_GROUP` downgrades from `GROUP_ONLY` (e.g. a stray Game Center heartbeat must not downgrade Disney+ to gaming) AND sustained-AMBIENT `CLOSE_AT_LAST_UPDATED` closes (a buffered Disney+ stream can legitimately make zero DNS queries for an hour — the classifier sees `AMBIENT_ONLY`, but the bundle is trusted, so we hold). Without this safelist, live monitor-mode on 2026-06-14 emitted **31 spurious close proposals in 20 min on Disney+** (commit 18c034a). Authoritative list lives in `custom_components/appletv_mgmt/media_attribution.py::CURATED_STREAMING_BUNDLES`.
- **`MIN_BUNDLE_CROSS_GROUP_HITS = 3`** — cross-group corrections require at least 3 BUNDLE hits to the new group before annotating. One stray DNS lookup cannot flip attribution.
- **pyatv-fresh threshold = 90 s** — when pyatv pushed within the last 90 s we trust it absolutely, regardless of DNS. The classifier only gets a say during steady-state silence.
- **Sustained-AMBIENT 4-tick (~2 min) gate** — single-tick AMBIENT cannot close anything; only sustained absence-of-signal does.
- **Dedup map** — per `(event_id, new_group)` for ANNOTATE_GROUP and per-event `__close__` key for CLOSE proposals prevents per-tick re-emission of the same correction.
- **Fail-open AdGuard** — any error (proxy down, network timeout, malformed JSON) returns empty result; classifier degrades to `NONE`; v0.17.x behaviour preserved.

**Seed list (curated for the owner's install + known background).**

- **Streaming bundles:** Disney+ (`disney-plus.net`, `disneyplus.com`), Netflix (`nflxvideo.net`, `nflxext.com`), Prime Video (`atv-ps.amazon.com`, `aiv-cdn.net`), YouTube (`googlevideo.com`, `youtube.com`), Twitch (`ttvnw.net`, `twitch.tv`), Plex (`plex.direct`, `plex.tv`), Jellyfin (user-host), ARD/ZDF Mediatheken (`ardmediathek.de`, `zdf.de`).
- **Games:** KooApps (`koo-apps.com` + AWS API Gateway hash `09nzmxy3h5.execute-api.us-west-2.amazonaws.com`), Perchang, Gameloft (`gameloft.com`), Game Center heartbeat (`gc.apple.com`, group-only).
- **Ambient allow-list:** Apple keepalive (`time.apple.com`, `gsa.apple.com`), iCloud (`icloud.com`), Sonos discovery, NTP, mDNS.

**Live HW test follow-ups.** Three corrective commits landed during 2026-06-14 monitor-mode rollout on the owner's Pi, all surfaced by `app_group_corrected_proposed` audit rows BEFORE the feature was flipped to `correct`:

- **18c034a** — sticky-bundle for AMBIENT close + dedup. Pre-fix: Disney+ steady streaming with no new DNS produced 31 spurious close proposals in 20 min (every tick re-evaluated the sustained-AMBIENT gate from scratch). Fix: extend `CURATED_STREAMING_BUNDLES` safelist to also block sustained-AMBIENT close, plus per-event `__close__` dedup key in the proposal map.
- **e3b9d05** — added `gameloft.com` to the DNS classifier seed. Caught by a live LEGO Star Wars Castaways session emitting `*.gameloft.com` lookups that landed as `NONE` confidence pre-fix.
- **94b5c2d** — added KooApps AWS API Gateway hash `09nzmxy3h5.execute-api.us-west-2.amazonaws.com` to the seed. The hash is more specific than the generic `execute-api.us-west-2.amazonaws.com` substring (which would over-match other AWS-hosted apps); confirmed against the original 2026-06-14 incident log.

**Migration / compatibility.**

- **Disabled by default on upgrade.** `dns_corroboration_mode` defaults to `'off'` and `apple_tv_ip` defaults to `None` on Profile load; existing v0.17.x installs are zero-impact.
- **Empty `apple_tv_ip` forces feature OFF** regardless of mode — a safe default that protects against accidental `mode='correct'` with no IP configured (which would query AdGuard with no filter and risk sibling-device leakage).
- **`UsageEvent.group_segments` backwards-compat.** Legacy events without the field map to the curated group exactly as v0.17.x (`group_seconds_breakdown` falls back to the curated group when `group_segments=[]`). Serialization omits the empty list so old storage dumps round-trip bit-identically.
- **No schema migration required.** `group_segments` is an optional field on `UsageEvent`; missing in old dumps decodes to `[]` via dataclass default. Forward-compatible.
- **Sensors.** Three new diagnostic sensors ship **disabled by default** (enable from the entity registry): `attribution_source` (`pyatv` | `dns` | `unknown`), `dns_classifier_confidence` (`NONE` | `AMBIENT_ONLY` | `GROUP_ONLY` | `BUNDLE`), `attribution_gap_minutes_today` (discrepancy between booked bundle's group and DNS-corrected group, today's total). Primary 6 sensors are unchanged.

**Tests.**

- `tests/test_dns_classifier.py` — 27 tests. Confidence-ladder rules, recency tiers, exact-IP filter, BUNDLE/GROUP_ONLY/AMBIENT/NONE classification, seed-list coverage including the 2026-06-14 KooApps + Gameloft replay.
- `tests/test_decide_attribution.py` — 32 tests. Full 13-rule decision-table coverage including 5 injustice traps verified closed: (a) gaming-as-movies (the live 2026-06-14 bug), (b) movies-as-gaming (sibling Game Center heartbeat must not downgrade Disney+), (c) silent AMBIENT close on trusted streaming bundle (must NOT close), (d) App-Store over-charge (App Store reachability ping must not annotate as gaming), (e) sibling-device DNS leak (parent's phone resolving `disney-plus.net` at a different IP must not affect kid's event).
- `tests/test_dns_corroboration_integration.py` — 15 tests. Coordinator-tick integration including the 2026-06-14 06:27-08:33 acceptance test: single Disney+ event with `group_segments = [(06:27-06:36, movies), (06:36-08:33, gaming)]`, monitor vs correct mode behaviour split, `app_group_corrected_proposed` audit row shape, TTL cache hit/miss, single-flight lock, fail-open on AdGuard error.
- `tests/test_group_segments.py` — 12 tests. `GroupSegment` dataclass round-trip, `group_seconds_breakdown` honors segments when present + legacy fallback to curated group, serialization omits empty list, the 2026-06-14 live-bug shape (9 min movies + 117 min gaming on a single Disney+ event).
- `tests/test_adguard_querylog.py` — exact-IP filter, recency cutoff, fail-open on HTTP error / timeout / malformed JSON.
- **Live-bug replay integrated.** The 2026-06-14 06:27-08:33 incident is encoded as an integration test (`test_dns_corroboration_integration.py`) that asserts the corrected per-group breakdown matches AdGuard's ground truth.
- **Suite progression:** v0.17.4 baseline 528 → +48 from the in-integration proactive pyatv reload (decide_pyatv_reload + 13 coordinator integration cases + 6 PATCH validators) → +17 from the diagnostic sensors (14 unit + 3 corroboration-integration cases) = **593/593 green** on CI at end of v0.18.0 development. (The DNS-corroboration pure modules + integration tests were merged in commits d580b41 / 1c200dc / 18c034a / e3b9d05 / 94b5c2d against the pre-v0.18.0 528-test baseline that already included them — the +91 progression from that earlier baseline was a transitional milestone, not the released state.)

**Known limitations.**

- **Fully-offline games remain undetectable by design.** A game that emits zero DNS (rare — even most "offline" tvOS games ping Game Center) leaves no signal. The classifier returns `NONE`, pyatv stays trusted, the booked bundle is whatever pyatv last reported. Same blind spot as v0.17.x; DNS-corroboration is strictly additive.
- **App-group-level resolution only.** A kid switching between two apps in the same curated group (e.g. Disney+ → Netflix) within the same open event is not detectable from group-level evidence — the corrected segment would say "movies → movies". Sub-minute app-switch precision is out of scope.
- **Seed list is curated, not exhaustive.** New streaming services or games not in the seed will be classified as `NONE` (no signal) and pyatv will be trusted as fallback. `monitor` mode is designed to surface gaps safely via `app_group_corrected_proposed` audit rows showing `confidence=NONE` on events the parent recognises as the wrong group — a maintenance prompt to seed the new domain.
- **Monitor-to-correct flip is parent-initiated.** No auto-promotion. A parent who forgets to flip from `monitor` to `correct` after the verification window gets corrections proposed in the audit log but never applied to sensors. Acceptable trade-off: the audit log is the parent's verification surface.

**Commits.** d580b41 (P0.1 — pure modules foundation), 1c200dc (P0.6 — coordinator integration), f9267ba (P0.5 — REST API surface), 18c034a (sticky-bundle for AMBIENT close + dedup), e3b9d05 (Gameloft seed), 94b5c2d (KooApps AWS API Gateway hash seed).

### Added (v0.18.0 — operations follow-ups)

Three follow-up cohorts landed alongside the DNS-corroboration core, hardening recovery + observability:

- **Self-heal Case C — stuck `idle`.** `deploy/ha/appletv_self_heal.yaml` gains a third arm: if `media_player.heimkinoaaa` is stuck at `idle` for ≥ 10 min while the Samsung TV reports `on` STRICTLY (NOT the fail-open `tv != 'off'` of Cases A and B — legit home-screen browsing is the common `idle` shape), reload the apple_tv config entry. Calibrated against a live 2026-06-14 incident (pyatv stuck `idle` for 13+ min while the kid was actively using the device) with ~1.3× margin. The recovery-gated audit was rewritten from a blacklist (`not in {'unknown','unavailable'}`) to a per-case post-state whitelist (Case A: → `playing|paused|buffering|on`; Case B: → `off|paused|buffering|on`; Case C: → `playing|paused|buffering|on`), which closes the `idle → idle` no-op hole the old blacklist would have mis-fired on. Cases A and B condition arms are byte-identical to before. Reload primitive reused — empirically wake-safe per the 2026-06-08 Case-A HW test.

- **In-integration proactive pyatv reload (P1).** A code-driven complement to the YAML self-heal: when pyatv has been push-quiet ≥ 10 min AND Samsung is `on` AND AdGuard shows recent DNS hits from the Apple TV's IP, the coordinator calls `hass.config_entries.async_reload(profile.apple_tv_entry_id)` directly. Rate-limited to one reload per 10 min. New pure function `decide_pyatv_reload(PyatvReloadInputs) -> bool` in `media_attribution.py` — fail-CLOSED on every gate (spurious reload is worse than missed reload). Buffered Disney+ regression check: during steady playback the DNS gate is empty → reload does NOT fire (verified by adversarial review + test). New `Profile.apple_tv_entry_id: str` field (defaults to empty; back-compat for old persisted profiles). REST `PATCH /limits` accepts the field. The YAML self-heal stays in place as the deploy-level fallback for installs where the integration itself is down.

- **Three diagnostic sensors (EntityCategory.DIAGNOSTIC, opt-in via entity registry).**
  - **`sensor.<profile>_attribution_source`** — short `<action>:<reason_hint>` label of the last attribution decision: `preserve:pyatv_fresh`, `annotate_group:dns_bundle`, `close_at_last_updated:sustained`, plus `disabled` (mode=off), `no_open_event`.
  - **`sensor.<profile>_dns_classifier_confidence`** — last classifier verdict: `NONE | AMBIENT_ONLY | GROUP_ONLY | BUNDLE`.
  - **`sensor.<profile>_attribution_gap_minutes_today`** (`UnitOfTime.MINUTES`, `DURATION`, `MEASUREMENT`) — total minutes today annotated via dns_classifier-source `GroupSegment` (a continuous measure of how much pyatv silence the classifier corrected). HA long-term-statistics will graph it; expect the value to monotonically grow during open dns_classifier segments and reset at local midnight by design.

**Commits.** wf-10 self-heal Case C → `453fd7a`; wf-11 in-integration proactive pyatv reload → `1857771`; wf-12 diagnostic sensors → `31b4d22`; conflict-resolution merge → `0890f25`. Final ops-follow-up suite: **593/593 green**.

### Fixed (v0.17.4 — watchdog spam when Apple TV is idle)

- **The v0.15.4 pyatv watchdog kept retrying `media_player.turn_off` every 60s
  even after the kid had stopped watching** (Apple TV `idle` on the home
  screen). Once v0.17.3 made the budget actually bite, this produced
  **184 ERROR log rows + 184 `enforce_turn_off_failed` audit rows over 6
  hours** on the live install, while the kid was nowhere near the TV.
- **Root cause:** comment-vs-code mismatch. The watchdog's quiet-check
  used `INACTIVE_MEDIA_STATES` (which does NOT include `idle`), but the
  adjacent comment claimed "Apple TV is off/standby/idle — watchdog
  quiet." Fix: use a new public `ACTIVELY_PLAYING_STATES = {playing,
  paused, buffering}` (the same set already used by `media_attribution`
  for "kid actively watching something"). `idle`/`on` are home-screen
  navigation, not active consumption, so the watchdog now correctly stays
  quiet when the kid walks away.
- **Watchdog log level** demoted from `ERROR` (every retry) to a single
  `WARNING` per retry, AND the per-retry `enforce_turn_off_failed` audit
  row is suppressed. The FIRST enforce attempt (label without
  `(watchdog)`) still logs ERROR + audit row as before — what matters
  operationally is the failure event, not the count of identical retries.
- Three new regression tests cover the bug + the no-regression case
  (watchdog still fires when actually playing). Full suite 437/437 green.

### Fixed (v0.17.3 — usage under-count regression)

- **Samsung-liveness gate on the stale-session pruner.** v0.17.1's staleness
  detection (close the open `UsageEvent` at `last_updated` when the apple_tv
  mirror has been push-quiet for >= `stale_session_minutes`) was added to fix
  the overnight-Disney+ 12.9h-phantom-usage bug — but on tvOS 26 Apple TV 4K
  it caused a SEVERE under-count: pyatv pushes very rarely during steady
  playback (~once per 5+ min observed; 312s gaps recorded), so the check
  fired every push-quiet window and closed back at the open-time
  `last_updated`, recording **0.0 min for the chunk** even while the kid was
  actually watching. Live cross-check on 2026-06-09 against Samsung TV
  history: 2 of 3 zero-duration sessions today were genuine under-counts
  (Samsung stayed `on` throughout); the v0.17.1 pruner mis-classified them
  as "device went off". For an enforcement tool this is the worst failure
  mode — the budget silently lifts.
- **Fix shape:** added a new pure function `decide_stale_action` in
  `media_attribution.py` (HA-free, unit-testable) that takes apple_tv state
  + age, Samsung TV state, and open-event age, and returns one of
  `NOOP` / `KEEP_OPEN` / `CLOSE_AT_LAST_UPDATED` / `CLOSE_RUNAWAY`. The
  coordinator's `_check_for_stale_session` is now a thin adapter that reads
  the entities, clamps clock skew, and acts on the result.
- **Decision rule:** when the apple_tv mirror is stale-in-ACTIVE, consult the
  Samsung TV state (reusing `Profile.tv_entity_id` — the same field already
  set by the v0.4.0 TV-shutdown feature). Samsung `off`/`standby` → close at
  `last_updated` (overnight-Disney path preserved). Samsung
  `on`/`unknown`/`unavailable`/anything else → `KEEP_OPEN`, the event
  accrues to `now` naturally. Bias under uncertainty is keep-counting
  because under-count is the worse failure for an enforcement tool.
- **Safety nets** preventing unbounded over-count if liveness is broken:
  - When `tv_entity_id` is NOT configured → fall back to v0.17.1 contract
    (`CLOSE_AT_LAST_UPDATED`). All 17 existing v0.17.1/2 regression tests
    stay green by construction (fixture defaults `tv_entity_id=None`).
  - `CLOSE_RUNAWAY` hard cap fires once the open event's age exceeds
    `STALE_RUNAWAY_CEILING_S = 150 min` (~2x the longest legit
    continuous-`playing` span observed in 8 days of history — 74 min), so
    a stuck-`on` Samsung or uninstalled-Samsung integration can't grant
    more than ~2.5h phantom usage. Audit detail calls out runaway so the
    parent sees why.
- AdGuard DNS-recency was rejected as a liveness signal (the original
  v1 self-heal gate fragility re-applies — standby Apple TV emits Apple
  background keepalive every 1-12 min so DNS recency cannot separate
  standby from active, proven earlier this cycle).
- Designed via two adversarial review rounds (5-agent workflow,
  ~465k tokens). Both reviewers' hybrid recommendation incorporated:
  no-cache, strict OFF allow-list (`{'off','standby'}` only), and the
  runaway-ceiling safety net.
- **Tests:** 35 new tests (28 pure-function decision-table covering all
  rules + edge cases, including 3 reproductions of today's live
  under-count sessions; 7 coordinator-integration covering the
  `tv_entity_id`-configured branches). Full suite **434/434 green**.

### Added (deployment / ops)

- **pyatv stale-state self-heal** — HA config in `deploy/ha/appletv_self_heal.yaml`.
  Auto-recovers from the recurring tvOS 26 Companion-protocol drop (pyatv#2845)
  where `media_player.heimkinoaaa` freezes at `off`/`unavailable` while the
  Apple TV is actually playing (root cause behind "Apple TV on but nothing on
  the HA"). An automation reloads the `apple_tv` config entry when the mirror is
  off/unavailable **and the Samsung display is not definitively off** (fail-open
  on unknown/unavailable, so a real freeze is never missed; suppressed when the
  TV is off, which kills standby/overnight churn). No external DNS/proxy gate.
  **Recovery-gated audit**: a logbook line is written only when a reload actually
  un-sticks the mirror, so harmless reloads are silent. Reload verified
  empirically to be **wake-safe** (a sleeping Apple TV stays off, no new traffic).
  Validated with a hardware-in-the-loop test (Sonos silenced, full truth-table,
  live standby) and two rounds of adversarial review. Live on the owner's Pi 2026-06-08.
  - *Case B (stale-`playing`) now covered:* a freeze where the mirror is stuck on
    `playing` for hours after the TV was turned off (over-counts screen time) is
    healed via CONTINUOUS-playing duration (`last_changed` age > 6h) — not
    `last_updated` (event-driven, would false-fire on a long healthy title) and
    not `media_position` (which is `None` on this device — tvos26 breaks the
    now-playing fetch). The 6h threshold is data-validated against 8 days of
    history: it catches exactly the one real freeze (Disney+ stuck `playing`
    00:39→10:26, 588 min, overnight 2026-05-31) and zero legit sessions (longest
    legit continuous-`playing` span: 74 min). Recovery-gated audit compares
    pre/post-reload state — with a two-sample stability check (the reload's
    reconnect flickers through transient states, so the recovered state must hold
    for 6s) — logging both a Case-A off→on heal and a Case-B playing→off cleanup,
    silent on a no-op.
  - *HW-in-the-loop test (2026-06-08, Sonos silenced):* T1 live standby → no fire
    (suppression confirmed); T2 live Case-A condition → automation fires the
    reload, Apple TV stays off (wake-safe), recovery-gate silent on the no-op.
    The transient-`on` false-log risk above was caught by this test and fixed.
    Active-playback couldn't be automated — the encrypted Samsung can't be powered
    on over the network (no WoL), so a muted display couldn't be guaranteed.
  - *Superseded:* an earlier DNS-activity gate was dropped — a standby Apple TV's
    background keepalive DNS kept it open (false positives), and a proxy hiccup
    made it fail closed (missed real freezes).

Planned next:
- One shared `AppleTVMgmtStore` across config entries (today each entry has its own — fine for 1 Apple TV but corrupts data with 2+).
- Auto-generate `api_key` on first config-entry creation (currently OPEN by default; documented in README §13 — fine for the owner, wrong default for HACS).
- Rename `Profile` → `Device` before HACS submission (breaking API change deferred to v1.0).
- Replace the chronic pyatv companion-protocol with a more reliable kill switch — smart plug on Apple TV power, or HDMI matrix disconnect. The Samsung TV fallback works when reachable, but doesn't help when it's already off and the kid could turn it back on to resume the still-active Apple TV stream.
- Panel icons + (optional) action labels for the new `reactivation` and `parent_notified` audit rows so the Recent Activity card surfaces v0.16.0 events with the same visual weight as enforce/voice rows.

---

## [0.17.2] — 2026-05-31

Live-test follow-up to v0.17.1. The staleness detection introduced in v0.17.1 correctly CLOSED stuck sessions, but `_sync_open_event` immediately reopened a fresh session against the same stale entity state on the very next tick — creating an audit-log loop:

```
app_stale_closed  entity stale 325s; closed at last…
app_started       Disney+ opened
app_ended         Disney+ closed (0.0 min)
app_stale_closed  entity stale 355s; closed at last…
app_started       Disney+ opened
app_ended         Disney+ closed (0.0 min)
…hundreds per hour…
```

Surfaced during the 2026-05-31 live hardware-in-the-loop test using REST state injection. Without the fix, `used_today_min` would re-roll-back to ~0 every 5 minutes as long sessions got truncated to 0-min duration at the stale `last_updated` timestamp — making real-world enforcement unpredictable.

### Fixed

- **`Coordinator._sync_open_event`** now consults a new `_entity_is_stale(now)` helper before opening a new `UsageEvent`. If the source entity's `last_updated` is ≥ `profile.stale_session_minutes` ago, opening is refused and the method returns without effect. CLOSING decisions (entity transitions to off / standby / idle) are unchanged — a legitimate stop still closes the open session.
- **`_entity_is_stale(now)`** centralizes the staleness check so v0.17.1's `_check_for_stale_session` and v0.17.2's `_sync_open_event` gate use the same source of truth. `stale_session_minutes = 0` keeps the existing opt-out semantics.
- The gate is *targeted* — only blocks opening NEW sessions or reopening on bundle changes. A continuation of an already-open same-bundle session is unaffected (we're just observing accrued time, not making a decision based on the stale state). Stitch path is also bypassed because stitching is a continuation, not a fresh start.

### Tests

- 6 new tests in `tests/test_stale_session.py`:
  - the live-bug repro (stale entity → open refused)
  - sanity inverse (fresh entity → open proceeds normally)
  - inactive-state-still-closes (stale state but `state=off` → close happens)
  - same-bundle continuation (no opening, gate is irrelevant)
  - bundle change with fresh entity (close old + open new works)
  - `_entity_is_stale` opt-out boundary (`stale_session_minutes=0`)
- 362 passing (was 356, +6).

### Architectural note

v0.17.1 + v0.17.2 together form a complete staleness defense:
- v0.17.1: close existing open sessions whose backing entity has gone stale (catches the overnight phantom)
- v0.17.2: refuse to open new sessions while the backing entity is stale (catches the close-then-reopen loop)

Both rely on the same `stale_session_minutes` threshold + the same `_entity_is_stale` helper. Setting the field to `0` opts out of BOTH behaviours — the integration behaves exactly as in v0.17.0.

---

## [0.17.1] — 2026-05-31

Live-reported P0 the v0.17.0 BA audit didn't surface (audit focused on state-machine + voice paths, not staleness of the upstream `media_player` entity).

**The bug.** the owner's kid opened Disney+ at 21:28 UTC (2026-05-30). pyatv's companion protocol disconnected silently at ~00:39 UTC overnight; HA's `media_player.heimkinoaaa` entity stayed at its last cached state (`playing com.disney.disneyplus`) but stopped getting refreshed. The coordinator only reacts to state CHANGES, so no `_sync_open_event` fired — the open `UsageEvent` kept accruing time. By 10:30 UTC the next morning, the dashboard showed **12h 21m of "today's Disney+ usage"** even though the Apple TV had been physically off since ~02:00 UTC. Switching the integration into `enforced` mode triggered repeated `enforce_turn_off_failed` audit rows (pyatv couldn't reach the now-off device).

### Fixed

- **`Coordinator._check_for_stale_session(now)`** — new tick hook that runs BEFORE `_sync_open_event`. When the `apple_tv_entity_id` is in an `ACTIVE_MEDIA_STATES` state but its `last_updated` is ≥ `profile.stale_session_minutes` ago, the coordinator closes the open `UsageEvent` at the entity's `last_updated` timestamp (best-guess actual end time, **not `now`** — preserves the kid's right not to be charged for time accrued after pyatv died).
- **New audit action** `app_stale_closed` with detail `"<bundle_id> entity stale <Ns>; closed at last_updated <iso>"` so the cleanup is visible on the dashboard.
- **New `Profile.stale_session_minutes`** field (default 5, range 0..60). `0` disables the check. Exposed through `PATCH /limits` + `LIMITS_PATCH_FIELDS`.

### Behaviour matrix

| Entity state | last_updated age | Pre-v0.17.1 | v0.17.1 |
|---|---|---|---|
| `playing` | 30 s | session continues | session continues |
| `playing` | ≥ 5 min | session continues — phantom time accrues | session closed at last_updated; audit row |
| `paused` / `buffering` | ≥ 5 min | session continues — phantom time accrues | session closed at last_updated; audit row |
| `off` / `standby` | any | `_sync_open_event` closes it | `_sync_open_event` closes it (same — F is a no-op for inactive states) |
| no open session | any | no-op | no-op |
| `stale_session_minutes = 0` | any | no check | no check (opt-out) |

### Tests

- New `tests/test_stale_session.py` — 10 tests covering: threshold=0 disabled, no open session, inactive state, recent state, missing entity, the live repro (close at last_updated NOT `now`), audit row contents, boundary at exactly the threshold, paused-state also-stale, audit exception non-fatal.
- 356 passing (was 346, +10).

### Migration / compatibility

- New `Profile.stale_session_minutes` defaults to 5 — existing profiles get the safeguard automatically on load.
- Setting it to 0 via `PATCH /limits` opts out (useful if a test scenario or unusual deployment wants the legacy behavior).

---

## [0.17.0] — 2026-05-30

Comprehensive follow-through on the post-v0.16.5 BA audit (parallel Opus + Sonnet reviews → adversarial plan review by a third Opus session). Sixteen findings deduplicated into 13 fixes shipped across 5 cohorts, organised by cross-cutting theme rather than severity. **No further P0 / P1 BA findings remain open.**

The full audit + plan history is in the PR description: [/tmp/audit_v016_opus.md, /tmp/audit_v016_sonnet.md, /tmp/v016_plan_v2.md, /tmp/v016_plan_challenge.md]. Total test count: **318 → 346** (+28 across the release).

### Cohort 1 — Adult-mode preserves the reactivation counter (F-B)

`_exit_enforcing` accepts a new `reason` arg; the caller (currently only the adult-mode branch in `evaluate()`) signals a TEMPORARY exit. Adult-mode exits preserve `_reactivation_count` + the edge-detector flag so the kid can't earn a free "reactivation #1" by triggering adult mode, waiting it out, and re-arming. **the owner Q4 decision: post-adult-mode reverts to a fresh WARN→GRACE cycle** (state.py:50-51 already does this — v0.16.2 fix composes naturally).

### Cohort 2 — Theme A: policy-bypass cleanup (F-C, F-F, F-G, F-H, F-N)

Five sites that missed the v0.16.4 unification of the `decision.kind != "ACT"` gate.

- **F-C** — `_watch_reactivation_locked` splits BYPASS vs OBSERVE. Monitor mode now records the `reactivation` audit row + increments the counter (dashboard signal for the calibrating parent) but skips voice + parent push (the kid hears nothing during calibration).
- **F-F** — `reassert()` computes the policy decision ONCE at the top and gates BOTH the AdGuard drift-heal AND the Apple TV watchdog. Pre-fix the drift-heal would fire `_enter_enforcing()` every 60 s in monitor mode + `enable_adguard_block=True` (narrow repro condition; v0.15.7's `_is_blocked=True` shortcut for AdGuard-disabled-enforced already covered the common the owner setup).
- **F-G** — `_exit_enforcing` skips the AdGuard `set_blocked(False)` call when `_is_blocked=False` AND policy ≠ ACT. Read live state, not a flag — the adversarial reviewer flagged that a persisted-flag approach would interact badly with Cohort 4's state persistence. Defensive "user toggled enforcement_enabled OFF mid-block" path preserved (that path has `_is_blocked=True` so it still fires).
- **F-H** — `evaluate()` short-circuits to OK at the top when policy is BYPASS (paused mode). Pre-fix paused mode walked the full state machine; each tick laid down warn / grace_start / enforce_start audit rows even though side effects no-op'd.
- **F-N** — `force_block` under BYPASS logs a warning and returns without state mutation. **the owner Q2 decision: silent no-op + warning log** (vs HTTP 409). Pre-fix it left `_state=ENFORCING` + `_is_blocked=False`, drift-spamming every 60 s.

### Cohort 3 — Theme B: close-reopen defeat + visibility (F-D, F-I, F-L, F-P)

The `current_group=None` shape Gap B exposed had two more failure modes adjacent to the full-exhaustion case v0.16.5 handles.

- **F-D** — WARN/GRACE latch. When state is WARN/GRACE with `_enforce_reason` starting `"group:"` AND `current_group=None` on a tick AND the latched group is NOT fully exhausted (v0.16.5 pin has stricter precedence), promote `current_group` to the latched group so the binding block picks it up. The kid closing an app briefly mid-grace no longer relaxes state to OK; reopening continues from WARN territory, not a fresh OK→WARN→GRACE walk. ~75 s/cycle defeat closed. Latch clears naturally on group change or midnight rollover.
- **F-I** — snapshot `_enforce_reason` at the top of `evaluate()` (before any binding mutation), thread through `_apply` → `_emit_state_event` → bus event payload. Pre-fix relax-to-OK transitions emitted `reason=None`, losing the original attribution ("quiet:Bedtime", "group:movies"). The adversarial reviewer generalised this beyond Sonnet's single-case fix to cover all binding-reason mutation sites.
- **F-L** — new audit rows: `warn_cleared` (WARN→OK) and `grace_cleared` (GRACE→OK), both carrying the prev_reason attribution from F-I. Pre-fix the dashboard timeline read "grace started" with no terminator on close-reopen patterns. (Panel addon renders the new action names with default styling; semantic clarity wins over unstyled-but-clean.)
- **F-P** — removed the unreachable `state in (STATE_OK, STATE_WARNING)` arm from `audit.py`'s `enforce_end` branch. The state machine only transitions ENFORCING → OK (never ENFORCING → WARNING per state.py:37-38); the WARNING arm was dead code that misled future engineers.

### Cohort 4 — Theme C: enforce-cycle state persistence (F-E)

The riskiest cohort: new storage schema. Pre-fix every HA restart reset `_state=OK` / `_grace_started_at=None` / `_reactivation_count=0`. A kid past their daily budget could earn ~75 s of free TV per restart because the post-restart `evaluate()` walked a fresh OK→WARN→GRACE cycle. Worst case for the owner's setup (v0.15.5 default `enable_adguard_block=False`) which disabled `seed_from_adguard` — the only other recovery path.

- **RuntimeState dataclass** in `storage.py` (state, grace_started_at, enforce_reason, reactivation_count, was_apple_tv_active_last_tick, last_enforcement_failed, updated_at) round-trips through JSON; defaults all fields so a missing-key dict from older dumps decodes cleanly.
- **AppleTVMgmtStore** gains `get_runtime_state` / `set_runtime_state` + load/save under the new `runtime_states` JSON key (optional; absent on old dumps → empty dict; safe migration).
- **EnforcementController** gains `seed_from_runtime_state()` (called BEFORE `seed_from_adguard` in coordinator startup — runtime state is now the authoritative recovery source) + `_persist_runtime_state()` (called after every state transition in `_apply` + on every reactivation count increment).
- **Adversarial-required sanity check**: if restored `_grace_started_at` is ≥ 2× `grace_seconds` old (HA was down for a long time), drop to ENFORCING rather than risk a negative grace delta in `state.compute_next_state`.
- **Per the owner Q2**: separate runtime_state store (cleaner migration than extending the Profile dataclass).

### Cohort 5 — Theme D: sync drift (F-K, F-M, F-J; F-O deferred)

Three "two implementations of one contract" gaps where REST and HA-service paths or schemas had silently diverged.

- **F-K** — extension grant shared helper. `audit.record_extension_granted` consolidates audit row + positive-minutes voice; both REST POST /extension AND `appletv_mgmt.grant_extension` HA service now call it. Pre-fix the service was silent (no voice, no audit row).
- **F-M** — `/requests/{id}/decide` enforces ±240 minutes cap matching POST /extension. **Breaking-behaviour change**: pre-v0.17.0 `minutes=-5` was silently clamped to 0; post-v0.17.0 it's a 422.
- **F-J** — **the owner Q3 decision: auto-generate OpenAPI from validator**. New module-level `LIMITS_PATCH_FIELDS` dict at api.py is the single source of truth for `_build_openapi`'s PATCH schema. Drift-detection test (`test_v017_openapi_limits_patch_integer_constraints_match_validator`) probes 4 fields' min/max bounds through both the OpenAPI schema AND `_validate_field`, asserting equivalence. The adversarial reviewer flagged the specific failure mode this catches: "validator gets max=900 added, OpenAPI still says max=600".
- **F-O** — DEFERRED per adversarial review (not reachable through current code paths; refactor adds hot-path I/O without a concrete bug).

### Tests

| File | Δ | Total |
|---|---|---|
| test_reactivation.py | +4 | (F-B + F-C) |
| test_enforcement_failed.py | +7 | (F-F + F-G + F-N) |
| test_group_exhaustion_persistence.py | +5 | (F-D) |
| test_runtime_state.py (new) | +10 | (F-E + sanity check) |
| test_api_validators.py | +3 | (F-J + F-M) |
| **TOTAL** | **+29** | **346 passing** (was 318) |

### Migration / compatibility notes

- **Storage schema**: new `runtime_states` JSON key under `appletv_mgmt_data`. Pre-v0.17.0 dumps simply have no entry → controllers boot with constructor defaults (same as pre-v0.17.0 behavior). Forward-compatible; no migration script needed.
- **Bus event payload**: `EVENT_ENFORCEMENT_CHANGED` now includes a new `prev_reason` field. Listeners that pre-date v0.17.0 simply ignore the field; audit.py's listener uses it for F-L. Forward-compatible.
- **`/requests/{id}/decide` breaking change**: negative `minutes` now 422 instead of silently clamped to 0. Documented.
- **`appletv_mgmt.grant_extension` service**: now fires the extension voice for positive minutes. Parents who scripted around the previous silence should be aware (no failure mode, just an audible difference).

---

## [0.16.5] — 2026-05-30

Anti-defeat for group-budget enforcement (Gap B from the v0.16.3 hardware-in-the-loop test on 2026-05-30 13:11–13:19). When a group budget triggers `ENFORCING` and the kid stops watching (or pyatv's turn_off succeeds), `current_group` becomes `None`, daily binding takes over, and state relaxed to `OK` → `enforce_end` fired → `_reactivation_count` reset to 0. Restarting the Apple TV walked a fresh `WARN → GRACE → ENFORCING` cycle; no inactive→active edge was detected while state=ENFORCING, so the reactivation voices (friendly + stern) and the parent push never fired.

### Fixed

- **Group-exhaustion pin** in `EnforcementController.evaluate()`. Two new optional kwargs — `group_totals_seconds` and `group_budgets_seconds` (full-table view of today's usage) — let the evaluator detect "any group is at-or-over its budget today". When the kid isn't actively in a non-exhausted group, the evaluator pins `binding_reason = "group:{name}"` + `effective_used = budget_s`, keeping the state machine at `ENFORCING` across the off-period. The reactivation cycle now sees the on→off→on transition while still under ENFORCING and the friendly + stern voices fire as designed.
- **`coordinator._async_update_data`** now passes the full per-group totals + budgets snapshot to `evaluate()`. Was previously only passing the *current* group's row.

### Behaviour matrix

| Scenario | Pre-v0.16.5 | v0.16.5 |
|---|---|---|
| Group exhausted, kid stops watching | state relaxes to OK | state stays ENFORCING ✓ |
| Group exhausted, kid switches to a different group with remaining budget | state relaxes to OK | state follows the new group (OK) ✓ |
| Group exhausted, kid lands on an unbudgeted "other" group | state relaxes to OK | state pins to ENFORCING (group:<exhausted>) ✓ |
| No exhausted group, kid stops watching | state relaxes to OK | state relaxes to OK ✓ (back-compat) |
| All evaluate() callers without the new kwargs | n/a | back-compat: no pin, identical to v0.16.4 |

### Tests

- New `tests/test_group_exhaustion_persistence.py` — 9 tests covering the matrix above plus midnight-rollover-clears-latch + repeated-tick-stability + reactivation-counter-survival.
- 318 tests passing (was 309).

### Live verification

- REST state-injection technique (`POST /api/states/media_player.heimkinoaaa state=playing app_id=com.netflix.Netflix`) — same approach proven for v0.16.3 + v0.16.4.
- Drop `group_budgets.movies` past current usage → kid in `ENFORCING` → standby-inject the media_player → assert state stays `ENFORCING` (pre-fix: relaxes to OK).

---

## [0.16.4] — 2026-05-30

Live test 2026-05-30 13:11–13:14: monitor_only mode with `warn_in_monitor_mode=True`. Warn voice fired at 13:11:50 ✅, then 13:13:03 `grace_start` audit row ✅, but **no `voice_announcement reason=countdown` row at 13:13:33** — the 30 s "Achtung! Noch 30 Sekunden" cue was silently dropped. `enforce_start` followed at 13:14:33. Compare the 13:11 enforced-mode cycle earlier the same day: countdown row present at 19:41:09 local.

Root cause: `EnforcementController._fire_countdown_now` (enforcer.py:1007–1013) gated on `decision.kind != "ACT"`. In monitor_only mode the decision is `OBSERVE`, so the HA timer fired into a no-op. The warn voice — which uses `audit._voice_allowed_for(profile, "warning")` — already respects `warn_in_monitor_mode`. The countdown didn't.

### Fixed

- **Countdown gate** routes through the same `warn_in_monitor_mode` policy the warn voice uses. New `EnforcementController._countdown_voice_allowed(decision)` helper mirrors `audit._voice_allowed_for(..., "countdown")`. Applied to both the production timer path (`_fire_countdown_now`) and the legacy tick path (`_maybe_fire_countdown`).
- **`policy.voice_allowed`** recognises a new `"countdown"` trigger; under OBSERVE it returns `True` (caller still gates on the flag), same as `"warning"`. ACT speaks unconditionally, BYPASS stays silent.
- **`audit._voice_allowed_for`** extends its OBSERVE branch from `trigger == "warning"` to `trigger in ("warning", "countdown")` so the helper is reusable by future callers.

### Tests

- `tests/test_countdown_voice.py` — new `test_countdown_fires_under_monitor_only_with_warn_opt_in` (legacy tick path) plus three timer-path tests (`test_fire_countdown_now_fires_under_monitor_only_with_warn_opt_in`, `..._silent_under_monitor_only_default`, `..._silent_under_adult_mode`) that exercise `_fire_countdown_now` directly. Existing `test_countdown_silent_under_monitor_only_mode` docstring updated to reflect the new opt-in semantics.
- 309 tests passing (was 305).

### Design notes

Considered adding a sibling `countdown_in_monitor_mode` flag instead of reusing `warn_in_monitor_mode`. Reused the existing flag: both warnings and the countdown cue are "heads-up before nothing happens" voices used together for threshold calibration — a parent who wants one wants the other. An extra knob would only let them be configured incoherently.

---

## [0.16.3] — 2026-05-29

Live test 2026-05-29 19:35: kid watching Netflix (group `movies`, 60-min budget) hit the 5-min warn threshold; warn voice fired and announced **"Achtung! Noch 145 Minuten Bildschirmzeit übrig"** — the *daily* remaining (200 - 55 = 145), not the *group* remaining (60 - 55 = 5). Five minutes later the group hit 0 → `grace_start` → "Noch 30 Sekunden" countdown → enforce. From the kid's point of view: "145 minutes" then suddenly "30 seconds" 5 minutes later — incoherent.

Two engines were operating on different numbers: `enforcer.evaluate()` correctly scaled `effective_used` so the state machine tripped at the right moment, but `audit._maybe_speak()` for the warn voice substituted raw daily remaining into `{minutes}` with zero awareness that the group was what triggered the WARN. The `warn` audit row also carried `reason=None` (pre-v0.16.3 the binding-constraint reason was only set when fully exhausted at GRACE entry, not at WARN entry).

### Fixed

- **Binding-constraint awareness** — `enforcer.evaluate()` now tracks `binding_reason` and `binding_remaining_s` explicitly. `_enforce_reason` is set from the WARN boundary onwards (was: only at exhaustion). `_effective_remaining_s` is set unconditionally and exposed via the new `effective_remaining_seconds` property.
- **Coordinator snapshot** now includes `effective_remaining_seconds` — the binding constraint's remaining (group OR daily, whichever is smaller; 0 under a quiet window).
- **`audit._remaining_min`** prefers `effective_remaining_seconds` over raw `remaining_seconds_today` when computing the `{minutes}` substitution for warn voice. Negative values clamped to 0.

### Tests

- New `tests/test_binding_constraint.py` (7 tests): the exact live scenario (group movies @ 5 min remaining, daily @ 145 min) — voice now reads group's 5 min; daily-only binding still sets `reason=daily_limit` at WARN; OK state keeps `reason=None`; quiet window forces `effective_remaining=0` + `reason=quiet:<label>`; adult mode keeps `reason=None`.
- Pre-existing 298 tests still passing (305 total).

---

## [0.16.2] — 2026-05-28

Four surgical fixes from the Opus + Sonnet BA audit after the v0.16.1 live test ("WTF.. it switched off without a warning.. no Grace"):

- **Persistence of v0.16.0 fields** (`countdown_message`, `reactivation_message_*`, `notify_parent_target`) across HA restarts — selective-merge in `async_setup_entry` was missing the new attributes, so every restart silently wiped them back to `""`.
- **State machine** — fresh `OK` with `remaining ≤ 0` now passes through `WARNING` for one tick before `GRACE` (was direct `OK→GRACE`, so the warn voice never fired when the group budget was exhausted at app-start or overshot in a single tick).
- **`watch_reactivation` race** — coordinator's reassert tick was calling `watch_reactivation` outside the controller lock during `_enter_enforcing`'s ~9s await window, producing a phantom "reactivation #1" on every enforce_start. Now serialized via `self._lock`.
- **GRACE audit row** — `STATE_GRACE` transitions now produce an audit row so the dashboard surfaces the grace window. Was previously invisible.

---

## [0.16.1] — 2026-05-28

Hotfix: seed `_was_apple_tv_active_last_tick` correctly in `_enter_enforcing` (was always False before, causing the first watch_reactivation tick after enforce_start to trip the False→True edge and fire a phantom "reactivation #1"). Also switches countdown firing from tick-based detection to a one-shot HA timer scheduled at GRACE entry.

---

## [0.16.0] — 2026-05-28

Three new anti-defeat features driven by the owner's live test on 2026-05-28: kid hit budget, `enforce_start` fired, both TVs went off for ~70s. Kid physically turned Samsung back on + restarted the Apple TV → watching resumed. The integration kept retrying pyatv (the v0.15.4 watchdog) but had no social-pressure intervention. This release closes that gap.

### Added

- **Countdown voice (`countdown_message`)** — fires once when remaining time drops to ≤ 35s while the state machine is in WARNING or GRACE. Comes AFTER the existing `warn_thresholds_min` voices (5 min, 2 min, …) as the final "wrap it up NOW" cue before enforce. Single-fire latch per cycle, reset on transition back to OK or on `_exit_enforcing`. Silent under adult_mode / paused / monitor_only.
- **Re-on detection (`reactivation_message_friendly` / `reactivation_message_stern`)** — edge-detects Apple TV inactive→active transitions while in ENFORCING. 1st event of the cycle plays the friendly voice; 2nd+ plays the stern voice **and** triggers the parent push (next bullet). The count resets in `_exit_enforcing` so each ENFORCING cycle starts at "first re-on". Empty templates still count + audit-log (you can opt out of voice while keeping the dashboard counter).
- **Parent notification (`notify_parent_target`)** — on the 2nd+ re-on of a cycle, fires `notify.<target>` with `title="<profile>: TV defeat"` and a message describing the attempt. Empty target falls back to `notify.notify` (HA's fanout to all configured notification services). Service-call failures are swallowed (a missing notify service must not block the reactivation handler).

### Profile additions (all default empty/opt-in)

| Field | Type | Default | Effect when empty |
|---|---|---|---|
| `countdown_message` | str (max 500) | `""` | no 30s warning voice |
| `reactivation_message_friendly` | str (max 500) | `""` | no voice on 1st re-on (still counts + audits) |
| `reactivation_message_stern` | str (max 500) | `""` | no voice on 2nd+ re-on (still counts + audits + pushes parent) |
| `notify_parent_target` | str (max 100) | `""` | falls back to `notify.notify` (HA default fanout) |

### REST

- `PATCH /limits` accepts all 4 new fields.
- `GET /limits` surfaces them in the payload alongside the existing `warning_message` / `enforce_message` / `extension_message`.

### New audit actions

- `voice_announcement` with `reason=countdown` — the 30s pre-enforce cue.
- `voice_announcement` with `reason=reactivation_friendly` / `reason=reactivation_stern` — the social-pressure voices.
- `reactivation` — every detected re-on, `detail="#N"`, `actor=system`. Independent of voice so the dashboard can tally attempts even when voice is muted.
- `parent_notified` — every parent push, `detail="#N via notify.<service>"` (or `… (failed)` if the service call raised), `actor=system`.

### Tests

- 19 new in Phase A (storage round-trip + REST validators).
- 14 new in Phase B (countdown voice — happy path, state gates, latch, bypass, reset).
- 20 new in Phase C+D (re-on detection happy path, transition edges, gates, latch, parent push routing, failure swallow).
- **281 → 334 total**.

### Suggested PATCH for the owner's install

All four fields default to empty so existing v0.15.9 users don't get surprise voice on upgrade. To opt in with the suggested German wording:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{
  "countdown_message": "Achtung! Noch 30 Sekunden Bildschirmzeit.",
  "reactivation_message_friendly": "Bildschirmzeit ist vorbei. Apple TV bitte aus lassen.",
  "reactivation_message_stern": "Apple TV bleibt aus. Die Eltern wurden jetzt informiert.",
  "notify_parent_target": "mobile_app_your_phone"
}' http://homeassistant.local:8123/api/appletv_mgmt/profiles/<id>/limits
```

Leave `notify_parent_target` blank to use HA's default fanout (`notify.notify`) instead of a single device target.

### Migration

Loading a v0.15.x Profile that lacks any of the new keys → all four default to `""`. No silent behavior change for existing users. The 4 new audit actions land only when one of the new triggers fires, so old action queries (`enforce_start` / `warn` / `extension`) are unaffected.

### Carry-over from Unreleased

The two voice triggers shipped + wired in v0.15.6 (`adult_mode_on_message`, `mode_change_message`) remain unchanged. v0.16.0 only adds the four new fields above.

---

## [0.15.6] — 2026-05-25

Wires the two voice triggers that shipped in v0.15.0 but were never connected to callers — `adult_mode_on_message` + `mode_change_message`. Closes the loop on the spec §3.7 voice rules table.

### Added

- **`fire_mode_change_voice(hass, profile, *, old_mode, new_mode)`** in voice_notifier.py — gating: `voice_on_mode_change=True` AND template set AND `old_mode != new_mode`. Substitutes `{old_mode}` / `{new_mode}` placeholders. Records a `voice_announcement` audit row on success.
- **`fire_adult_mode_on_voice(hass, profile)`** — gating: `adult_mode_on_message` set AND `mode != paused` (per spec §3.7: "paused = be quiet, including for adult-mode-on chime"). Audit row on success.
- **Switch-driven `adult_mode_on/off` audit rows** — `AdultModeSwitch.async_turn_on/off` now record their own audit rows (previously only the REST POST emitted; switch toggle was silent).

### Wired into 5 call sites

`select.async_select_option` · `EnforcementEnabledSwitch._set_mode` · `AdultModeSwitch.async_turn_on` · `PATCH /limits` dedup mode_changed · `POST /adult_mode` REST view. All fired via `hass.async_create_task` so the toggle/REST handler returns immediately; voice runs in background.

### Tests

6 new in `test_voice_notifier.py` (now 240 total). Cover: opt-in/opt-out gating, no-op-on-same-value, paused-mode silence, empty-template silence.

### To enable on your install

Both off by default. Opt in via PATCH /limits:
```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{
  "voice_on_mode_change": true,
  "mode_change_message": "Modus von {old_mode} auf {new_mode} umgestellt",
  "adult_mode_on_message": "Erwachsenenmodus aktiv"
}' http://homeassistant.local:8123/api/appletv_mgmt/profiles/<id>/limits
```

---

## [0.15.5] — 2026-05-25

AdGuard blocking is now **opt-in** (default OFF). the owner's setup uses Samsung TV (via the [samsungtv_encrypted](https://github.com/sermayoral/ha-samsungtv-encrypted) integration — documented in [jarvis2k1/SmartTV-Tooling](https://github.com/jarvis2k1/SmartTV-Tooling)) as the primary kill switch, plus the v0.15.4 pyatv watchdog. AdGuard's DNS-blocking role is supplementary, not essential — and an unnecessary dependency for installs without an AdGuard addon.

### Added

- **`Profile.enable_adguard_block: bool = False`** — new field. Was effectively always-on through v0.15.4; explicit opt-in from v0.15.5+.
- **`PATCH /limits` accepts `enable_adguard_block: bool`** — opt back in anytime.
- **`enable_adguard_block` surfaced in `/limits` GET payload** so panel + Lovelace card + REST consumers can render the current state.

### Changed

- **`_enter_enforcing` skips `AdGuard.set_blocked(True)`** when disabled; `adguard_ok` defaults to True for the failure-flag math (we don't count "chose not to call" as failure).
- **`_exit_enforcing` skips `set_blocked(False)`** unless AdGuard was enabled OR we still have stale `_is_blocked=True` from a mid-session toggle (defensive cleanup).
- **`seed_from_adguard` no-ops when disabled** — no startup network call to the AdGuard proxy for nothing.

### Migration

Existing installs upgrade transparently: stored Profile JSON won't have the new field → `Profile.from_dict` defaults to `False` → AdGuard stops being called from upgrade onward. **This is a behavior change** — your AppleTV will no longer have DNS-level blocking unless you opt back in. The TV-off enforcement (pyatv + Samsung) is unchanged.

### Test count

231 → **234** (+3 for the gating tests).

---

## [0.15.4] — 2026-05-25

Parallel turn_off + Apple TV watchdog under ENFORCING. the owner's response to the smart-plug suggestion: "kid would just unplug" — fair. So instead of new hardware, make `pyatv` and Samsung work better together.

### Added

- **Parallel turn_off via `asyncio.gather`** — `_enter_enforcing` fires pyatv (Apple TV) + Samsung TV (via samsungtv_encrypted) concurrently. Was sequential before — Samsung waited up to ~25s for pyatv to fail-and-retry before the user-visible screen went dark. Now ~12s max parallel.
- **Apple TV watchdog** in `reassert()` (which the coordinator already calls every tick) — if state=ENFORCING + Apple TV is in an ACTIVE_MEDIA_STATE (playing/on/idle), retry just the pyatv `turn_off`. Catches the chronic v0.14.x pattern where pyatv's Companion protocol drops silently → Apple TV stays on → kid could resume watching when Samsung TV comes back up.
  - Does NOT re-fire the full `_enter_enforcing` (preserves v0.15.2 fix: no audit-row spam, no re-block AdGuard, no voice spam).
  - When Apple TV is already idle/off → true no-op.
  - Respects mid-cycle bypass: if user toggled adult_mode / monitor_only / paused, `_should_act_now()` returns OBSERVE/BYPASS and the watchdog exits.

### Tests

4 new in `test_enforcement_failed.py` (now 231 total):
- `test_v0_15_4_parallel_turn_off_when_both_targets_configured`
- `test_v0_15_4_watchdog_retries_pyatv_when_apple_tv_still_active`
- `test_v0_15_4_watchdog_silent_when_apple_tv_already_idle` (locks in v0.15.2 quiet behavior)
- `test_v0_15_4_watchdog_skipped_under_bypass`

### Live impact

Coverage matrix after this release:

| Scenario | Pre-v0.15.4 | v0.15.4+ |
|---|---|---|
| Kid hits budget, both targets work | ✅ ~3s | ✅ ~3s |
| pyatv fails, Samsung works | ⏱️ 25s for screen dark | ✅ 3s (parallel) |
| pyatv keeps failing, kid resumes via Samsung | ❌ Resumes | ✅ Watchdog retried until success |
| Kid paused, walks away | ✅ No spam | ✅ No spam (locked in by test) |
| User adult-modes mid-cycle | ✅ Bypass respected next tick | ✅ Respected immediately |

---

## [0.15.3] — 2026-05-25

Third same-day hotfix. Live UX feedback: the owner's device card still showed 3 switches (Apple TV Mgmt, Adult mode, Shut down TV on enforcement) that look largely redundant — exactly the cross-wiring perception the v0.15 redesign was supposed to fix. The legacy switches were set `default_disabled=True` in v0.15.0 (Phase C), but HA's entity registry **preserves the user's existing-enabled state** from v0.14.x, so existing installs kept showing them.

### Fixed

- **`_maybe_hide_legacy_switches`** runs ONCE per install on first load after v0.15.3. Calls `entity_registry.async_update_entity(<legacy>, disabled_by=INTEGRATION)` for the two legacy switches. Marked complete via `entry.options["legacy_cleanup_done"] = "0.15.3"` so it never repeats — if the user later re-enables them via Settings → Devices → Apple TV Mgmt → Entities (for automations referencing the legacy entity_ids), the cleanup does not override that choice on next load.

After v0.15.3 the device card surfaces exactly one canonical control per concept:

| Concept | Single canonical control |
|---|---|
| Mode (enforced / monitor_only / paused) | `select.<profile>_mode` (dropdown) |
| Adult-mode bypass | `switch.<profile>_adult_mode` |
| TV-shutdown target | configured via PATCH /limits, panel mode pill, or options flow |

Legacy switches still exist (re-enable via UI if you have automations). Just hidden by default.

### Test count

227 (no new tests — the registry-update is a one-shot UI cleanup and the HA registry API shape is version-specific; covered by manual verification on the owner's live install).

---

## [0.15.2] — 2026-05-25

Same-day follow-up to v0.15.1. While diagnosing the voice-spam under load on the owner's live install, found a second issue: the enforcer was re-firing the full `_enter_enforcing` (AdGuard block + media_player turn_off chain + voice attempt) on **every** coordinator tick (~30s) while the state stayed in ENFORCING — not just on the transition INTO it. The coordinator already calls `reassert()` separately for AdGuard drift, so the re-fire was redundant; it just produced a continuous stream of `enforce_turn_off_failed` audit rows (one per tick) because pyatv companion is chronically unreliable on the owner's Apple TV (the v0.14.x issue).

### Fixed

- **`_apply` only calls `_enter_enforcing` on the transition into ENFORCING.** Previously fired every tick while staying in ENFORCING. Cut audit-log noise from "every 30s" to "once per state cycle" (typically once per day when the kid first hits budget). Coordinator's `reassert()` still handles AdGuard-drift heal per tick (cheap, idempotent).

### Added (test discipline)

- **4 regression tests** in `tests/test_enforcement_failed.py` that would have caught both v0.15.1 and v0.15.2 bugs if they had existed before:
  - `test_call_turn_off_with_verify_returns_false_when_already_inactive` — locks in the v0.15.1 fix (Samsung TV already off → return False, don't count as "successful turn_off", don't speak the voice lie).
  - `test_call_turn_off_with_verify_attempts_when_active` — counter-test: when the entity IS in an active state, we DO call the service (don't accidentally skip valid enforcement).
  - `test_lying_voice_regression_samsung_already_off` — end-to-end: pyatv broken + Samsung off → voice does NOT fire.
  - `test_apply_only_calls_enter_enforcing_on_transition_into` — this v0.15.2 fix: stay-in-ENFORCING does NOT re-fire enter.

### Live impact

Verified on the owner's install: 3 minutes after the v0.15.2 deploy, zero new audit rows (vs ~4-6 expected before the fix).

### Test count

223 → **227** (4 new).

---

## [0.15.1] — 2026-05-25

Same-day hotfix to v0.15.0. Live deploy produced voice spam every ~45 seconds on the owner's install: the kid was over-budget, the enforcer ran each coordinator tick, pyatv on `media_player.heimkinoaaa` failed (the chronic v0.14.x issue), but `media_player.turn_off` on the `tv_shutdown_target` (Samsung TV, just configured for the first time) "succeeded" trivially **because Samsung was already off**. `_call_turn_off_with_verify` saw an inactive state on the first poll and returned True, which made `any_turn_off_succeeded = True` and fired `enforce_message` every cycle — a lie (nothing changed; the screen was already dark).

This was a latent v0.14.x bug — only fired now because Samsung TV was set as `tv_shutdown_target` for the first time in this deploy. v0.15.0's `enforcing_failed` flag was correctly set, but the voice gate (predicated on `any_turn_off_succeeded` rather than "did anything actually change") didn't notice.

### Fixed

- **`_call_turn_off_with_verify` pre-checks the entity state.** If it's already in an inactive state (`off`, `standby`, `unavailable`, `unknown`, `None`), skip the service call and return False (don't count as success). The voice now only fires when at least one `turn_off` actually changed something.
- The `enforce_turn_off_failed` audit row is no longer written for the skipped path (since we didn't try); the parent sees enforcement intent in the other audit rows but doesn't get the misleading "Apple TV (heimkinoaaa) didn't go off" line for a path we deliberately skipped.

### Live impact

Voice messages from ~12/min to 0 within seconds of deploy.

---

## [0.15.0] — 2026-05-25

The business-logic redesign. Triggered by a user-reported "cross-wiring" bug: toggling the Enforcement-enabled switch appeared to enable adult mode, and vice versa. Two BA agents traced it — **no code-level cross-wiring**. Root cause: UX. `enforcement_enabled=False` and `adult_mode_active=True` produce identical observable behavior (TV keeps running) with different backing fields, and the `enforcement_enabled` translation key was missing from `en.json`.

Process: 2 BA reports + 2 rounds of QA review (Opus + Sonnet each) + PO sign-off (D1-D9) before any code was written. Spec at `/tmp/biz_logic_spec_v2.2.md` (1071 lines, locked).

### Added

- **`select.<profile>_mode`** — replaces the boolean with explicit 3-option select: `enforced` (Aktiv) / `monitor_only` (Nur beobachten) / `paused` (Pausiert). Per PO D2, monitor mode is **silent by default** — opt in via `warn_in_monitor_mode=True` for calibration use.
- **`sensor.<profile>_effective_state`** — derived 10-state sensor that collapses (mode × state-machine × adult_mode × enforcement_failed) into one user-facing label. New `enforcing_failed` state covers the case where AdGuard blocked but TV didn't turn off (or vice versa).
- **`policy.py`** module with pure `should_act(mode, adult_mode_until, now)` — single source of truth for the act/observe/bypass decision. Replaces 3-4 sites that previously had their own branching. Plus `voice_allowed(decision, trigger)` for the unified voice gate.
- **`actor` field on `ActionLogEntry`** — every audit row carries the origin (`switch_entity | select_entity | service | rest | panel | options_flow | companion | automation | voice_assist`). Set via `X-AppleTV-Mgmt-Source` request header.
- **New audit actions:** `mode_changed`, `tv_shutdown_target_changed`. Every mode toggle (switch, select, REST PATCH, options flow) emits exactly one audit row.
- **`POST /adult_mode` accepts `{until: ISO}`** alongside `{minutes: N}`. Full §3.2.1 validation: rejects TZ-naive (422), past (422), both fields provided (422), >24h future (422).
- **`PATCH /limits`** accepts `mode`, `tv_shutdown_target`, `warn_in_monitor_mode`, `voice_on_mode_change`, `adult_mode_on_message`, `mode_change_message`. Conflict detection (e.g. `{mode: monitor_only, enforcement_enabled: True}` → 422). PATCH dedup: mode + tv_shutdown_target emit dedicated audit rows, not generic `limits_changed`.
- **`/status` payload** exposes `mode`, `effective_state`, `tv_shutdown_target`, `enforcement_failed` so panel + card render the new UI without re-computing.
- **`translations/de.json`** — full DE translation file, was missing.
- **Datetime picker** for adult-mode-until in both the panel addon AND the Lovelace card (per PO D5 expanded). Client-side JS converts the HTML5 datetime-local input to UTC ISO with Z before POST.

### Changed

- **`switch.<profile>_enforcement_enabled`** is now a legacy alias for the select. Reads/writes the **stored Profile** directly — no more `entry.options` writes, no more reload-blip on toggle. Phase C: `entity_category=CONFIG, default_disabled=True`. Existing installs with the entity already enabled keep it. ON ↔ `mode="enforced"`, OFF ↔ `mode="monitor_only"`.
- **`switch.<profile>_tv_shutdown`** rewritten same way. Preserves `tv_entity_id` as memory when toggled off (per Opus QA C3 — the case "user configured Samsung TV, paused for movie night, upgraded → target lost").
- **`sensor.<profile>_enforcement_state`** moved to `entity_category=DIAGNOSTIC`, re-labelled "State machine (raw)".
- **`Profile.from_dict`** does explicit 4-way reconciliation between stored `enforcement_enabled`, stored `mode`, `entry.options`, and defaults. No silent migration surprises.
- **`async_setup_entry`** reads `mode` + `tv_shutdown_target` from the stored Profile. Other fields stay options-primary. One-shot reconciliation uses `hass.data[DOMAIN][entry.entry_id]["_suppress_next_reload"]` (NOT `entry.runtime_data` — that's HA 2024.6+).
- **`is_adult_mode_active_at(now)`** new pure store accessor. The side-effecting `is_adult_mode_active` retained for back-compat. `should_act` consumes the pure version exclusively.
- **`set_adult_mode_until`** asserts TZ-aware input on write.
- **Voice rules** unified through `policy.voice_allowed`:
  - Warning: speaks under enforced; opt-in under monitor_only (PO D2 default off); never under paused or adult_mode.
  - Enforcing: speaks only after verified turn_off + ACT mode.
  - **Extension granted: speaks regardless of mode** (PO D8 — the only universal trigger).
- **`enforcement_failed` flag** plumbed from `EnforcementController` through coordinator data to the effective_state sensor.

### Fixed

- **`audit.py` payload-key mismatch.** `api.py` fires `EVENT_LIMITS_UPDATED` with `{updated_fields: [...]}`; `audit.py` was reading `data.get("changed")`. Audit detail always showed "limits updated" instead of the actual field list. Pre-existing bug from before v0.14, surfaced by Opus QA round 2.
- **Duplicate `get_profile()` method** at `storage.py:541` and `:663`. Removed the shadow.
- **`switch.py` docstring** said "one switch per profile" — actually 3.

### Migration

**Invisible at the live level for the owner's install:**
- `enforcement_enabled=True` → `mode="enforced"` (no change).
- `tv_shutdown_enabled=True, tv_entity_id="media_player.samsung_tv"` → `tv_shutdown_target="media_player.samsung_tv"` + `tv_entity_id` preserved.
- `adult_mode_until` survives.
- Legacy switches stay in entity registry (`default_disabled`).
- Per PO D7: indefinite support for legacy PATCH keys. No removal version, no Repairs issue.

After upgrade, HA Core restart picks up the new entities. Verify:
- `curl http://homeassistant.local:8123/api/appletv_mgmt/health` → version 0.15.0
- `curl .../profiles/<id>/status` → contains `mode`, `effective_state`, `tv_shutdown_target`.

### Test count

159 (v0.14.5) → **223** (v0.15.0). New: `test_policy.py` (19), `test_storage_helpers.py` (4-way migration matrix), `test_audit_actor.py` (14), `test_init.py` (290 lines, suppress-reload flag), `test_select.py` (311 lines, no-op guard + dedup), `test_enforcement_failed.py` (282 lines), `test_effective_state.py` (19 — full §3.3 matrix).

---

## [0.14.5] — 2026-05-24

User caught the integration speaking a lie: the Sonos announced
"Apple TV wird jetzt abgeschaltet" while the same-second audit row
said "Turn-off FAILED — Apple TV didn't go off after 2 attempts."
The voice was triggered by the state transition, not by the actual
effect.

### Fixed

- The enforce_message announcement MOVED OUT of the audit recorder
  (`audit.py _on_enforcement_changed`) and into
  `EnforcementController._enter_enforcing`, where it now fires
  ONLY after at least one turn_off call has verified successful.
- Both turn_off paths count: Apple TV via pyatv AND the optional
  separate-TV via its own integration (Samsung TV, etc.). If at
  least one returns True from `_call_turn_off_with_verify`, we
  speak. If both fail → no announcement (the audit log already
  carries `enforce_turn_off_failed` so the failure is visible).
- New `_maybe_announce_enforce()` helper on the enforcer that calls
  `voice_notifier.speak()` and records the `voice_announcement`
  audit row on success.
- Warning announcements unchanged — they're heads-ups, fine to fire
  on state transition regardless of downstream success.
- `extension_message` unchanged — extensions are real actions
  regardless of turn_off state.

### Why moving the voice fixed it cleanly

The audit recorder runs synchronously with the state-change event
bus tick. Verifying turn_off success takes 4–9 seconds (1 attempt
8s timeout + 0.5s poll grid + 1s pause + retry). Asking the audit
listener to wait that long would block the event loop. Letting
the enforcer fire the voice after verification is the natural fit
— it's already awaiting the verification result.

---

## [0.14.4] — 2026-05-24

User-caught UX bug: the Sonos was announcing
"Bildschirmzeit ist vorbei. Apple TV wird jetzt abgeschaltet."
even though the Dashboard's same-second audit row said
"daily budget reached **(monitor mode — not blocked)**". The
announcement was a lie — nothing was being shut down.

### Fixed

- Audit recorder now skips the voice announcement on WARNING +
  ENFORCING state transitions when the enforcer wouldn't actually
  act. Two suppression conditions, OR'd:
  * `enforcement_enabled == False` (monitor mode)
  * `is_adult_mode_active(profile_id)` (adult mode bypass)
- Extension announcements still fire — those are real actions (the
  kid actually got more time), regardless of mode.
- Audit log entry still records `enforce_start` / `warn` so the
  state transition is visible; only the SPOKEN message gets
  suppressed.

### Live-verified

Monitor mode on (`switch.<profile>_enforcement_enabled` → off) +
`force_block` service call:
- HTTP 200
- Audit row: `enforce_start detail='daily budget reached (monitor
  mode — not blocked)'`
- NO `voice_announcement` row appended
- Sonos stayed silent

---

## [0.14.3] — 2026-05-24

User-asked defensive guard.

### Why

the owner enabled adult mode and tried to play a movie — the TV switched off
anyway, and he reported that the integration was the suspect. Live
investigation showed the integration was NOT calling turn_off (zero
calls in a 90s log tail; state was OK; AdGuard unblocked), so the TV
was being killed by something else (Samsung's own no-signal auto-off,
CEC cross-talk, or the misbehaving `samsungtv_encrypted` integration's
polling lag). But even so: any future bug that lets `_enter_enforcing`
run during adult mode would silently kill the TV again. Adding the
guard makes us bullet-proof on this axis.

### Added

- `EnforcementController` now takes the store. New first check in
  `_enter_enforcing`: if `store.is_adult_mode_active(profile_id)` is
  true, **skip every side effect** (no AdGuard call, no
  `media_player.turn_off` on the Apple TV, no TV-shutdown fallback).
  Log "ADULT MODE ACTIVE — skipping all enforcement side effects".
  State transition + audit log entry still happen so the trigger is
  visible — but the audit row's `detail` is suffixed
  `(adult mode active — not blocked)`.
- This guard fires for ANY path into `_enter_enforcing` — the normal
  state machine, `force_block()` service call, `reassert()` drift
  healing, and any future code paths.

### Live-verified

- Enabled adult mode for 60 min.
- Called `appletv_mgmt.force_block`.
- Result: HTTP 200, audit row `enforce_start detail='forced by service
  call (adult mode active — not blocked)'`, zero `media_player.turn_off`
  log lines, Samsung TV state unchanged.

---

## [0.14.2] — 2026-05-24

P0 reliability fix caught by live observation: a Disney+ session
continued playing for an hour past the budget because
`media_player.turn_off` silently failed.

### Why

pyatv's Companion protocol (the channel that handles power commands)
is intermittent. When it drops, `media_player.turn_off` on an
Apple TV no-ops without error. The enforcer was calling it with
`blocking=False`, so failures returned immediately and looked like
success — the audit log proudly said "enforce_start", AdGuard was
correctly blocking 136 services, but the kid kept watching because:
(a) DNS blocking doesn't kill in-flight TCP/QUIC streams (only stops
NEW lookups), and (b) the Apple TV sleep — the actual kill switch —
silently didn't happen.

### Fixed

- New `EnforcementController._call_turn_off_with_verify(entity_id, label)`:
  * Awaits the service call with an 8s timeout (was fire-and-forget).
  * Polls the entity state for up to 4s after; verifies it reached an
    inactive state (off / standby / unavailable / unknown).
  * Retries once with a 1s delay if the first attempt didn't take.
  * On persistent failure (both attempts), writes an audit row
    `action="enforce_turn_off_failed"` with an actionable detail
    pointing the parent at `tv_shutdown_enabled` as the fallback.
- Both Apple TV and (when configured) separate TV turn-offs go through
  the verify wrapper.
- Loud ERROR log on failure with the practical remediation hint.

### Recommended config change

When the Apple TV path is flaky, enable the TV-shutdown fallback with
a TV entity that doesn't depend on pyatv. the owner's setup now uses
`media_player.samsung_tv` (Samsung Smart TV integration) via:

    PATCH /api/appletv_mgmt/profiles/{id}/limits
    {"tv_entity_id": "media_player.samsung_tv",
     "tv_shutdown_enabled": true}

With both paths active, an enforce_start will:
1. AdGuard block (stops new lookups)
2. Apple TV turn_off via pyatv (best-effort, may fail)
3. Samsung TV turn_off via Samsung integration (reliable fallback —
   the screen going dark stops the streaming session at the source)

---

## [0.14.1] — 2026-05-24

Small follow-up on the voice-announcement pipeline (v0.13.0).

### Added

- **`Profile.extension_message`** — spoken on the configured speaker
  (e.g. Sonos) whenever extension minutes are granted. Two triggers:
  - **Manual** extension via `POST /extension` (or the Dashboard's
    +15 / +30 / +60 buttons) — fires for positive minutes only;
    negative grants (e.g. -15 to take time back) are silent.
  - **Approval** of a kid's extension request via Companion or
    `POST /requests/{id}/decide` with `approve=true` — fires when
    `granted_minutes > 0`. Denials / expiries are silent.
- Template supports `{minutes}` (the granted amount, not remaining)
  and `{app}` placeholders. Default empty = silent (opt-in).
- `/limits` exposes the field; `PATCH /limits` accepts it (max 500
  chars, same as the other message templates).
- Each successful announcement records a `voice_announcement` audit
  entry with `reason="extension"` (manual) or `reason="extension_approved"`
  (after a request) so the parent can audit later.

---

## [0.14.0] — 2026-05-24

Two new parent-facing controls.

### Added

- **Monitor mode** — new `Profile.enforcement_enabled` (default `True`).
  When toggled OFF, the integration tracks usage, fires warnings, and
  writes audit entries normally, but **does not** call AdGuard or sleep
  the Apple TV when the state machine enters ENFORCING. Useful for the
  setup phase (observe patterns before committing to limits) and for
  one-off "let it slide tonight" without changing the budget.
  - New `switch.<profile>_enforcement_enabled` entity — flip from the
    HA UI / Companion app instantly. State is persisted in the entry's
    options dict.
  - Audit log marks `enforce_start` rows with `(monitor mode — not
    blocked)` so the parent sees what *would have* happened.
  - Voice announcements still fire (the warning value is independent
    of the block being real).
  - `/limits` exposes the field; `PATCH /limits` accepts it.
  - `/status` (ProfileSummary) carries `enforcement_enabled` so the
    panel can render a "Monitor mode" banner on the Dashboard.

- **Quick extension grants on the Dashboard** (already shipped as REST
  endpoint in v0.7.0; UI was missing in the panel). See panel v0.5.2
  for the +15/+30/+60/-15 buttons.

### Tests

- **159 passing** (was 156) — 3 new for the Profile field's default,
  round-trip, and backward-compat from pre-v0.14.0 stored data.

---

## [0.13.0] — 2026-05-24

**Voice announcements over any HA media_player** (Sonos, Apple TV
speaker, Google Home, …). The integration now optionally speaks a
configurable message in the room when the budget is about to run out —
audio plays through a separate speaker so the kid's video on the
Apple TV keeps running uninterrupted.

### Why

the owner asked "can we play a video as warning?" — Apple TV `play_media`
would interrupt the kid's Netflix. Routing the warning through a Sonos
keeps the show going and gets the kid's attention with a voice that
fits the room. Discovered the owner has `media_player.dining_room` (Sonos)
+ `tts.google_translate_en_com` (multi-language) — both work via
`tts.speak` service.

### Added

- 6 new `Profile` fields, all optional (no behavior change if unset):
  - `notify_media_player_entity_id` — target speaker (e.g.
    `media_player.dining_room`)
  - `notify_tts_entity_id` — TTS engine (default: HA picks)
  - `notify_tts_language` — `de` / `en-US` / etc. (default `de`)
  - `notify_volume` — 0.0–1.0, set right before speaking (default 0.35)
  - `warning_message` — spoken on WARNING transition. Supports `{minutes}`
    and `{app}` placeholders.
  - `enforce_message` — spoken on ENFORCING start.
- New `voice_notifier.py` module — pure-ish (HA only for the service call).
  `format_message`, `should_speak`, `speak` (async).
- Wired into the audit recorder: state machine WARNING / ENFORCING
  transitions trigger TTS, and a `voice_announcement` row is added to
  the audit log on success so you can see in Recent Activity exactly
  what was said and when.
- `/limits` payload exposes all 6 new fields.
- `PATCH /limits` accepts all 6 new fields (with sane validation —
  empty defaults are fine, max 500 chars for messages, 200 for
  entity ids, 0–1 for volume).

### Notes

- Volume is set ONCE on the speaker before each announcement. We don't
  restore the previous level; Sonos and most players retain it for the
  next track. Configure once, forget.
- `tts.speak` is fire-and-forget (`blocking=False`) — we don't await
  the audio finishing. The TTS engine handles its own queue.
- No notification debouncing yet. The state machine's own grace logic
  means a WARNING fires at most once per natural transition (every 5
  min the threshold is hit), and ENFORCING fires once per enter. We'll
  add debouncing if real use shows over-fire.

### Tests

- **156 passing** (was 146) — 10 new in `test_voice_notifier.py`:
  format_message substitution (minutes / app / both / missing / empty
  / whitespace), should_speak gating (both required, neither, target
  only, message only).

---

## [0.12.2] — 2026-05-24

Visibility-focused release. User asked "Netflix has been running since
10:00, why is that not shown?" — the integration was correctly counting
only actual playback (not Netflix navigation), but the panel didn't
surface *when* the current session started, and the audit log didn't
capture app launches.

### Added

- **Current-session fields in the coordinator snapshot** —
  `current_session_started_at` (ISO timestamp of the open UsageEvent)
  and `current_session_duration_s` (live tick-to-tick).
- **`current_session_started_at` + `current_session_duration_min`** in
  the `/status` REST response (and the OpenAPI ProfileSummary stays
  forward-compatible with these new optional fields).
- **`app_started` / `app_ended` action types** in the audit log. The
  recorder now subscribes to the existing `EVENT_APP_STARTED` /
  `EVENT_APP_ENDED` bus events and writes one entry per launch and
  close. "Netflix opened at 10:13" is now visible in the Dashboard's
  Recent Activity feed.

### Notes

- We deliberately did NOT add "count menu-navigation time as the app
  in use." pyatv reports `media_state=idle` with no `app_id` while
  the user is browsing inside an app — there's no signal we can use,
  and counting it would re-introduce the kind of phantom accumulation
  v0.12.1 fixed. The 13-min Netflix navigation gap on 05-24 stays
  uncounted; the session-start display makes the gap obvious to the
  parent instead of hidden.

---

## [0.12.1] — 2026-05-23

P0 hotfix on top of 0.12.0. Caught by the new audit log: user reported
"stats look wrong, we weren't home today" and the Dashboard showed
1138 / 60 min used with one giant 19-hour open event.

### Fixed

- **Phantom-idle attribution.** `_effective_bundle_id()` was returning
  the `"unknown"` sentinel when `media_state == "idle"` (or `"on"`) with
  no `app_id` and no recent last-known app. That opened a usage event
  that ran for as long as the Apple TV stayed on the home screen /
  screensaver, with no way to close itself short of a real `off`
  transition. Result on 2026-05-23: a 02:16 wake-up (CEC pulse / Remote
  app poll / whatever) opened an event that ran 19 hours, blew the
  budget, put the integration into ENFORCING all day even though
  nobody was home.
- The fix: `idle` / `on` with no current/recent app context now returns
  `None` — time is NOT attributed. The `"unknown"` fallback only fires
  for `playing` / `paused` / `buffering` (states where some app is
  clearly consuming audio/video — typically a game pyatv can't read).

### Refactored

- Extracted `_effective_bundle_id` from `coordinator.py` into a new
  pure module `media_attribution.py` so the rule can be unit-tested
  without HA imports. Coordinator method is now a 6-line wrapper.

### Tests

- **146 passing** (was 125) — 21 new in `test_media_attribution.py`
  covering inactive states, active+app_id, last-known grace inside/
  outside window, idle without app (the regression), playing without
  app_id (game case), and grace edge cases (zero, negative).

---

## [0.12.0] — 2026-05-23

System-action audit log. Data-driven release — built directly from
patterns observed in the first week of real-world use.

### Why

Reviewing 2026-05-22's data showed two problems no surface in the
panel addressed:

1. **The morning's enforcement was invisible by evening.** At 09:15 the
   integration cut a Netflix session because the daily 60-min budget was
   blown (52 min Netflix + 8 min game). By the time anyone looked at
   the Dashboard the state machine was back to OK, with no trace of the
   block. The Lovelace history card shows app usage but not "what did
   the integration do?".
2. **A 30-minute standoff (09:15–09:44) produced ~30 tiny app-event
   fragments** as the kid kept reopening Netflix and the enforcer kept
   sleeping the Apple TV every 30s. These polluted Top Apps and buried
   the real story ("repeated bypass attempts").

### Added

- New `ActionLogEntry` dataclass + persistence under `action_log` in
  the store, with 90-day retention to match event retention.
- New `storage.record_action()` with **bypass coalescing**: 3+
  `enforce_start` entries for the same profile within 5 min are replaced
  by a single `bypass_attempt` entry carrying the total count. The
  log stays readable; the count tells the parent "kid tried to bypass
  N times in this window."
- New `audit.py` module: bus-event listener (one per profile) that
  persists ENFORCE/RELEASE/WARN entries from `EVENT_ENFORCEMENT_CHANGED`,
  decisions from `EVENT_REQUEST_DECIDED`, and config writes from
  `EVENT_LIMITS_UPDATED`. Decoupled from the enforcer — no new
  responsibilities for the state machine.
- New REST endpoint: `GET /profiles/{id}/actions?limit=N&from=&to=`.
  Newest-first, default limit 50, capped at 500.
- `EVENT_ENFORCEMENT_CHANGED` payload now includes `prev_state` and
  `reason` so consumers (and the recorder) can render meaningful entries
  without inspecting controller internals.
- `record_admin_action()` helper for REST views to drop in entries for
  adult-mode toggles + manual extensions (which don't fire bus events).
- OpenAPI updated.

### Changed

- `EnforcementController.force_block()` now sets `enforce_reason="force_block"`
  if no other reason is set, so the audit log row is meaningful.
- `unblock()` now clears `enforce_reason`.

### Tests

- **125 passing** (was 116) — 9 new tests for the action log: append,
  unique ids, bypass coalescing across thresholds, window expiry, no
  cross-profile coalescing, since/until filtering, prune, round-trip.

---

## [0.11.1] — 2026-05-18

P0 hotfix on top of 0.11.0.

### Fixed

- `EnforcementController.seed_from_adguard()` now catches every exception, not just `AdGuardError`. When the AdGuard proxy addon was still booting at HA cold-start, `aiohttp.ClientConnectorError` propagated out of `async_setup_entry`, preventing the integration from loading at all (REST views unregistered → 404 on `/health`). The periodic reassert tick still heals the seed once AdGuard becomes reachable.

### Changed

- `_profile_summary` now derives `effective_budget_today_min` from the snapshot's `today_budget_min` (the weekday-resolved base) before adding extensions, so on days with a per-weekday override the value correctly reflects override + extension. Added an explicit `base_budget_today_min` field.

---

## [0.11.0] — 2026-05-18

Per-weekday parental controls — "weekend is different" + "school vs. holiday".

### Added

- New `Profile.weekday_budgets_min`, `weekday_group_budgets_min`, `weekday_quiet_windows` — per-day-of-week overrides for the daily budget, per-group budgets, and quiet windows. Keys: `mon`/`tue`/`wed`/`thu`/`fri`/`sat`/`sun`. Empty defaults preserve prior behavior exactly.
- New pure module `schedule.py` resolves "today's effective values" — base values when no override, per-day overrides when set. 31 unit tests.
- `GET /profiles/{id}/limits` now returns the new fields plus a `today` block with the resolved effective values (so consumers don't have to re-resolve).
- New `PATCH /profiles/{id}/limits` (and `POST` alias — Supervisor proxy doesn't forward PATCH) mutates any subset of profile fields with per-field validation. Unknown fields are rejected (422).
- New `EVENT_LIMITS_UPDATED` bus event fires on every successful mutation.

### Changed

- Coordinator now passes per-day effective `today_budget_min` + `today_quiet_windows_string` to the enforcer's `evaluate()` instead of using the base profile fields directly. Quiet windows are re-parsed when the string changes (i.e., once per day-boundary cross).
- `__init__.py async_setup_entry` now preserves the persisted weekday fields when reconstructing a Profile from the config entry on HA restart. Without this, PATCH-driven changes would silently reset every boot.
- Snapshot dict adds `today_budget_min` + `today_weekday`.

### Fixed

- N/A — additive release.

### Tests

- 116 passing (was 85) — 31 new in `test_schedule.py` covering weekday resolution, override merging, validation.

---

## [0.10.0] — 2026-05-17

QA review pass (Sonnet + Opus reviewers). See `REVIEW.md` for the consolidated spec.

### Fixed (P0)

- `handle_external_decision` no longer routes through the `APPROVE_HALF` action when the REST API caller supplies a specific `minutes` value. The previous path fired `EVENT_REQUEST_DECIDED` with the wrong `granted_minutes` before correcting via a delta — any automation listening for that event saw the wrong number. Rewritten to apply the decision atomically.
- `expire_old_requests` is now actually called (every coordinator tick) — previously defined but never invoked, leaving pending requests open forever.
- New `EnforcementController.seed_from_adguard()` runs at coordinator start so an HA restart while ENFORCING correctly seeds the in-memory state from AdGuard's live block state. Without this, a restart while blocked + a midnight reset could leave the kid silently locked out (or unblocked under-budget enforcement evaporated).
- New `EnforcementController.reassert()` runs every `ENFORCER_REASSERT_SECONDS` (60s) — heals any drift between desired state and AdGuard's state.
- Storage `async_load` is now resilient to per-record corruption — a single bad `UsageEvent` or `ExtensionRequest` is logged + skipped instead of crashing the entire integration on restart.

### Fixed (P1)

- Request IDs use `secrets.token_hex(4)` to avoid millisecond collisions (was `time.time()*1000`).
- `_check_api_key` uses `hmac.compare_digest` (constant-time compare).
- `RequestDecideView.post` returns 422 when `approve` is missing instead of silently treating it as `deny`.
- `enforce_reason` is `None` when adult mode bypasses (was `"adult_mode"`, which made the panel render "OK — adult_mode").
- `prune_old_requests()` clears decided requests older than `REQUEST_RETENTION_DAYS=30`. Pending requests are never pruned.
- DST-safe midnight reset: dedupes via `_last_midnight_reset_date`.

### Changed

- `_profile_summary` now includes `effective_budget_today_min` (= `budget_today_min + extension_minutes_today`). The invariant `remaining_today_min + used_today_min = effective_budget_today_min` always holds.
- `EVENT_RETENTION_DAYS` raised 30 → 90 so the panel's "quarter" analytics range has data.
- OpenAPI: parameters arrays declared for path + query params; `apiKeyAuth` security scheme added alongside `bearerAuth`; `ProfileSummary` includes the new `effective_budget_today_min` field.
- Manifest: `iot_class` bumped from `local_polling` to `local_push`; added `homeassistant: "2024.1.0"` minimum.

### Performance

- `AdGuardClient.list_known_services` caches the service list for 1 hour. Block transitions are ~50ms faster.

### Tests

- 85 passing (was 75) — added 10 covering `_safe_decode_list`, `_safe_decode_map`, `expire_old_requests`, `prune_old_requests`.

---

## [0.9.0] — 2026-05-17

- `GET /profiles/{id}/events` accepts `from=YYYY-MM-DD&to=YYYY-MM-DD` query params (window capped at 366 days).
- New: `GET /profiles/{id}/limits` returns the full Profile config in one shot.
- `storage.events_in_range()` does local-time window clipping identically to `events_today()`.

---

## [0.8.1] — 2026-05-17

### Fixed

- **"Custom element doesn't exist: appletv-mgmt-control-card"** (and the history card) in the dashboard's manual-card editor. Both cards used to bootstrap Lit by grabbing the prototype of `customElements.get('ha-panel-lovelace')`, with `HTMLElement` as the fallback. The fallback path silently returns `Element.prototype` which doesn't have `html`/`css` template tags, so `customElements.define(...)` never ran. Both cards now `import { LitElement, html, css } from 'https://unpkg.com/lit-element@4.1.1/lit-element.js?module'` — the HACS-standard pattern. The file is module-loaded by HA so native `import` works, and the result gets browser-cached after first fetch.

Also bumped the cache-busting `?v=` suffix on both registered Lovelace resources via the WS API so the user's browser actually re-fetches the new code.

---

## [0.8.0] — 2026-05-17

Phase 4 (part 1) — consolidated control card. The parent's daily-driver dashboard surface. Sidebar admin panel via a separate addon comes next session.

### Added

- **`appletv-mgmt-control-card`** — second Lovelace custom card (joins `appletv-mgmt-history-card` from 0.3.0). Single-file vanilla Lit, no build step. Shows:
  - Current state badge (`ok` / `warning` / `grace` / `enforcing`) with the active quiet-window label or enforce reason inline.
  - Per-group budget bars (Movies / TV Shows / Gaming / Other) with usage + budget + a red "over" tag when exceeded.
  - Quick actions: Adult mode toggle (with live countdown when active), ±15 min, Block now, Reset today.
  - Pending extension requests as inline cards with **Approve / Approve N/2 / Deny** buttons that hit `POST /api/appletv_mgmt/requests/{id}/decide` directly.
- Card auto-polls the REST API for pending requests every 15 seconds (configurable via `poll_requests_sec`).
- Deployed to `/config/www/community/appletv-mgmt-control-card/` and auto-registered as a Lovelace resource via WebSocket on the owner's HA — no manual click-through needed.

### Behavior

- The card prefers the per-group sensors when enabled in the entity registry; falls back to a "enable per-group sensors" hint when none are exposed.
- Adult-mode countdown is computed client-side from `switch.<…>_adult_mode`'s `until` attribute — updates every render without polling.
- All write actions go through HA's existing service / REST surfaces — no privileged data path.

### Usage

```yaml
type: custom:appletv-mgmt-control-card
profile_id: 01KRV4J2V4W6K0XMN1C01G6ENX   # required, from your config entry
title: Apple TV — Living Room              # optional
poll_requests_sec: 15                      # optional
```

### Docs

- README.md: new "UI" section pointing at both cards.
- CHANGELOG.md: this entry.

### Not in this release

- Configuration editing FROM the card (budgets, quiet windows). That requires either invasive entity calls or the panel-addon pattern. Use Settings → Devices & Services → Configure for now; or wait for the panel addon next session.

---

## [0.7.0] — 2026-05-17

Phase 3 — full REST API + extension-request flow with parent approval via HA Companion. OpenClaw on the Mac mini becomes the kid-facing voice surface; this release is what it'll talk to.

### Added

- **REST API** under `/api/appletv_mgmt/*` — 12 endpoints covering profile state, per-group totals, per-app usage, today's events, adult-mode enable/disable, direct extension grants, the kid-facing extension-request flow, and request decision. See [`docs/API.md`](docs/API.md) for the full surface.
- **OpenAPI 3.1 spec** served at `/api/appletv_mgmt/openapi.json`. Hand-rolled, compact, designed for autonomous-agent consumption (OpenClaw can introspect at startup and self-generate a calling skill).
- **Bearer-token auth** — `Authorization: Bearer <api_key>` OR `X-API-Key: <api_key>` (both accepted). Configure `api_key` in the options flow. **Leave blank to leave the API open on the LAN** (fine if HA is firewalled; not recommended otherwise).
- **`ExtensionRequest` model** in storage with full CRUD + auto-expire. Persists across HA restarts. Pruned at midnight along with the daily counters.
- **Actionable HA Companion notifications.** Kid POSTs `request_extension {minutes, reason, bundle_id?}` → integration creates the pending request → push fires on the parent's configured `notify.<service>` with **Approve <N> / Approve <N/2> / Deny** buttons. Tap → integration updates the request, grants extension minutes if approved, and triggers a coordinator refresh so the block lifts within seconds.
- **`POST /requests/{id}/decide`** — the same approval flow over REST, for cases where the Companion notification isn't desired (e.g. decisions from the future Lovelace control card or from voice via the Mac mini).
- **2 new bus events** for external consumers — `appletv_mgmt_request_created`, `appletv_mgmt_request_decided`.
- Options flow gains `api_key`, `notify_target`, and `request_expire_min` fields. Default `request_expire_min = 10` (pending requests auto-expire to `denied` after 10 minutes).

### Behavior

- The API works **with HA's existing bearer token** (since `/api/*` is HA's auth namespace) and also accepts our integration-specific `X-API-Key`. Either is sufficient — they're independent layers of defense.
- Adult mode via REST takes an optional `minutes` body field — falls back to the profile's configured `adult_mode_duration_min`.
- The `request_extension` flow degrades gracefully: if `notify_target` isn't configured, the request is still created but no push fires (the parent has to find it via REST or the future UI).
- iTunes auto-categorization from 0.6.0 already drives `current_group` in the snapshot, which the API surfaces for free.

### Tested live end-to-end

- `POST /profiles/{id}/adult_mode {"minutes": 5}` → status flips to `state=ok, adult_mode_active=true, until=...`.
- `DELETE /profiles/{id}/adult_mode` → flips back to enforcement.
- `POST /profiles/{id}/request_extension {"minutes": 10, ...}` → request created, returned with `status=pending`.
- `POST /requests/{id}/decide {"approve": true}` → request `status=approved`, `granted_minutes=10`, AdGuard block lifts within one coordinator tick.

### Docs

- SPEC.md: roadmap updated. Phase 3 ✅. Phase 4 = Lovelace control card. Phase 5 = HA Assist voice = **external** (handled by the owner's Mac mini AI agent — no code in this integration).
- docs/API.md: rewritten — full Phase 3 endpoint catalogue with example payloads + the `OpenAPI` link.
- README.md: new "REST API" section with curl examples for OpenClaw integration.

---

## [0.6.0] — 2026-05-17

The big one. Four interlocking capabilities that turn the integration from "one big daily counter" into something resembling Apple Screen Time per-category limits, plus a parent override.

### Added

- **App groups + per-group daily budgets.** Four bundled groups — `movies`, `tv_shows`, `gaming`, `other` — each with its own daily minutes budget configured in the options flow. Enforcement triggers when the CURRENT app's group runs out OR the overall daily budget runs out, whichever happens first. So the kids can keep gaming after the movies budget is spent (and vice versa), but can't grind past a category-specific limit by app-switching within it.
- **Static curated mapping** of `bundle_id → group` for ~30 top Apple TV apps (Disney+, Netflix, Prime, YouTube, Twitch, ARD/ZDF Mediathek, Plex, Jellyfin, Apple Arcade, …). Authoritative — wins over any auto-categorization.
- **Auto-categorize unknown apps via Apple's iTunes Search API.** When the integration sees a `bundle_id` it doesn't recognize, it queries `https://itunes.apple.com/lookup?bundleId=...` once, maps the returned `primaryGenreName` to a group, and caches the result in storage so the lookup happens at most once per app, ever. Network errors fail open (group=`other`).
- **Adult-mode override switch** — `switch.<profile>_adult_mode`. Flip on → ALL enforcement (budgets + groups + quiet windows) is bypassed for `adult_mode_duration_min` minutes (default 120, configurable). Auto-toggles off when the timer expires. Persists across HA restarts. Designed for the "wife is watching a movie" case where you don't want the integration cutting them off mid-scene. Logged in HA Logbook.
- **Per-group sensors** — `sensor.<profile>_<group>_time_used_today` and `sensor.<profile>_<group>_time_remaining_today` for each of the four groups (eight new sensors total). Disabled by default; enable from the entity registry for the groups you care about so the dashboard isn't cluttered with always-zero entities.
- **New snapshot fields** for the existing `today_history` flow: `current_group`, `enforce_reason` (one of `daily_limit`, `group:<name>`, `quiet:<label>`, `adult_mode`, or `None`), `adult_mode_active`, `adult_mode_until`, `group_totals_seconds`, `group_budgets_minutes`.

### Behavior

- **Adult mode is the highest-priority signal.** It overrides quiet windows AND budget exhaustion. Usage is still recorded (so the kids' counter is unaffected) but the enforcer stays in `OK` state for the duration.
- **The "current group" is what gates per-group enforcement.** If the kid is watching Disney+ (Movies group, exhausted) and switches to YouTube (TV Shows group, fine), the integration unblocks within ~30s. This is intentional — categories are independent budgets.
- **iTunes lookups never block enforcement.** They run as background tasks; the categorization for "this app" is `None` until the lookup resolves, at which point the next tick sees the cached category.

### Tests

- 17 new categorize tests (CURATED precedence, iTunes genre mapping, async happy-path + 404 + connection-error + content-type-quirk paths). Total suite: **75 passing**.

### Docs

- SPEC.md: new "App groups" section + Profile-field table extended.
- docs/API.md: snapshot fields documented; per-group sensors + adult-mode switch added to the entity table.
- README.md: new "App groups + adult mode" section with a worked example.

---

## [0.5.0] — 2026-05-17

### Added

- **Quiet windows — time-of-day enforcement.** Per profile, you can configure one or more local-time windows during which the integration forces enforcement regardless of how much daily budget remains. Format is comma-separated `HH:MM-HH:MM[:Label]` strings, e.g. `"20:30-07:00:Bedtime, 12:00-14:00:Lunch"`. Windows can cross midnight (start > end) which is interpreted as `[start, 24:00) ∪ [00:00, end)`.
- New `quiet.py` module — pure logic (zero HA imports): `QuietWindow` dataclass, `parse_windows()`, `find_active_window()`, `validate_windows_string()`. Fully unit-tested in isolation.
- New `Profile.quiet_windows: str` field — serialized as the comma-joined config-form string.
- New `CONF_QUIET_WINDOWS` field in the options flow with form-level validation (malformed input is rejected with a meaningful error key, not silently swallowed).
- New `active_quiet_window` field on the coordinator snapshot and as an attribute on `sensor.<profile>_enforcement_state`. Carries the label of the window currently in effect (or the formatted window if no label was given), or `None`.

### Behavior

- During a quiet window, the enforcer feeds the state machine `effective_used = max(used, budget)` — i.e. "treat as if budget is fully exhausted". This cascades through the **normal grace path** (`WARNING → GRACE → ENFORCING`) just like a real budget exhaustion. Matches the user's chosen "same grace as budget-exceeded" semantics.
- When a quiet window ends and remaining budget is positive, enforcement clears normally (`ENFORCING → OK`) and AdGuard unblocks.
- Extensions granted via `grant_extension` cannot override a quiet window — by design. The window IS the limit.
- Bad `quiet_windows` strings logged loudly at startup and treated as empty (fail-open rather than fail-locked-out).

### Tests

- 24 new pure-logic tests covering parsing, same-day vs crosses-midnight `contains()`, `find_active_window` precedence, edge cases (zero-length window, exact midnight boundary), and round-trip via `windows_to_string()`. Test suite: **50 passing**.

### Docs

- SPEC.md: new "Quiet windows" subsection under enforcement; Profile field table extended.
- docs/API.md: `active_quiet_window` attribute documented on the `enforcement_state` sensor.
- README.md: new section explaining the format and a couple of example automations.

---

## [0.4.0] — 2026-05-17

### Added

- **Optional TV shutdown on enforcement.** When the daily budget runs out and the integration enters ENFORCING, it can also call `media_player.turn_off` on a separately-configured TV entity (any HA media_player — Samsung, LG, Sony, generic IR, you name it). Useful when HDMI-CEC from the Apple TV doesn't reliably propagate to the TV, or when you want an explicit hard-kill.
- **New config-entry fields** (set via the options flow):
  - `tv_entity_id` — the HA `media_player.*` entity for the TV
  - `tv_shutdown_enabled` — boolean toggle (default **off**)
- **New entity** `switch.<profile>_tv_shutdown` — flip the toggle from any dashboard / automation without re-opening the options form. Writes through to `entry.options` so it survives restarts.
- Updated translations + options-flow form to surface the two new fields.

### Behavior

- **Ships disabled.** Default `tv_shutdown_enabled=False`. The capability is built but not active until the user enables it (per the explicit ask).
- When enabled: `_enter_enforcing()` does AdGuard block → Apple TV sleep → TV turn_off, in that order. Each is in its own try/except so a failure on one doesn't block the others.
- The switch entity is `available=False` until a `tv_entity_id` is configured, so accidentally enabling it without picking a TV is impossible.

### Why

the owner has a Samsung UE55HU8590V where `media_player.samsung_tv.turn_off` is verified working (per his SmartTV-Tooling repo). The Apple TV's HDMI-CEC isn't always reliable. This gives the integration a generic second-layer hard kill that works for any TV with an HA media_player integration. Built generic so anyone can plug in their own TV later.

---

## [0.3.2] — 2026-05-17

### Fixed

- **Transient apple_tv reconnects no longer fragment sessions.** The HA `apple_tv` integration's pyatv link drops + reconnects periodically during streaming (~30-60 s blips on the owner's install). Each blip used to produce a "started X / finished X" pair in the Logbook even though the user was watching one continuous movie.
- New `storage.reopen_recent_event_if_match()` — when an event would open with the same `bundle_id` as the previous closed one within `EVENT_STITCH_SECONDS` (default 60 s), reopen the closed one instead. The Logbook gets one entry, the timeline shows one session, and time during the transient blip is naturally excluded from the duration.

### Removed

- The WARNING-level `TICK ...` log added in v0.3.1 debugging. Downgraded to DEBUG and only fires when the watched entity is missing.

### Added

- `storage.prune_profiles(keep_ids)` runs on every config-entry setup so orphan profiles (from manual JSON injection during debugging) can't accumulate.

---

## [0.3.0] — 2026-05-17

### Added

- **HA Logbook integration.** Every time the active app changes the integration fires `logbook_entry` events. The built-in **Settings → Logbook** panel now shows a time-ordered "started YouTube" / "finished YouTube after 47.0 min" feed per Apple TV — no extra setup, filterable by entity.
- **New sensor `sensor.<profile>_today_history`.** State = number of distinct apps used today. Attributes:
  - `apps`: per-app totals sorted by usage `[{bundle_id, display_name, total_minutes, sessions}, ...]`
  - `events`: chronological session log clipped to today `[{bundle_id, display_name, started_at, ended_at, duration_minutes, open}, ...]`
- **Two new HA bus events** for downstream consumers (separate from `logbook_entry` so they're stable for automations):
  - `appletv_mgmt_app_started` with `{profile_id, bundle_id, display_name, started_at}`
  - `appletv_mgmt_app_ended` with `{..., ended_at, duration_seconds, duration_minutes}`
- **Friendly app-name mapping** (`APP_DISPLAY_NAMES`). YouTube/Netflix/Disney+/Prime Video/Twitch/ARD/ZDF Mediathek/Plex/Jellyfin/Spotify/Apple Music + Apple's first-party apps map to nice display names. Bundle id is used as fallback.
- **Custom Lovelace card** `appletv-mgmt-history-card.js`. Single-file vanilla Lit (no build step). Shows today's per-app totals as a bar chart + a reverse-chronological session timeline. Register as a JS module resource at `/local/community/appletv-mgmt-history-card/appletv-mgmt-history-card.js`.

### Why

the owner asked for "a UI showing what happened on Apple TV — log, when which app was active for how long." Three deliveries to cover different user preferences:

1. **HA Logbook** — zero-setup view in HA's standard UI, great for parents who don't want to touch dashboards.
2. **Sensor attributes** — usable from any HA template, Markdown card, apexcharts-card, or your own custom card.
3. **Custom card** — drop-in card with no template plumbing.

---

## [0.2.0] — 2026-05-17

### Added

- **`X-API-Key` auth mode in `AdGuardClient`.** New optional `api_key` constructor arg. When set, the client sends `X-API-Key` and skips HTTP Basic. Used to talk to the [companion proxy addon](https://github.com/jarvis2k1/ha-appletv-mgmt-adguard-proxy).
- **Config-flow field `adguard_api_key`** (optional). Three auth modes selectable by which fields the user fills in: api_key (proxy), username+password (direct + Basic), or neither (direct + no auth).
- **3 new unit tests** covering the api-key path, the no-auth header omission, and the precedence rule (api_key wins over username/password).

### Changed

- Field labels in the config-flow form now distinguish proxy URL examples from direct AdGuard examples.
- `README.md` rewritten to reflect the proxy-or-direct topology.

### Why

The HA AdGuard Home addon binds its REST API to `127.0.0.1:<random ephemeral port>` and exposes it only via HA's ingress proxy (which requires a logged-in HA session). HA Core's container cannot reach that. The companion proxy addon fixes the transport; this release teaches the integration to authenticate against it.

---

## [0.1.2] — 2026-05-17

### Fixed

- **`AttributeError: 'AdGuardClient' object has no attribute '_get_client'`** in the config flow. The method was renamed to the public `get_client` during the v0.1.0 refactor but `config_flow.py` wasn't updated, so every form submission failed with HA's generic "Unknown error occurred" toast.
- **Broader exception catching in `_validate_adguard`.** Connection timeouts (`asyncio.TimeoutError`), DNS failures (`OSError`), and anything unexpected now translate into meaningful error keys + a WARNING in the HA log, instead of surfacing as "Unknown error".

### Added

- New error key `unknown` with translation: *"Unexpected error — check HA log for the traceback."*

---

## [0.1.1] — 2026-05-17

### Added

- **AdGuard `username` / `password` fields are now optional.** Ingress-only AdGuard installations often have no HTTP authentication configured (HA's auth gates ingress instead). The previous Basic-auth header caused 400/401 responses against such installs.

### Changed

- `AdGuardClient` only adds the `Authorization: Basic` header when both `username` and `password` are non-empty.
- `manifest.json`: `documentation` and `issue_tracker` URLs corrected to point at `jarvis2k1/ha-appletv-mgmt` (previously `dasslermarc/HA-AppleTV-Mgmt` — wrong slug).
- Translation hints note that auth fields can be left blank.

### Tests

- +2 cases: no-auth header omitted, `set_blocked` works without credentials.

---

## [0.1.0] — 2026-05-17

### Added

- Initial Phase 1 MVP. Custom HA integration, HACS-installable, that:
  - Listens to the built-in `apple_tv` integration's `media_player.*` entity and attributes time spent per app bundle id.
  - Aggregates a single overall daily budget per profile, resets at local midnight.
  - Enforces budget exhaustion by blocking the Apple TV's client in AdGuard Home + calling `media_player.turn_off`.
- 5 sensors: `time_used_today`, `time_remaining_today`, `current_app`, `enforcement_state`, `extension_minutes_today`.
- 3 services: `appletv_mgmt.force_block`, `appletv_mgmt.grant_extension`, `appletv_mgmt.reset_usage`.
- 2 bus events: `appletv_mgmt_usage_updated`, `appletv_mgmt_enforcement_changed`.
- UI config flow + options flow with live AdGuard probe.
- 22 unit tests covering the pure state machine (every transition + edge cases) and the AdGuard client (request shape, the AdGuard name-quirk, error paths).

### Architecture highlights

- `state.py` — pure state machine, zero HA imports, easy to unit-test.
- `adguard.py` — async REST client, only depends on `aiohttp`.
- `coordinator.py` — `DataUpdateCoordinator` subclass; listens to state changes plus a 30-second tick.
- `enforcer.py` — bridges the pure state machine to side effects.

[Unreleased]: https://github.com/jarvis2k1/ha-appletv-mgmt/compare/v0.8.1...HEAD
[0.8.1]: https://github.com/jarvis2k1/ha-appletv-mgmt/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/jarvis2k1/ha-appletv-mgmt/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/jarvis2k1/ha-appletv-mgmt/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/jarvis2k1/ha-appletv-mgmt/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/jarvis2k1/ha-appletv-mgmt/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/jarvis2k1/ha-appletv-mgmt/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/jarvis2k1/ha-appletv-mgmt/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/jarvis2k1/ha-appletv-mgmt/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/jarvis2k1/ha-appletv-mgmt/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/jarvis2k1/ha-appletv-mgmt/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/jarvis2k1/ha-appletv-mgmt/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/jarvis2k1/ha-appletv-mgmt/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/jarvis2k1/ha-appletv-mgmt/releases/tag/v0.1.0
