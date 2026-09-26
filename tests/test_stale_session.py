"""Tests for v0.17.1 — staleness detection on the apple_tv_entity_id.

Bug being prevented (live-reported 2026-05-31, Heimkinoaaa profile):

    Disney+ session opened 2026-05-30 21:28 UTC. media_player.heimkinoaaa
    last_updated stopped refreshing at 2026-05-31 00:39 UTC (pyatv
    silently lost its companion-protocol connection). Pre-v0.17.1 the
    coordinator only reacted to STATE CHANGES, so no _sync_open_event
    fired, the open UsageEvent never closed, and the daily counter
    accumulated 12.9 hours of phantom usage by morning. The kid was
    actually finished by ~02:00 UTC.

Post-v0.17.1: every coordinator tick checks
`media_player.heimkinoaaa.last_updated`. If the entity is in an ACTIVE
state AND hasn't been refreshed for >= `profile.stale_session_minutes`
(default 5), the coordinator closes the open event at the entity's
last_updated timestamp (best-guess actual end time, not `now`) AND
records an `app_stale_closed` audit row.
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "appletv_mgmt"


def _ensure_ha_stubs():
    for mod_name in (
        "homeassistant",
        "homeassistant.config_entries",
        "homeassistant.const",
        "homeassistant.core",
        "homeassistant.helpers",
        "homeassistant.helpers.aiohttp_client",
        "homeassistant.helpers.config_validation",
        "homeassistant.helpers.event",
        "homeassistant.helpers.storage",
        "homeassistant.helpers.update_coordinator",
        "homeassistant.util",
        "homeassistant.util.dt",
    ):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)
    const = sys.modules["homeassistant.const"]
    if not hasattr(const, "SERVICE_TURN_OFF"):
        const.SERVICE_TURN_OFF = "turn_off"
    if not hasattr(const, "Platform"):
        const.Platform = types.SimpleNamespace(
            SENSOR="sensor", SWITCH="switch", SELECT="select"
        )
    core = sys.modules["homeassistant.core"]
    if not hasattr(core, "HomeAssistant"):
        core.HomeAssistant = type("HomeAssistant", (), {})
    if not hasattr(core, "CALLBACK_TYPE"):
        core.CALLBACK_TYPE = type("CALLBACK_TYPE", (), {})
    if not hasattr(core, "Event"):
        core.Event = type("Event", (), {})
    if not hasattr(core, "EventStateChangedData"):
        core.EventStateChangedData = type("EventStateChangedData", (), {})
    if not hasattr(core, "callback"):
        core.callback = lambda f: f
    ev_mod = sys.modules["homeassistant.helpers.event"]
    if not hasattr(ev_mod, "async_call_later"):
        ev_mod.async_call_later = lambda hass, delay, callback: (lambda: None)
    if not hasattr(ev_mod, "async_track_state_change_event"):
        ev_mod.async_track_state_change_event = lambda *a, **kw: (lambda: None)
    if not hasattr(ev_mod, "async_track_time_interval"):
        ev_mod.async_track_time_interval = lambda *a, **kw: (lambda: None)
    upd_mod = sys.modules["homeassistant.helpers.update_coordinator"]
    if not hasattr(upd_mod, "DataUpdateCoordinator"):
        class _DUC:
            def __init__(self, *a, **kw):
                self.hass = a[0] if a else None
            def __class_getitem__(cls, item):
                return cls
            async def async_config_entry_first_refresh(self):
                pass
            async def async_request_refresh(self):
                pass
        upd_mod.DataUpdateCoordinator = _DUC
    storage_mod = sys.modules["homeassistant.helpers.storage"]
    if not hasattr(storage_mod, "Store"):
        class _Store:
            def __init__(self, *a, **kw):
                pass
            async def async_load(self):
                return None
            async def async_save(self, data):
                pass
        storage_mod.Store = _Store
    aio_mod = sys.modules["homeassistant.helpers.aiohttp_client"]
    if not hasattr(aio_mod, "async_get_clientsession"):
        aio_mod.async_get_clientsession = lambda hass: MagicMock()
    dt_mod = sys.modules["homeassistant.util.dt"]
    if not hasattr(dt_mod, "utcnow"):
        dt_mod.utcnow = lambda: datetime.now(timezone.utc)
        dt_mod.as_local = lambda d: d
        dt_mod.as_utc = lambda d: d


_ensure_ha_stubs()

for pkg_name in ("custom_components", "custom_components.appletv_mgmt"):
    if pkg_name not in sys.modules:
        stub = types.ModuleType(pkg_name)
        stub.__path__ = [str(PKG)] if pkg_name.endswith("appletv_mgmt") else []
        sys.modules[pkg_name] = stub

import importlib.util

for name in ("const", "storage", "policy", "state", "quiet", "adguard",
             "media_attribution", "schedule", "categorize"):
    full = f"custom_components.appletv_mgmt.{name}"
    if full not in sys.modules:
        spec = importlib.util.spec_from_file_location(full, PKG / f"{name}.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules[full] = m
        spec.loader.exec_module(m)

# Stub audit/voice/enforcer for the coordinator's transitive imports.
for name in ("audit", "voice_notifier"):
    full = f"custom_components.appletv_mgmt.{name}"
    if full not in sys.modules:
        stub = types.ModuleType(full)
        if name == "audit":
            stub.record_admin_action = MagicMock()
            stub.record_action = MagicMock()
            stub.register_action_recorder = lambda hass, profile_id: (lambda: None)
        elif name == "voice_notifier":
            stub.should_speak = lambda *a, **kw: True
            stub.speak = AsyncMock(return_value={"status": "spoken", "message": "x"})
        sys.modules[full] = stub

# Load enforcer (transitively needed by coordinator).
spec = importlib.util.spec_from_file_location(
    "custom_components.appletv_mgmt.enforcer", PKG / "enforcer.py"
)
if "custom_components.appletv_mgmt.enforcer" not in sys.modules:
    enforcer_mod = importlib.util.module_from_spec(spec)
    sys.modules["custom_components.appletv_mgmt.enforcer"] = enforcer_mod
    spec.loader.exec_module(enforcer_mod)

# Now the coordinator itself. v0.17.1 — force a fresh import even if
# `sys.modules` already has a stub for this module (test_init.py and
# friends populate it with a MagicMock for their own purposes, which
# clobbers our access to the real AppleTVMgmtCoordinator class). Save
# any pre-existing entry and restore it on test teardown so we don't
# break our siblings.
_PREV_COORD = sys.modules.get("custom_components.appletv_mgmt.coordinator")
spec = importlib.util.spec_from_file_location(
    "custom_components.appletv_mgmt.coordinator", PKG / "coordinator.py"
)
coord_mod = importlib.util.module_from_spec(spec)
sys.modules["custom_components.appletv_mgmt.coordinator"] = coord_mod
spec.loader.exec_module(coord_mod)


storage_mod = sys.modules["custom_components.appletv_mgmt.storage"]


def _make_profile(stale_session_minutes=5):
    return storage_mod.Profile(
        id="p1",
        display_name="Living Room",
        apple_tv_entity_id="media_player.heimkinoaaa",
        adguard_client_name="AppleTV",
        daily_budget_min=60,
        grace_seconds=60,
        warn_thresholds_min=[5],
        idle_grace_minutes=5,
        stale_session_minutes=stale_session_minutes,
    )


class _BareCoordinator:
    """Test-only coordinator-like object that does NOT inherit from
    DataUpdateCoordinator. Bypasses the base class' __init__ (which
    differs in subtle ways depending on which other test file polluted
    `sys.modules['homeassistant.helpers.update_coordinator']` first)
    by binding the method we care about directly. Pre-v0.17.1 this
    file ran clean in isolation but blew up in the full suite because
    test_runtime_state.py's stub of DataUpdateCoordinator lacked
    __class_getitem__. Sidestepping the inheritance graph entirely is
    the only reliable fix without enforcing test ordering.
    """
    _check_for_stale_session = coord_mod.AppleTVMgmtCoordinator._check_for_stale_session
    _fire_app_ended = coord_mod.AppleTVMgmtCoordinator._fire_app_ended

    def __init__(self, hass, store, enforcer, profile):
        self.hass = hass
        self._store = store
        self._enforcer = enforcer
        self._profile = profile


def _build_coordinator(profile):
    hass = MagicMock()
    hass.states = MagicMock()
    hass.async_create_task = lambda coro: asyncio.ensure_future(coro)
    hass.data = {}
    hass.bus = MagicMock()
    hass.bus.async_fire = MagicMock()

    store = MagicMock()
    store.open_event_for = MagicMock(return_value=None)
    store.close_open_event = MagicMock(return_value=None)
    store.async_save = AsyncMock(return_value=None)

    enforcer = MagicMock()
    enforcer.evaluate = AsyncMock()
    enforcer.reassert = AsyncMock()

    c = _BareCoordinator(hass, store, enforcer, profile)
    return c, hass, store


def _make_state(state_str, last_updated):
    """Build a minimal state-like object for hass.states.get to return."""
    s = MagicMock()
    s.state = state_str
    s.last_updated = last_updated
    s.attributes = {}
    return s


NOW = datetime(2026, 5, 31, 10, 30, 0, tzinfo=timezone.utc)


# ============================================================================
# Happy paths — no-op when conditions don't require closing
# ============================================================================


def test_no_op_when_threshold_is_zero():
    """`stale_session_minutes=0` disables the check entirely.

    Some users may not want the safeguard (e.g. they trust their ATV's
    pyatv connection or are debugging the entity state directly).
    """
    p = _make_profile(stale_session_minutes=0)
    c, hass, store = _build_coordinator(p)
    # Even with everything wrong, we should not touch the store.
    store.open_event_for.return_value = MagicMock(id="evt1", bundle_id="com.x", started_at=NOW - timedelta(minutes=15))
    hass.states.get.return_value = _make_state(
        "playing", NOW - timedelta(hours=12)
    )

    asyncio.run(c._check_for_stale_session(now=NOW))

    store.close_open_event.assert_not_called()


def test_no_op_when_no_open_event():
    """Nothing open → nothing to close."""
    p = _make_profile()
    c, hass, store = _build_coordinator(p)
    store.open_event_for.return_value = None
    hass.states.get.return_value = _make_state(
        "playing", NOW - timedelta(hours=12)
    )

    asyncio.run(c._check_for_stale_session(now=NOW))

    store.close_open_event.assert_not_called()


def test_no_op_when_state_is_inactive():
    """If the entity reports off/standby/idle, `_sync_open_event` will
    handle it on the same tick — we should NOT race-close it here."""
    p = _make_profile()
    c, hass, store = _build_coordinator(p)
    store.open_event_for.return_value = MagicMock(id="evt1", bundle_id="com.x", started_at=NOW - timedelta(minutes=15))
    hass.states.get.return_value = _make_state(
        "off", NOW - timedelta(hours=12)
    )

    asyncio.run(c._check_for_stale_session(now=NOW))

    store.close_open_event.assert_not_called()


def test_no_op_when_entity_recent():
    """ACTIVE state + recent last_updated → entity is healthy."""
    p = _make_profile(stale_session_minutes=5)
    c, hass, store = _build_coordinator(p)
    store.open_event_for.return_value = MagicMock(id="evt1", bundle_id="com.x", started_at=NOW - timedelta(minutes=15))
    # Last update 30 s ago — well under 5 min.
    hass.states.get.return_value = _make_state(
        "playing", NOW - timedelta(seconds=30)
    )

    asyncio.run(c._check_for_stale_session(now=NOW))

    store.close_open_event.assert_not_called()


def test_no_op_when_no_entity():
    """Entity removed / never registered → skip gracefully."""
    p = _make_profile()
    c, hass, store = _build_coordinator(p)
    store.open_event_for.return_value = MagicMock(id="evt1", bundle_id="com.x", started_at=NOW - timedelta(minutes=15))
    hass.states.get.return_value = None

    asyncio.run(c._check_for_stale_session(now=NOW))

    store.close_open_event.assert_not_called()


# ============================================================================
# Stale path — close the event at last_updated
# ============================================================================


def test_stale_closes_event_at_last_updated_not_now():
    """The live-reported repro: pyatv went silent at 00:39 UTC; we're
    waking up at 10:30 UTC. Default threshold is 5 min, so the event
    is definitely stale. The closed event's `ended_at` must be the
    entity's last_updated (00:39), NOT `now` (10:30) — preserves the
    kid's right not to be charged for the hours after pyatv died.
    """
    # v0.20.1 — the overnight-Disney scenario realistically has the TV OFF
    # (no-TV installs now KEEP_OPEN and rely on the runaway ceiling instead).
    p = _make_profile_with_tv(stale_session_minutes=5)
    c, hass, store = _build_coordinator(p)
    open_evt = MagicMock(id="evt1", bundle_id="com.disney.disneyplus", started_at=NOW - timedelta(minutes=15))
    store.open_event_for.return_value = open_evt
    last_updated = datetime(2026, 5, 31, 0, 39, 21, tzinfo=timezone.utc)
    hass.states.get.side_effect = _hass_states_get_router(
        _make_state("playing", last_updated), _make_state("off", NOW)
    )
    # Have the close return something so the audit branch runs.
    closed_evt = MagicMock(id="evt1", bundle_id="com.disney.disneyplus")
    store.close_open_event.return_value = closed_evt
    # Stub the app_ended hook to avoid touching the HA bus.
    c._fire_app_ended = MagicMock()

    asyncio.run(c._check_for_stale_session(now=NOW))

    store.close_open_event.assert_called_once_with(p.id, at=last_updated)
    c._fire_app_ended.assert_called_once_with(closed_evt)
    store.async_save.assert_called_once()


def test_stale_records_app_stale_closed_audit_row():
    """The cleanup must be visible on the dashboard so the parent
    understands why the counter moved without an obvious user action.
    """
    p = _make_profile_with_tv(stale_session_minutes=5)
    c, hass, store = _build_coordinator(p)
    open_evt = MagicMock(id="evt1", bundle_id="com.netflix.Netflix", started_at=NOW - timedelta(minutes=15))
    store.open_event_for.return_value = open_evt
    last_updated = NOW - timedelta(minutes=30)  # 30 min stale
    hass.states.get.side_effect = _hass_states_get_router(
        _make_state("playing", last_updated), _make_state("off", NOW)
    )
    closed_evt = MagicMock(id="evt1", bundle_id="com.netflix.Netflix")
    store.close_open_event.return_value = closed_evt
    c._fire_app_ended = MagicMock()

    audit_mod = sys.modules["custom_components.appletv_mgmt.audit"]
    audit_mod.record_admin_action.reset_mock()

    asyncio.run(c._check_for_stale_session(now=NOW))

    audit_mod.record_admin_action.assert_called_once()
    _, kwargs = audit_mod.record_admin_action.call_args
    assert kwargs.get("action") == "app_stale_closed"
    assert kwargs.get("profile_id") == "p1"
    # Detail mentions both the bundle id and the staleness duration.
    detail = kwargs.get("detail", "")
    assert "com.netflix.Netflix" in detail
    assert "1800s" in detail  # 30 min = 1800 s


def test_stale_threshold_boundary_exactly_at_threshold_closes():
    """If `last_updated` is EXACTLY `stale_session_minutes` old, that
    counts as stale (>= comparison). Avoids "1 second under threshold
    every tick" bouncing back and forth.
    """
    p = _make_profile_with_tv(stale_session_minutes=5)
    c, hass, store = _build_coordinator(p)
    store.open_event_for.return_value = MagicMock(
        id="evt1", bundle_id="com.x", started_at=NOW - timedelta(minutes=15)
    )
    # Exactly 5 min old, TV off (deterministic close).
    hass.states.get.side_effect = _hass_states_get_router(
        _make_state("playing", NOW - timedelta(minutes=5)), _make_state("off", NOW)
    )
    store.close_open_event.return_value = MagicMock(
        id="evt1", bundle_id="com.x"
    )
    c._fire_app_ended = MagicMock()

    asyncio.run(c._check_for_stale_session(now=NOW))

    store.close_open_event.assert_called_once()


def test_active_states_paused_buffering_also_count_as_potentially_stale():
    """paused / buffering / on / idle are all ACTIVE per
    media_attribution.ACTIVE_MEDIA_STATES — a stuck paused state has
    the same risk of unbounded accrual as a stuck playing state."""
    p = _make_profile_with_tv(stale_session_minutes=5)
    c, hass, store = _build_coordinator(p)
    store.open_event_for.return_value = MagicMock(
        id="evt1", bundle_id="com.x", started_at=NOW - timedelta(minutes=15)
    )
    hass.states.get.side_effect = _hass_states_get_router(
        _make_state("paused", NOW - timedelta(minutes=30)), _make_state("off", NOW)
    )
    store.close_open_event.return_value = MagicMock(
        id="evt1", bundle_id="com.x"
    )
    c._fire_app_ended = MagicMock()

    asyncio.run(c._check_for_stale_session(now=NOW))

    store.close_open_event.assert_called_once()


def test_audit_exception_does_not_crash_tick():
    """Defense in depth: the audit row write must not bubble up and
    crash the coordinator tick."""
    p = _make_profile_with_tv(stale_session_minutes=5)
    c, hass, store = _build_coordinator(p)
    store.open_event_for.return_value = MagicMock(
        id="evt1", bundle_id="com.x", started_at=NOW - timedelta(minutes=15)
    )
    hass.states.get.side_effect = _hass_states_get_router(
        _make_state("playing", NOW - timedelta(minutes=30)), _make_state("off", NOW)
    )
    store.close_open_event.return_value = MagicMock(
        id="evt1", bundle_id="com.x"
    )
    c._fire_app_ended = MagicMock()
    audit_mod = sys.modules["custom_components.appletv_mgmt.audit"]
    audit_mod.record_admin_action.side_effect = RuntimeError("boom")

    # Should not raise.
    asyncio.run(c._check_for_stale_session(now=NOW))

    store.close_open_event.assert_called_once()
    # Reset side_effect so subsequent tests aren't affected.
    audit_mod.record_admin_action.side_effect = None


# ============================================================================
# v0.17.2 — _sync_open_event refuses to open against a stale entity
# (closes the staleness re-open loop bug surfaced in the 2026-05-31 live test)
# ============================================================================


class _BareCoordinator2:
    """Test-only coordinator-like object that exposes the methods we
    need for v0.17.2 without invoking DataUpdateCoordinator's __init__.
    """
    _check_for_stale_session = coord_mod.AppleTVMgmtCoordinator._check_for_stale_session
    _fire_app_ended = coord_mod.AppleTVMgmtCoordinator._fire_app_ended
    _fire_app_started = coord_mod.AppleTVMgmtCoordinator._fire_app_started
    _sync_open_event = coord_mod.AppleTVMgmtCoordinator._sync_open_event
    _entity_is_stale = coord_mod.AppleTVMgmtCoordinator._entity_is_stale
    _effective_bundle_id = coord_mod.AppleTVMgmtCoordinator._effective_bundle_id

    def __init__(self, hass, store, enforcer, profile):
        self.hass = hass
        self._store = store
        self._enforcer = enforcer
        self._profile = profile
        self._last_known_bundle_id = None
        self._last_known_seen_at = None


def _build_coordinator_v0172(profile):
    hass = MagicMock()
    hass.states = MagicMock()
    hass.async_create_task = lambda coro: asyncio.ensure_future(coro)
    hass.data = {}
    hass.bus = MagicMock()
    hass.bus.async_fire = MagicMock()
    store = MagicMock()
    store.open_event_for = MagicMock(return_value=None)
    store.close_open_event = MagicMock(return_value=None)
    store.open_event = MagicMock(return_value=MagicMock(id="evt-new", bundle_id="x"))
    store.reopen_recent_event_if_match = MagicMock(return_value=None)
    store.async_save = AsyncMock(return_value=None)
    enforcer = MagicMock()
    enforcer.evaluate = AsyncMock()
    c = _BareCoordinator2(hass, store, enforcer, profile)
    return c, hass, store


def test_v0172_sync_open_event_refuses_to_open_against_stale_entity():
    """v0.17.2 (the live 2026-05-31 bug).

    The v0.17.1 close/reopen loop: staleness check correctly closes the
    stuck open event, then _sync_open_event immediately sees the same
    stale 'playing/Disney+' state and opens a fresh event, then next
    tick stales again. The audit log got polluted with hundreds of
    0-duration app_started/app_ended/app_stale_closed rows over a few
    minutes.

    Fix: _sync_open_event now consults _entity_is_stale before opening
    a new session. Stale state → refuse to open. CLOSING decisions
    unchanged (a state going inactive still closes the open session).
    """
    p = _make_profile(stale_session_minutes=5)
    c, hass, store = _build_coordinator_v0172(p)
    store.open_event_for.return_value = None  # no open event to close

    # Entity is stale by 30 min — well past the 5-min threshold.
    stale_state = _make_state(
        "playing", NOW - timedelta(minutes=30)
    )
    hass.states.get.return_value = stale_state

    asyncio.run(c._sync_open_event(
        effective="com.disney.disneyplus", now=NOW
    ))

    # No new event was opened (pre-fix would have called open_event here).
    store.open_event.assert_not_called()
    # Stitch path also bypassed for the same reason.
    store.reopen_recent_event_if_match.assert_not_called()


def test_v0172_sync_open_event_opens_when_entity_recent():
    """Sanity inverse: with a fresh entity, opening still works."""
    p = _make_profile(stale_session_minutes=5)
    c, hass, store = _build_coordinator_v0172(p)
    store.open_event_for.return_value = None
    # Entity refreshed 30 seconds ago — well under 5 min.
    fresh_state = _make_state("playing", NOW - timedelta(seconds=30))
    hass.states.get.return_value = fresh_state

    asyncio.run(c._sync_open_event(
        effective="com.disney.disneyplus", now=NOW
    ))

    # Opening proceeds normally.
    store.open_event.assert_called_once()


def test_v0172_sync_open_event_still_closes_on_inactive_state_even_when_stale():
    """Critical: stale check ONLY gates OPENING. When the entity reports
    state=off / standby (inactive), we MUST still close the open session
    so the timeline doesn't get a phantom open session forever.
    """
    p = _make_profile(stale_session_minutes=5)
    c, hass, store = _build_coordinator_v0172(p)
    # Open event present.
    open_evt = MagicMock(id="evt-open", bundle_id="com.disney.disneyplus", started_at=NOW - timedelta(minutes=15))
    store.open_event_for.return_value = open_evt
    store.close_open_event.return_value = open_evt
    c._fire_app_ended = MagicMock()

    # Entity is stale BUT state is "off" — should still close.
    hass.states.get.return_value = _make_state(
        "off", NOW - timedelta(minutes=30)
    )

    asyncio.run(c._sync_open_event(
        effective=None, now=NOW
    ))

    store.close_open_event.assert_called_once()


def test_v0172_sync_open_event_keeps_existing_event_when_same_bundle():
    """Continuation case: open event already exists, state still playing
    the same bundle. No new opening required, no gate matters."""
    p = _make_profile(stale_session_minutes=5)
    c, hass, store = _build_coordinator_v0172(p)
    open_evt = MagicMock(id="evt-open", bundle_id="com.disney.disneyplus", started_at=NOW - timedelta(minutes=15))
    store.open_event_for.return_value = open_evt
    # Even with stale state, since we're NOT about to open anything new,
    # no rejection happens. open_event is unchanged.
    hass.states.get.return_value = _make_state(
        "playing", NOW - timedelta(minutes=30)
    )

    asyncio.run(c._sync_open_event(
        effective="com.disney.disneyplus", now=NOW
    ))

    # No close, no new open — just continuing.
    store.close_open_event.assert_not_called()
    store.open_event.assert_not_called()


def test_v0172_sync_open_event_bundle_change_with_fresh_entity_works():
    """Bundle change with FRESH entity: close old session, open new one.
    Confirms the gate doesn't block legitimate app switches."""
    p = _make_profile(stale_session_minutes=5)
    c, hass, store = _build_coordinator_v0172(p)
    open_evt = MagicMock(id="evt-old", bundle_id="com.netflix.Netflix", started_at=NOW - timedelta(minutes=15))
    store.open_event_for.return_value = open_evt
    store.close_open_event.return_value = open_evt
    c._fire_app_ended = MagicMock()
    c._fire_app_started = MagicMock()
    hass.states.get.return_value = _make_state(
        "playing", NOW - timedelta(seconds=10)
    )

    asyncio.run(c._sync_open_event(
        effective="com.disney.disneyplus", now=NOW
    ))

    # Old closed, new opened.
    store.close_open_event.assert_called_once()
    store.open_event.assert_called_once()


def test_v0172_entity_is_stale_helper_threshold_zero_is_opt_out():
    """`stale_session_minutes=0` opts out of staleness completely
    (no closing OR opening rejection)."""
    p = _make_profile(stale_session_minutes=0)
    c, hass, store = _build_coordinator_v0172(p)
    hass.states.get.return_value = _make_state(
        "playing", NOW - timedelta(hours=12)
    )

    assert c._entity_is_stale(NOW) is False


# ============================================================================
# v0.17.3 — Samsung-liveness gate (the under-count fix)
#
# These tests cover the new branches added by `decide_stale_action`. The
# existing tests above continue to validate the no-`tv_entity_id`-configured
# branch (rule 6 of the pure function), which preserves the v0.17.1
# overnight-Disney behavior unchanged for any user who hasn't wired up
# Samsung. The tests below cover the `tv_entity_id` IS configured case.
#
# The hass.states.get mock here uses a side_effect so we can return
# different mocks for the apple_tv entity vs. the Samsung entity in the
# same test — that's the core asymmetry the gate exploits.
# ============================================================================


def _make_profile_with_tv(stale_session_minutes=5,
                          tv_entity_id="media_player.samsung_tv"):
    return storage_mod.Profile(
        id="p1",
        display_name="Living Room",
        apple_tv_entity_id="media_player.heimkinoaaa",
        adguard_client_name="AppleTV",
        daily_budget_min=60,
        grace_seconds=60,
        warn_thresholds_min=[5],
        idle_grace_minutes=5,
        stale_session_minutes=stale_session_minutes,
        tv_entity_id=tv_entity_id,
    )


def _hass_states_get_router(apple_tv_state_obj, samsung_state_obj):
    """Return a side_effect that returns different state mocks for the
    apple_tv vs. samsung entities. Used to mock hass.states.get when a
    test cares about both."""
    def _get(entity_id):
        if entity_id == "media_player.heimkinoaaa":
            return apple_tv_state_obj
        if entity_id == "media_player.samsung_tv":
            return samsung_state_obj
        return None
    return _get


def test_v0173_keep_open_when_tv_on_during_stale_window():
    """The under-count fix. Owner watching Netflix live; pyatv push-quiet
    for 6 min (apple_tv stale); Samsung TV stayed `on` throughout.

    Expected: NO close, NO audit row. Reproduces the live evidence from
    2026-06-09 (sessions at 13:00:24 and 15:12:12 — both 0.0 min
    recorded under v0.17.1, both Samsung=on throughout).
    """
    p = _make_profile_with_tv(stale_session_minutes=5)
    c, hass, store = _build_coordinator(p)
    open_evt = MagicMock(
        id="evt-netflix",
        bundle_id="com.netflix.Netflix",
        started_at=NOW - timedelta(minutes=6),
    )
    store.open_event_for.return_value = open_evt
    apple_tv = _make_state("playing", NOW - timedelta(minutes=6))
    samsung = _make_state("on", NOW - timedelta(hours=1))
    hass.states.get.side_effect = _hass_states_get_router(apple_tv, samsung)
    c._fire_app_ended = MagicMock()

    audit_mod = sys.modules["custom_components.appletv_mgmt.audit"]
    audit_mod.record_admin_action.reset_mock()

    asyncio.run(c._check_for_stale_session(now=NOW))

    # The whole point: we did NOT close, did NOT audit, did NOT save.
    store.close_open_event.assert_not_called()
    c._fire_app_ended.assert_not_called()
    audit_mod.record_admin_action.assert_not_called()
    store.async_save.assert_not_called()


def test_v0173_keep_open_when_tv_unknown_fail_open():
    """Samsung integration reporting `unknown` -> fail-open (KEEP_OPEN).

    Under-count is the worse failure for an enforcement tool — when the
    liveness signal is unreadable, default to keep-counting (bounded by
    the runaway-ceiling so a sustained Samsung outage can't grant
    unlimited time).
    """
    p = _make_profile_with_tv()
    c, hass, store = _build_coordinator(p)
    open_evt = MagicMock(
        id="evt1", bundle_id="com.netflix.Netflix",
        started_at=NOW - timedelta(minutes=10),
    )
    store.open_event_for.return_value = open_evt
    apple_tv = _make_state("playing", NOW - timedelta(minutes=10))
    samsung = _make_state("unknown", NOW - timedelta(hours=1))
    hass.states.get.side_effect = _hass_states_get_router(apple_tv, samsung)

    asyncio.run(c._check_for_stale_session(now=NOW))

    store.close_open_event.assert_not_called()


def test_v0173_keep_open_when_tv_unavailable_fail_open():
    """Samsung integration `unavailable` (dropped/uninstalled) -> KEEP_OPEN.
    Same fail-open posture as the deployed self-heal automation."""
    p = _make_profile_with_tv()
    c, hass, store = _build_coordinator(p)
    open_evt = MagicMock(
        id="evt1", bundle_id="com.netflix.Netflix",
        started_at=NOW - timedelta(minutes=10),
    )
    store.open_event_for.return_value = open_evt
    apple_tv = _make_state("playing", NOW - timedelta(minutes=10))
    samsung = _make_state("unavailable", NOW - timedelta(hours=1))
    hass.states.get.side_effect = _hass_states_get_router(apple_tv, samsung)

    asyncio.run(c._check_for_stale_session(now=NOW))

    store.close_open_event.assert_not_called()


def test_v0173_close_at_last_updated_when_tv_off():
    """The overnight-Disney path: apple_tv stale `playing`, Samsung `off`
    (device definitively off) -> close at last_updated. This is what the
    v0.17.1 fix did for everyone; under v0.17.3 it fires only when the
    Samsung-on liveness signal corroborates the close."""
    p = _make_profile_with_tv(stale_session_minutes=5)
    c, hass, store = _build_coordinator(p)
    open_evt = MagicMock(
        id="evt-disney",
        bundle_id="com.disney.disneyplus",
        started_at=NOW - timedelta(hours=1),
    )
    store.open_event_for.return_value = open_evt
    last_updated = NOW - timedelta(hours=1)  # very stale
    apple_tv = _make_state("playing", last_updated)
    samsung = _make_state("off", NOW - timedelta(hours=1))
    hass.states.get.side_effect = _hass_states_get_router(apple_tv, samsung)
    closed_evt = MagicMock(id="evt-disney", bundle_id="com.disney.disneyplus")
    store.close_open_event.return_value = closed_evt
    c._fire_app_ended = MagicMock()

    asyncio.run(c._check_for_stale_session(now=NOW))

    # MUST close at last_updated (not now) — preserves the overnight-Disney
    # contract: kid not charged for time after pyatv went dark.
    store.close_open_event.assert_called_once_with(p.id, at=last_updated)
    c._fire_app_ended.assert_called_once_with(closed_evt)


def test_v0173_close_at_last_updated_when_tv_standby():
    """`standby` is in the definitely-off set (some Samsung firmwares
    report it instead of `off`)."""
    p = _make_profile_with_tv()
    c, hass, store = _build_coordinator(p)
    open_evt = MagicMock(
        id="evt1", bundle_id="com.x",
        started_at=NOW - timedelta(hours=1),
    )
    store.open_event_for.return_value = open_evt
    last_updated = NOW - timedelta(minutes=30)
    apple_tv = _make_state("playing", last_updated)
    samsung = _make_state("standby", NOW - timedelta(hours=1))
    hass.states.get.side_effect = _hass_states_get_router(apple_tv, samsung)
    store.close_open_event.return_value = MagicMock(
        id="evt1", bundle_id="com.x"
    )
    c._fire_app_ended = MagicMock()

    asyncio.run(c._check_for_stale_session(now=NOW))

    store.close_open_event.assert_called_once_with(p.id, at=last_updated)


def test_v0173_runaway_ceiling_force_closes_even_with_tv_on():
    """Samsung stuck `on` is a documented failure mode (firmware bugs +
    cable users who never turn the TV off). Without the ceiling the open
    event would accrue forever; this caps the worst-case damage at
    ~STALE_RUNAWAY_CEILING_S of the kid's daily budget.

    Audit detail must call out runaway so the parent can see this is
    NOT a normal stale-close.
    """
    from custom_components.appletv_mgmt.media_attribution import (
        STALE_RUNAWAY_CEILING_S,
    )
    p = _make_profile_with_tv(stale_session_minutes=5)
    c, hass, store = _build_coordinator(p)
    # Open event has been open for > ceiling (Samsung-on stuck scenario).
    started_at = NOW - timedelta(seconds=STALE_RUNAWAY_CEILING_S + 600)
    open_evt = MagicMock(
        id="evt-stuck",
        bundle_id="com.netflix.Netflix",
        started_at=started_at,
    )
    store.open_event_for.return_value = open_evt
    last_updated = NOW - timedelta(minutes=10)  # stale enough to gate
    apple_tv = _make_state("playing", last_updated)
    samsung = _make_state("on", NOW - timedelta(hours=4))
    hass.states.get.side_effect = _hass_states_get_router(apple_tv, samsung)
    store.close_open_event.return_value = MagicMock(
        id="evt-stuck", bundle_id="com.netflix.Netflix"
    )
    c._fire_app_ended = MagicMock()

    audit_mod = sys.modules["custom_components.appletv_mgmt.audit"]
    audit_mod.record_admin_action.reset_mock()

    asyncio.run(c._check_for_stale_session(now=NOW))

    # Closes at last_updated (NOT now) — same anti-phantom-charge rule.
    store.close_open_event.assert_called_once_with(p.id, at=last_updated)
    # Audit reason mentions runaway so the parent sees why.
    audit_mod.record_admin_action.assert_called_once()
    _, kwargs = audit_mod.record_admin_action.call_args
    assert "runaway-ceiling" in kwargs.get("detail", "")


def test_v0173_no_op_when_apple_tv_age_below_threshold_even_with_tv_off():
    """Sanity: TV state is irrelevant when apple_tv is still fresh. The
    Samsung gate is a tie-breaker for the STALE case only; for a healthy
    fresh entity we do nothing (rule 4 fires before rule 6/7)."""
    p = _make_profile_with_tv()
    c, hass, store = _build_coordinator(p)
    open_evt = MagicMock(
        id="evt1", bundle_id="com.x",
        started_at=NOW - timedelta(minutes=5),
    )
    store.open_event_for.return_value = open_evt
    apple_tv = _make_state("playing", NOW - timedelta(seconds=30))
    samsung = _make_state("off", NOW - timedelta(hours=1))
    hass.states.get.side_effect = _hass_states_get_router(apple_tv, samsung)

    asyncio.run(c._check_for_stale_session(now=NOW))

    store.close_open_event.assert_not_called()


def test_v0173_clock_skew_negative_age_clamped_to_zero():
    """NTP can briefly put last_updated slightly ahead of `now` right
    after HA restart. A naive subtraction would yield a negative age
    that would pass <threshold and noop — but the clamp also defends
    against the inverse: a future contributor flipping the inequality
    accidentally. With max(0, ...) the negative age is treated as 0
    (fresh entity) and we noop — same outcome by design."""
    p = _make_profile_with_tv()
    c, hass, store = _build_coordinator(p)
    open_evt = MagicMock(
        id="evt1", bundle_id="com.x",
        started_at=NOW - timedelta(minutes=5),
    )
    store.open_event_for.return_value = open_evt
    # last_updated is 5 seconds AFTER `now` (clock skew)
    apple_tv = _make_state("playing", NOW + timedelta(seconds=5))
    samsung = _make_state("on", NOW - timedelta(hours=1))
    hass.states.get.side_effect = _hass_states_get_router(apple_tv, samsung)

    # Must not raise (clamp prevents negative-arithmetic surprises);
    # must not close (age=0 < 300s threshold).
    asyncio.run(c._check_for_stale_session(now=NOW))

    store.close_open_event.assert_not_called()
