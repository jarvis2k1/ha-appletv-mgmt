# Architecture

This document explains *how* the integration is wired together. For *what* the integration does (behaviors, contracts, data model) read [SPEC.md](../SPEC.md). For installation read [README.md](../README.md). For the service/entity contract read [API.md](API.md).

## 1. Two-repo layout

The full system is split across two repositories — the integration and a companion proxy addon.

| Repo | What it ships | Where it runs | Why it exists |
|---|---|---|---|
| [`jarvis2k1/ha-appletv-mgmt`](https://github.com/jarvis2k1/ha-appletv-mgmt) | A HACS-installable custom integration (Python). | Inside HA Core's container. | The business logic — coordinator, state machine, storage, sensors, services. |
| [`jarvis2k1/ha-appletv-mgmt-adguard-proxy`](https://github.com/jarvis2k1/ha-appletv-mgmt-adguard-proxy) | A small HA add-on (Python/FastAPI). | Host network mode on the HA OS host. | Bridges HA Core to the AdGuard Home addon, whose REST API is bound to `127.0.0.1:<random port>` and therefore unreachable from any other container. |

They communicate over plain HTTP on the LAN — `http://homeassistant.local:8101` by default — gated by an `X-API-Key` header.

## 2. Component diagram

```
                            HA OS host (Raspberry Pi 4)
┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                             │
│   Container: homeassistant                                                  │
│   ┌─────────────────────────────────────────────────────────────────────┐  │
│   │  HA Core                                                            │  │
│   │  ┌────────────────────────────────────────────────────────────┐    │  │
│   │  │  custom_components/appletv_mgmt/                           │    │  │
│   │  │  ┌──────────────┐ ┌────────────┐ ┌────────────────────┐   │    │  │
│   │  │  │ coordinator  │ │ enforcer   │ │ AdGuardClient      │   │    │  │
│   │  │  │  (per-tick)  │─┤ (state mgr)│─│  (HTTP /control/*) │   │    │  │
│   │  │  └──────────────┘ └────────────┘ └─────────┬──────────┘   │    │  │
│   │  │       │              ▲                     │              │    │  │
│   │  │       │ subscribes   │ pure                │ X-API-Key    │    │  │
│   │  │       │ to state-    │ compute_            │              │    │  │
│   │  │       │ change       │ next_state          │              │    │  │
│   │  │       ▼              │                     │              │    │  │
│   │  │  ┌──────────────────────┐  ┌────────────┐  │              │    │  │
│   │  │  │ media_player.        │  │ storage    │  │              │    │  │
│   │  │  │ <apple_tv> (pyatv)   │  │ (Store)    │  │              │    │  │
│   │  │  └──────────────────────┘  └────────────┘  │              │    │  │
│   │  └────────────────────────────────────────────┼──────────────┘    │  │
│   └────────────────────────────────────────────────┼───────────────────┘  │
│                                                    │ TCP                  │
│                                                    │ 192.168.x.y:8101     │
│   Container: addon_local_appletv_adguard_proxy     ▼                      │
│   ┌─────────────────────────────────────────────────────────────────────┐  │
│   │  uvicorn → FastAPI app (proxy.py)                                   │  │
│   │  - reads /proc/net/tcp to find localhost listeners                  │  │
│   │  - probes each for /control/status with AdGuard JSON signature      │  │
│   │  - forwards /control/* requests to discovered port                  │  │
│   │  - host_network = true → shares loopback with AdGuard               │  │
│   └─────────────────────────┬───────────────────────────────────────────┘  │
│                             │ TCP                                          │
│                             │ 127.0.0.1:<ephemeral>                        │
│                             ▼                                              │
│   Container: addon_a0d7b954_adguard (also host_network)                    │
│   ┌─────────────────────────────────────────────────────────────────────┐  │
│   │  AdGuard Home                                                        │  │
│   │  bound: 127.0.0.1:<ephemeral>                                       │  │
│   │  ingress proxy via HA Supervisor handles UI access                  │  │
│   └─────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 3. Module map (`custom_components/appletv_mgmt/`)

Layered so each module has the minimum dependencies it needs. Pure-logic modules (`state.py`, `adguard.py`) are unit-tested without HA installed.

```
manifest.json
hacs.json
const.py             ─── shared constants, domain key, defaults

state.py             ─── compute_next_state — pure function, no HA imports
adguard.py           ─── AdGuardClient — async aiohttp wrapper, no HA imports
                          \
storage.py            \    \
  ├─ Profile          │     │
  ├─ UsageEvent       │     │
  └─ AppleTVMgmtStore │     │  ← imports HA's Store; HA-coupled
                      │     │
enforcer.py ──────────┴─────┴──── EnforcementController
   uses: state, adguard, storage           - holds in-memory enforcement state
   imports HA's service registry           - calls AdGuard via AdGuardClient
                                            - calls media_player.turn_off
coordinator.py ─── AppleTVMgmtCoordinator (DataUpdateCoordinator subclass)
   uses: enforcer, storage                  - listens to media_player state
   imports HA's helpers/event               - aggregates usage per local day
                                            - drives enforcer on every tick

__init__.py ─── async_setup_entry / async_unload_entry
   wires the above into HA, registers services

config_flow.py ─── single-step UI install + options flow

sensor.py        ─── 5 CoordinatorEntity-backed sensors
services.yaml    ─── service schemas (Developer Tools UI)
translations/en.json ─── error strings, field labels
```

### Why `state.py` and `adguard.py` are separate from `enforcer.py`

Both are *pure* in HA terms — `state.py` has no I/O, `adguard.py` only imports `aiohttp`. Splitting them out means our test suite imports only those two modules (no HA Core install needed in dev), keeps the unit tests fast, and makes the state machine trivial to reason about in isolation.

## 4. Request paths

### Path A — observing the Apple TV

```
1. pyatv (running inside the Apple TV integration in HA Core) receives a
   "now-playing" update from the Apple TV over MRP/AirPlay.

2. The built-in apple_tv integration translates that into a state change
   on the media_player entity: state, attributes.app_id, etc.

3. AppleTVMgmtCoordinator's listener (async_track_state_change_event)
   fires _handle_state_change.

4. _sync_open_event closes the prior open UsageEvent (if any) and opens a
   new one with the current bundle_id.

5. Storage is saved (one .storage write per change — small, JSON).
```

### Path B — periodic tick (every 30 s)

```
1. DataUpdateCoordinator's _async_update_data fires.

2. We re-poll the media_player state to catch the "device turned off
   without firing a state change" case.

3. used_seconds_today = aggregate of all events clipped to local-today.
   extension_seconds = extension_minutes_today * 60.

4. enforcer.evaluate(used_seconds - extension_seconds, now) runs.

5. enforcer calls state.compute_next_state(...) → StateDecision.

6. On state change, side effects fire (AdGuard + media_player.turn_off).

7. A snapshot dict is returned. Sensors update via CoordinatorEntity.
```

### Path C — service call (e.g. `appletv_mgmt.grant_extension`)

```
1. HA service registry routes the call to _grant_extension in __init__.py.

2. We look up the profile bundle (coordinator + enforcer + storage).

3. store.add_extension_minutes(profile_id, minutes), then store.async_save().

4. coordinator.async_request_refresh() → triggers an immediate tick → state
   machine recomputes → if remaining > 0, ENFORCING → OK → AdGuard unblock.

Net effect: extension granted, AdGuard unblocked within ~1 second.
```

### Path D — AdGuard block (only on `→ ENFORCING`)

```
1. enforcer._enter_enforcing()
     ↓
2. AdGuardClient.set_blocked(client_name, True)
     ↓
3. HTTP GET /control/clients (via proxy)
     ↓ returns full client list
4. HTTP POST /control/clients/update with the full client doc, only
   use_global_blocked_services + blocked_services mutated.
     ↓
5. HA service call: media_player.turn_off (non-blocking).

If AdGuard returns 5xx, the integration logs and continues — the state
machine still entered ENFORCING. The next tick (30 s) will retry.
```

## 5. Persistence & restart resilience

| Concern | Handling |
|---|---|
| HA restart | `async_unload_entry` closes any open event and saves storage. On restart, the coordinator opens a fresh event matching whatever the device is doing right now. |
| Open event across restart | Closed at unload, *not* lost — the time before unload is preserved as a closed event. |
| AdGuard addon restart (new ephemeral port) | The proxy invalidates its cached target on 502/connection error and re-discovers on the next request. |
| Apple TV unplugged then plugged back in | Open event closes when media_player → `unavailable`, opens fresh when it returns. The duration in between is *not* counted (the device was off). |
| Time zone changes | Aggregation uses HA's `dt_util.as_local` — picks up the configured HA timezone every call. Travel scenarios "just work". |
| Storage corruption | `Store` reads `None` on parse error and we fall back to an empty state. Worst case: today's counter starts fresh and historical events are gone — no crash. |

## 6. Testing strategy

Phase 1 ships **22 unit tests** in [`tests/`](../tests). Two modules are pure and exhaustively covered:

- `test_state.py` — 15 cases hitting every transition + 3 edge cases (negative used_seconds, zero warn threshold, used == budget boundary).
- `test_adguard.py` — 7 cases verifying the AdGuard request shape (envelope vs data block, the name-quirk, the no-creds case, the api-key case, error paths) using `aioresponses` to stub aiohttp.

Storage and coordinator are tested in Phase 2 via `pytest-homeassistant-custom-component`. Until then, smoke tests on the live Pi are the verification path.

## 7. Future-proofing notes

- **Multi-profile / shared Apple TV** (Phase 2 ext): the `Profile` table is already keyed by id, not by Apple TV — adding a "who's watching" picker means adding a UI surface that fires a service to flip the active profile pointer, not changing the data model.
- **REST API for OpenClaw** (Phase 3): the integration will register `HomeAssistantView` subclasses at `/api/appletv_mgmt/*`. The `EnforcementController` + `AppleTVMgmtStore` are already the right level of abstraction for those endpoints to call into — no internal refactor needed.
- **Lovelace card** (Phase 4): card state comes from sensors (already exposed) plus a couple of synthetic sensors we'll add for `pending_requests` and `per_app_usage`. No coordinator changes required.
