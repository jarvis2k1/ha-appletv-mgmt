"""v0.20.0 — ONE system, not two: the unified Living Room profile.

The merge folds a secondary device (the Xbox) into the PRIMARY (Apple TV)
profile so BOTH accrue to ONE shared daily/group budget and BOTH are blocked
on exhaustion. This suite covers every blocker the adversarial review found:

  - storage: secondary_devices round-trips + defaults [] for legacy JSON.
  - coordinator._resolve_room_activity: Apple-TV-wins union, never double-count,
    Xbox fold-in when the Apple TV is idle/off, back-compat for no-secondary.
  - coordinator._sync_open_event: the staleness gate is BYPASSED for a secondary
    source (the critical fix — without it the Xbox event never opens while the
    Apple TV is off >= stale_session_minutes), but STILL applies to the primary.
  - coordinator._check_for_stale_session: must NOT close an open xbox.console
    event when the Apple TV is frozen at "playing" (pyatv freeze).
  - media_attribution.someone_could_be_watching: a live secondary keeps the
    warn/countdown voices firing even with the Apple TV off + Samsung off.
  - enforcer: _enter_enforcing fans the block out to the secondary switch (in
    the same transition as the Apple TV + Samsung kill); _exit_enforcing
    restores it; a secondary switch failure does NOT mark enforcement failed;
    adult mode suppresses the secondary block; reassert Job 3 re-asserts a
    secondary switch the kid flipped back on.

REST-validator coverage for secondary_devices lives in test_api_validators.py.

Same stub-the-world import pattern as test_stale_session.py / test_enforcement_failed.py.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "appletv_mgmt"

NOW = datetime(2026, 6, 27, 20, 30, 0, tzinfo=timezone.utc)


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
    if not hasattr(core, "callback"):
        core.callback = lambda f: f
    for _n in ("Event", "EventStateChangedData", "ServiceCall", "State"):
        if not hasattr(core, _n):
            setattr(core, _n, type(_n, (), {}))
            # Support `Event[EventStateChangedData]` subscripting in annotations.
            getattr(core, _n).__class_getitem__ = classmethod(lambda cls, item: cls)
    ev_mod = sys.modules["homeassistant.helpers.event"]
    if not hasattr(ev_mod, "async_call_later"):
        ev_mod.async_call_later = lambda hass, delay, cb: (lambda: None)
    if not hasattr(ev_mod, "async_track_state_change_event"):
        ev_mod.async_track_state_change_event = lambda *a, **kw: (lambda: None)
    if not hasattr(ev_mod, "async_track_time_interval"):
        ev_mod.async_track_time_interval = lambda *a, **kw: (lambda: None)
    uc_mod = sys.modules["homeassistant.helpers.update_coordinator"]
    if not hasattr(uc_mod, "DataUpdateCoordinator"):
        class _DUC:
            def __init__(self, *a, **kw):
                pass

            def __class_getitem__(cls, item):
                return cls
        uc_mod.DataUpdateCoordinator = _DUC
    ac_mod = sys.modules["homeassistant.helpers.aiohttp_client"]
    if not hasattr(ac_mod, "async_get_clientsession"):
        ac_mod.async_get_clientsession = lambda hass: MagicMock()
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

for name in ("const", "storage", "policy", "state", "quiet", "adguard",
             "media_attribution", "schedule", "categorize", "dns_classifier"):
    full = f"custom_components.appletv_mgmt.{name}"
    if full not in sys.modules:
        spec = importlib.util.spec_from_file_location(full, PKG / f"{name}.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules[full] = m
        spec.loader.exec_module(m)

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

# Force-load the real enforcer + coordinator (siblings may have stubbed them).
spec = importlib.util.spec_from_file_location(
    "custom_components.appletv_mgmt.enforcer", PKG / "enforcer.py"
)
enforcer_mod = importlib.util.module_from_spec(spec)
sys.modules["custom_components.appletv_mgmt.enforcer"] = enforcer_mod
spec.loader.exec_module(enforcer_mod)

spec = importlib.util.spec_from_file_location(
    "custom_components.appletv_mgmt.coordinator", PKG / "coordinator.py"
)
coord_mod = importlib.util.module_from_spec(spec)
sys.modules["custom_components.appletv_mgmt.coordinator"] = coord_mod
spec.loader.exec_module(coord_mod)

storage_mod = sys.modules["custom_components.appletv_mgmt.storage"]
const_mod = sys.modules["custom_components.appletv_mgmt.const"]
ma_mod = sys.modules["custom_components.appletv_mgmt.media_attribution"]

STATE_OK = const_mod.STATE_OK
STATE_ENFORCING = const_mod.STATE_ENFORCING

XBOX_SW = "switch.xboxone_internet_access"
XBOX_TRACKER = "device_tracker.xboxone"
XBOX_BUNDLE = "xbox.console"


def _secondary_xbox():
    return {
        "entity_id": XBOX_TRACKER,
        "device_kind": "xbox_presence",
        "enforcement_switch_entity_id": XBOX_SW,
        "bundle_id": XBOX_BUNDLE,
    }


def _make_merge_profile(*, secondaries=None, tv_shutdown_target="media_player.samsung_tv",
                        **overrides):
    """The unified Living Room profile: primary=Apple TV, one Xbox secondary."""
    defaults = dict(
        id="lr-1",
        display_name="Living Room",
        apple_tv_entity_id="media_player.heimkinoaaa",
        adguard_client_name="AppleTV",
        daily_budget_min=60,
        grace_seconds=60,
        warn_thresholds_min=[5],
        idle_grace_minutes=5,
        stale_session_minutes=5,
        device_kind="apple_tv",
        tv_entity_id="media_player.samsung_tv",
        tv_shutdown_target=tv_shutdown_target,
        secondary_devices=(
            [_secondary_xbox()] if secondaries is None else secondaries
        ),
    )
    defaults.update(overrides)
    return storage_mod.Profile(**defaults)


def _state(state_str, last_updated=NOW, attrs=None):
    s = MagicMock()
    s.state = state_str
    s.last_updated = last_updated
    s.attributes = attrs or {}
    return s


# ---------------------------------------------------------------------------
# coordinator bare double (no DataUpdateCoordinator.__init__)
# ---------------------------------------------------------------------------


class _Coord:
    _resolve_room_activity = coord_mod.AppleTVMgmtCoordinator._resolve_room_activity
    _effective_bundle_id = coord_mod.AppleTVMgmtCoordinator._effective_bundle_id
    _extract_activity_signal = coord_mod.AppleTVMgmtCoordinator._extract_activity_signal
    _entity_is_stale = coord_mod.AppleTVMgmtCoordinator._entity_is_stale
    _secondary_entity_ids = coord_mod.AppleTVMgmtCoordinator._secondary_entity_ids
    # v0.21.0 — native-TV helpers the resolver / seed / resubscribe now call.
    _native_tv_entity_id = coord_mod.AppleTVMgmtCoordinator._native_tv_entity_id
    _watched_entity_ids = coord_mod.AppleTVMgmtCoordinator._watched_entity_ids
    _sync_open_event = coord_mod.AppleTVMgmtCoordinator._sync_open_event
    _seed_from_current_state = coord_mod.AppleTVMgmtCoordinator._seed_from_current_state
    _check_for_stale_session = coord_mod.AppleTVMgmtCoordinator._check_for_stale_session
    _fire_app_started = coord_mod.AppleTVMgmtCoordinator._fire_app_started
    _fire_app_ended = coord_mod.AppleTVMgmtCoordinator._fire_app_ended

    def _handle_state_change(self, event):  # dummy callback for resubscribe test
        return None

    def __init__(self, profile, *, states):
        self._profile = profile
        self._last_known_bundle_id = None
        self._last_known_seen_at = None
        self._unsub_state = None
        self.hass = MagicMock()
        # states: dict entity_id -> state object (or None)
        self.hass.states.get = lambda eid: states.get(eid)
        self.hass.bus = MagicMock()
        self.hass.bus.async_fire = MagicMock()
        store = MagicMock()
        store.open_event_for = MagicMock(return_value=None)
        store.close_open_event = MagicMock(return_value=None)
        store.open_event = MagicMock(return_value=MagicMock(id="evt", bundle_id="x"))
        store.reopen_recent_event_if_match = MagicMock(return_value=None)
        store.async_save = AsyncMock(return_value=None)
        self._store = store


# ===========================================================================
# storage round-trip
# ===========================================================================


def test_secondary_devices_round_trips_to_dict_and_back():
    p = _make_merge_profile()
    d = p.to_dict()
    assert d["secondary_devices"] == [_secondary_xbox()]
    p2 = storage_mod.Profile.from_dict(d)
    assert p2.secondary_devices == [_secondary_xbox()]


def test_secondary_devices_defaults_empty_for_legacy_json():
    """A profile persisted before v0.20.0 has no secondary_devices key — it
    must default to [] (back-compat: behaves like a single-device profile)."""
    p = storage_mod.Profile.from_dict({
        "id": "p1",
        "display_name": "Living Room",
        "apple_tv_entity_id": "media_player.heimkinoaaa",
        "adguard_client_name": "AppleTV",
        "daily_budget_min": 60,
        "grace_seconds": 60,
        "warn_thresholds_min": [5],
        "idle_grace_minutes": 5,
    })
    assert p.secondary_devices == []


# ===========================================================================
# _resolve_room_activity — the union
# ===========================================================================


def test_resolve_apple_tv_wins_when_playing_even_if_xbox_home():
    """Both devices physically on → the room collapses to the Apple TV app
    (never double-counted). from_secondary is False."""
    p = _make_merge_profile()
    states = {
        "media_player.heimkinoaaa": _state(
            "playing", attrs={"app_id": "com.netflix.Netflix"}
        ),
        XBOX_TRACKER: _state("home"),
    }
    c = _Coord(p, states=states)
    effective, from_secondary = c._resolve_room_activity(NOW)
    assert effective == "com.netflix.Netflix"
    assert from_secondary is False


def test_resolve_xbox_wins_when_apple_tv_off():
    """The fold-in case: Apple TV off + Xbox home → xbox.console, and
    from_secondary=True so the staleness gate is bypassed downstream."""
    p = _make_merge_profile()
    states = {
        "media_player.heimkinoaaa": _state("off"),
        XBOX_TRACKER: _state("home"),
    }
    c = _Coord(p, states=states)
    effective, from_secondary = c._resolve_room_activity(NOW)
    assert effective == XBOX_BUNDLE
    assert from_secondary is True


def test_resolve_idle_when_apple_tv_off_and_xbox_not_home():
    p = _make_merge_profile()
    states = {
        "media_player.heimkinoaaa": _state("off"),
        XBOX_TRACKER: _state("not_home"),
    }
    c = _Coord(p, states=states)
    effective, from_secondary = c._resolve_room_activity(NOW)
    assert effective is None
    assert from_secondary is False


def test_resolve_does_not_pollute_idle_grace_memory_with_xbox():
    """When the Xbox wins, xbox.console must NOT be written into the Apple TV's
    idle-grace memory (cross-contamination guard from the correctness review)."""
    p = _make_merge_profile()
    states = {
        "media_player.heimkinoaaa": _state("off"),
        XBOX_TRACKER: _state("home"),
    }
    c = _Coord(p, states=states)
    c._resolve_room_activity(NOW)
    assert c._last_known_bundle_id != XBOX_BUNDLE


def test_resolve_back_compat_no_secondaries_matches_primary_signal():
    """With secondary_devices=[], the resolver returns exactly the primary's
    effective bundle for playing / idle / off — single-device parity."""
    p = _make_merge_profile(secondaries=[])
    # playing
    c = _Coord(p, states={"media_player.heimkinoaaa": _state(
        "playing", attrs={"app_id": "com.disney.disneyplus"})})
    eff, sec = c._resolve_room_activity(NOW)
    assert (eff, sec) == ("com.disney.disneyplus", False)
    # off → None
    c = _Coord(p, states={"media_player.heimkinoaaa": _state("off")})
    eff, sec = c._resolve_room_activity(NOW)
    assert (eff, sec) == (None, False)


# ===========================================================================
# _sync_open_event — staleness gate scoped to the primary
# ===========================================================================


def test_sync_open_bypasses_stale_gate_for_secondary_source():
    """THE critical fix: with the Apple TV stale (off/quiet 30 min), a secondary
    (Xbox) event must STILL open — otherwise zero Xbox minutes are ever logged
    and the budget never bites."""
    p = _make_merge_profile()
    # Apple TV entity is old (stale) — the staleness gate would normally refuse.
    states = {"media_player.heimkinoaaa": _state(
        "off", last_updated=NOW - timedelta(minutes=30))}
    c = _Coord(p, states=states)
    c._store.open_event_for.return_value = None
    asyncio.run(c._sync_open_event(
        effective=XBOX_BUNDLE, from_secondary=True, now=NOW))
    c._store.open_event.assert_called_once()


def test_sync_open_still_gates_stale_for_primary_source():
    """Scope check: the bypass is ONLY for secondaries. A primary (Apple TV)
    bundle against a stale entity is still refused (v0.17.2 behavior intact)."""
    p = _make_merge_profile()
    states = {"media_player.heimkinoaaa": _state(
        "playing", last_updated=NOW - timedelta(minutes=30))}
    c = _Coord(p, states=states)
    c._store.open_event_for.return_value = None
    asyncio.run(c._sync_open_event(
        effective="com.netflix.Netflix", from_secondary=False, now=NOW))
    c._store.open_event.assert_not_called()


# ===========================================================================
# _check_for_stale_session — must not close a secondary's open event
# ===========================================================================


def test_stale_session_does_not_close_open_xbox_event():
    """Apple TV frozen at 'playing' (pyatv freeze) while the open event is the
    Xbox's gaming session → the Apple-TV staleness machinery must NOT close it
    (that would silently drop live Xbox minutes)."""
    p = _make_merge_profile()
    open_evt = MagicMock(id="evt", bundle_id=XBOX_BUNDLE,
                         started_at=NOW - timedelta(minutes=15))
    states = {
        "media_player.heimkinoaaa": _state(
            "playing", last_updated=NOW - timedelta(minutes=30)),
        "media_player.samsung_tv": _state("off"),
    }
    c = _Coord(p, states=states)
    c._store.open_event_for.return_value = open_evt
    asyncio.run(c._check_for_stale_session(now=NOW))
    c._store.close_open_event.assert_not_called()


def test_stale_session_still_closes_normal_apple_event():
    """Regression guard: a normal Apple TV event still closes when stale +
    Samsung off (the overnight-Disney protection is untouched)."""
    p = _make_merge_profile()
    open_evt = MagicMock(id="evt", bundle_id="com.disney.disneyplus",
                         started_at=NOW - timedelta(minutes=15))
    states = {
        "media_player.heimkinoaaa": _state(
            "playing", last_updated=NOW - timedelta(minutes=30)),
        "media_player.samsung_tv": _state("off"),
    }
    c = _Coord(p, states=states)
    c._store.open_event_for.return_value = open_evt
    c._store.close_open_event.return_value = open_evt
    c._fire_app_ended = MagicMock()
    asyncio.run(c._check_for_stale_session(now=NOW))
    c._store.close_open_event.assert_called_once()


# ===========================================================================
# someone_could_be_watching — secondary keeps the voices alive
# ===========================================================================


def test_someone_watching_true_when_secondary_active_even_if_primary_idle():
    """Xbox-only play: Apple TV idle + Samsung off + secondary active → the
    warn/countdown voices must still fire."""
    assert ma_mod.someone_could_be_watching(
        "apple_tv", "idle", "off", secondary_active=True) is True


def test_someone_watching_false_when_nothing_active():
    assert ma_mod.someone_could_be_watching(
        "apple_tv", "idle", "off", secondary_active=False) is False


def test_someone_watching_secondary_defaults_false_back_compat():
    """The new param defaults False so existing callers are unchanged."""
    assert ma_mod.someone_could_be_watching("apple_tv", "playing", None) is True
    assert ma_mod.someone_could_be_watching("apple_tv", "idle", "off") is False


# ===========================================================================
# enforcer — dual enforcement fan-out
# ===========================================================================


def _make_enforcer(profile, *, turn_off_results=None, switch_raises=False):
    hass = MagicMock()
    hass.services.async_call = AsyncMock(
        side_effect=RuntimeError("switch down") if switch_raises else None
    )
    adguard = MagicMock()
    adguard.set_blocked = AsyncMock(return_value=None)
    adguard.get_client = AsyncMock(return_value={
        "blocked_services": [], "use_global_blocked_services": True})
    store = MagicMock()
    store.is_adult_mode_active = MagicMock(return_value=False)
    store.adult_mode_until = MagicMock(return_value=None)
    e = enforcer_mod.EnforcementController(hass, adguard, profile, store=store)

    results = list(turn_off_results or [True, True])
    n = {"i": 0}

    async def fake_turn_off(entity_id, *, label):
        i = n["i"]
        n["i"] += 1
        return results[i] if i < len(results) else results[-1]

    e._call_turn_off_with_verify = fake_turn_off
    e._maybe_announce_enforce = AsyncMock()
    return e, hass


def _switch_off_calls(hass):
    return [
        c for c in hass.services.async_call.call_args_list
        if c.args[:3] == ("switch", "turn_off", {"entity_id": XBOX_SW})
    ]


def _switch_on_calls(hass):
    return [
        c for c in hass.services.async_call.call_args_list
        if c.args[:3] == ("switch", "turn_on", {"entity_id": XBOX_SW})
    ]


def test_enter_enforcing_fans_block_out_to_secondary_switch():
    p = _make_merge_profile()
    e, hass = _make_enforcer(p, turn_off_results=[True, True])
    asyncio.run(e._enter_enforcing())
    assert len(_switch_off_calls(hass)) == 1, "secondary switch.turn_off not fired"


def test_exit_enforcing_restores_secondary_switch():
    p = _make_merge_profile()
    e, hass = _make_enforcer(p)
    e._is_blocked = True  # a real block was in effect
    e._state = STATE_ENFORCING
    asyncio.run(e._exit_enforcing(reason="under_budget"))
    assert len(_switch_on_calls(hass)) == 1, "secondary switch.turn_on not fired"


def test_secondary_switch_failure_does_not_mark_enforcement_failed():
    """A secondary switch error is log-only — the Apple TV + Samsung kill is the
    user-visible enforcement, so a partial block must not flip the failed flag."""
    p = _make_merge_profile()
    e, hass = _make_enforcer(p, turn_off_results=[True, True], switch_raises=True)
    asyncio.run(e._enter_enforcing())
    assert e._last_enforcement_failed is False


def test_adult_mode_suppresses_secondary_block():
    """Adult-mode bypass must not flip the Xbox switch (no enforcement side
    effects at all)."""
    p = _make_merge_profile()
    e, hass = _make_enforcer(p)
    e._store.is_adult_mode_active = MagicMock(return_value=True)
    asyncio.run(e._enter_enforcing())
    assert _switch_off_calls(hass) == []


def test_reassert_reasserts_secondary_switch_drift():
    """Job 3 anti-defeat: the kid flips the Xbox switch back ON mid-block →
    reassert re-asserts it OFF (even though _is_blocked shows no drift)."""
    p = _make_merge_profile()
    e, hass = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._is_blocked = True  # AdGuard flag agrees → Job 1 sees no drift
    # Every states.get returns "on" → the switch reads on (kid re-enabled it);
    # the Apple TV also reads "on" (not actively playing) so Job 2 is a no-op.
    hass.states.get = lambda eid: _state("on")
    asyncio.run(e.reassert())
    assert len(_switch_off_calls(hass)) >= 1, "drift re-assert did not fire"


def test_reassert_no_secondary_action_when_switch_already_off():
    p = _make_merge_profile()
    e, hass = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._is_blocked = True
    hass.states.get = lambda eid: _state("off")
    asyncio.run(e.reassert())
    assert _switch_off_calls(hass) == []


# --- review fix C: false enforcing_failed on Xbox-only enforcement -----------


def test_xbox_only_enforcement_not_marked_failed_when_primary_already_off():
    """Headline scenario: kid on the Xbox, Apple TV + Samsung already off →
    both primary turn_offs report failure (targets already off), but the Xbox
    switch IS cut. Must NOT read as enforcing_failed."""
    p = _make_merge_profile()
    # Both primary turn_offs "fail" (already-off targets), secondary succeeds.
    e, hass = _make_enforcer(p, turn_off_results=[False, False])
    asyncio.run(e._enter_enforcing())
    assert len(_switch_off_calls(hass)) == 1
    assert e._last_enforcement_failed is False


def test_single_device_still_marked_failed_when_turn_off_fails():
    """Regression guard for fix C: a profile with NO secondaries still reports
    enforcing_failed when the only kill path fails (secondary_block_ok=False
    must not mask a real single-device failure)."""
    p = _make_merge_profile(secondaries=[], tv_shutdown_target=None)
    e, hass = _make_enforcer(p, turn_off_results=[False])
    asyncio.run(e._enter_enforcing())
    assert e._last_enforcement_failed is True


# --- review fix B: secondary unblock runs for an xbox-PRIMARY profile too ----


def test_exit_restores_secondary_for_xbox_primary_profile():
    """An xbox_presence-PRIMARY profile that also has a (second) secondary must
    still restore it on exit — the xbox branch early-returns, so the unblock
    has to run before the dispatch."""
    p = _make_merge_profile(
        device_kind="xbox_presence",
        apple_tv_entity_id=XBOX_TRACKER,
        enforcement_switch_entity_id="switch.primary_xbox",
        secondaries=[_secondary_xbox()],
        tv_shutdown_target=None,
    )
    e, hass = _make_enforcer(p)
    e._is_blocked = True
    e._state = STATE_ENFORCING
    asyncio.run(e._exit_enforcing(reason="under_budget"))
    assert len(_switch_on_calls(hass)) == 1, "secondary not restored for xbox-primary"


# --- review fix D: seed back-compat for a missing primary --------------------


def test_seed_skips_when_primary_missing_and_no_secondaries():
    """Single-device profile, primary entity absent at seed → no store writes
    (byte-identical to pre-v0.20.0; the stale logic owns recovery)."""
    p = _make_merge_profile(secondaries=[])
    c = _Coord(p, states={})  # primary returns None
    c._fire_app_ended = MagicMock()
    asyncio.run(c._seed_from_current_state())
    c._store.open_event.assert_not_called()
    c._store.close_open_event.assert_not_called()


def test_seed_opens_xbox_when_primary_missing_but_secondary_home():
    """Merged profile: primary entity absent but Xbox home → seed still opens
    the gaming event (in-progress Xbox session picked up on restart)."""
    p = _make_merge_profile()
    c = _Coord(p, states={XBOX_TRACKER: _state("home")})
    asyncio.run(c._seed_from_current_state())
    c._store.open_event.assert_called_once()


# --- review fix A: re-subscribe the state listener on live attach ------------


def test_resubscribe_watched_includes_secondary_entities():
    """After a live PATCH-attach, resubscribe_watched_entities re-wires the
    state listener to include the secondary's entity (not just the primary)."""
    p = _make_merge_profile()
    captured = {}

    def fake_track(hass, entities, cb):
        captured["entities"] = list(entities)
        return lambda: captured.__setitem__("unsubbed", True)

    orig = coord_mod.async_track_state_change_event
    coord_mod.async_track_state_change_event = fake_track
    try:
        c = _Coord(p, states={})
        c._unsub_state = None
        coord_mod.AppleTVMgmtCoordinator.resubscribe_watched_entities(c)
    finally:
        coord_mod.async_track_state_change_event = orig
    assert "media_player.heimkinoaaa" in captured["entities"]
    assert XBOX_TRACKER in captured["entities"]
