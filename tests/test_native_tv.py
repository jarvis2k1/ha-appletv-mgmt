"""v0.21.0 — native TV watching ("Live TV") as a first-class tracked activity.

Native TV = the display is ON but the input is NOT one of the tracked devices
(Apple TV on HDMI1, Xbox on HDMI2/DVI) — tuner / broadcast / SCART / a
smart-TV app. Booked to the synthetic bundle "tv.native" → group "linear_tv"
under the same room budget. Ships disabled (track_native_tv=False).

Coverage (mirrors the spec test matrix):
  - media_attribution.native_tv_is_active — the pure source/state rule.
  - coordinator._resolve_room_activity — precedence (apple_tv > secondary >
    native) + the fail-closed / excluded-source / feature-off cases, and that
    TV-off closes the open tv.native event.
  - coordinator._watched_entity_ids / _native_tv_entity_id — the TV entity is
    watched only when the feature is on.
  - enforcer._enter_enforcing — native TV turns the TV off UNCONDITIONALLY
    (tv_shutdown switch bypassed) and skips AdGuard + Apple-TV sleep.
  - enforcer.reassert — Job 4 anti-defeat re-fires on a native source, NOT on
    an excluded (HDMI1) source.

Same stub-the-world import pattern as test_merge_unified_profile.py — test file
is self-contained.
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

NOW = datetime(2026, 7, 12, 20, 30, 0, tzinfo=timezone.utc)


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
            getattr(core, _n).__class_getitem__ = classmethod(
                lambda cls, item: cls
            )
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
cat_mod = sys.modules["custom_components.appletv_mgmt.categorize"]

STATE_OK = const_mod.STATE_OK
STATE_ENFORCING = const_mod.STATE_ENFORCING
NATIVE_TV_BUNDLE_ID = cat_mod.NATIVE_TV_BUNDLE_ID

APPLE_TV = "media_player.living_room_apple_tv"
SAMSUNG = "media_player.samsung_tv"
XBOX_TRACKER = "device_tracker.xboxone"
XBOX_BUNDLE = "xbox.console"


def _secondary_xbox():
    return {
        "entity_id": XBOX_TRACKER,
        "device_kind": "xbox_presence",
        "enforcement_switch_entity_id": "switch.xboxone_internet_access",
        "bundle_id": XBOX_BUNDLE,
    }


def _make_native_profile(*, track_native_tv=True, secondaries=None,
                         excluded=None, tv_shutdown_target=None,
                         tv_entity_id=SAMSUNG, **overrides):
    """Living Room profile with native-TV tracking on (default). tv_shutdown_
    target defaults None → the tv_shutdown switch is OFF, so the enforcement
    tests prove native TV kills the TV regardless of that switch."""
    defaults = dict(
        id="lr-1",
        display_name="Living Room",
        apple_tv_entity_id=APPLE_TV,
        adguard_client_name="AppleTV",
        daily_budget_min=60,
        grace_seconds=60,
        warn_thresholds_min=[5],
        idle_grace_minutes=5,
        stale_session_minutes=5,
        device_kind="apple_tv",
        tv_entity_id=tv_entity_id,
        tv_shutdown_target=tv_shutdown_target,
        track_native_tv=track_native_tv,
        secondary_devices=([] if secondaries is None else secondaries),
    )
    if excluded is not None:
        defaults["native_tv_excluded_sources"] = excluded
    defaults.update(overrides)
    return storage_mod.Profile(**defaults)


def _state(state_str, last_updated=NOW, attrs=None):
    s = MagicMock()
    s.state = state_str
    s.last_updated = last_updated
    s.attributes = attrs or {}
    return s


# ===========================================================================
# media_attribution.native_tv_is_active — the pure rule
# ===========================================================================

EXCL = ["HDMI1", "HDMI2/DVI"]


def test_native_active_when_on_with_nontracked_source():
    assert ma_mod.native_tv_is_active(
        track_native_tv=True, tv_configured=True,
        tv_state="on", source="TV", excluded_sources=EXCL) is True
    # SCART is also native (not in the excluded list).
    assert ma_mod.native_tv_is_active(
        track_native_tv=True, tv_configured=True,
        tv_state="on", source="SCART", excluded_sources=EXCL) is True


def test_native_false_when_source_missing_fails_closed():
    assert ma_mod.native_tv_is_active(
        track_native_tv=True, tv_configured=True,
        tv_state="on", source=None, excluded_sources=EXCL) is False
    # Empty string is treated as missing (fail closed) too.
    assert ma_mod.native_tv_is_active(
        track_native_tv=True, tv_configured=True,
        tv_state="on", source="", excluded_sources=EXCL) is False


def test_native_false_when_source_excluded():
    assert ma_mod.native_tv_is_active(
        track_native_tv=True, tv_configured=True,
        tv_state="on", source="HDMI1", excluded_sources=EXCL) is False
    assert ma_mod.native_tv_is_active(
        track_native_tv=True, tv_configured=True,
        tv_state="on", source="HDMI2/DVI", excluded_sources=EXCL) is False


def test_native_false_when_feature_off_or_no_tv():
    assert ma_mod.native_tv_is_active(
        track_native_tv=False, tv_configured=True,
        tv_state="on", source="TV", excluded_sources=EXCL) is False
    assert ma_mod.native_tv_is_active(
        track_native_tv=True, tv_configured=False,
        tv_state="on", source="TV", excluded_sources=EXCL) is False


def test_native_false_when_tv_not_on():
    for st in ("off", "standby", "idle", "unavailable", None):
        assert ma_mod.native_tv_is_active(
            track_native_tv=True, tv_configured=True,
            tv_state=st, source="TV", excluded_sources=EXCL) is False


def test_native_none_excluded_list_defaults_to_no_exclusions():
    # Robustness: excluded_sources=None → nothing excluded (still needs on + src).
    assert ma_mod.native_tv_is_active(
        track_native_tv=True, tv_configured=True,
        tv_state="on", source="HDMI1", excluded_sources=None) is True


# ===========================================================================
# coordinator._resolve_room_activity — precedence + native branch
# ===========================================================================


class _Coord:
    _resolve_room_activity = coord_mod.AppleTVMgmtCoordinator._resolve_room_activity
    _effective_bundle_id = coord_mod.AppleTVMgmtCoordinator._effective_bundle_id
    _extract_activity_signal = coord_mod.AppleTVMgmtCoordinator._extract_activity_signal
    _entity_is_stale = coord_mod.AppleTVMgmtCoordinator._entity_is_stale
    _secondary_entity_ids = coord_mod.AppleTVMgmtCoordinator._secondary_entity_ids
    _native_tv_entity_id = coord_mod.AppleTVMgmtCoordinator._native_tv_entity_id
    _watched_entity_ids = coord_mod.AppleTVMgmtCoordinator._watched_entity_ids
    _maybe_warn_native_source_list_mismatch = (
        coord_mod.AppleTVMgmtCoordinator._maybe_warn_native_source_list_mismatch
    )
    _sync_open_event = coord_mod.AppleTVMgmtCoordinator._sync_open_event
    _check_for_stale_session = coord_mod.AppleTVMgmtCoordinator._check_for_stale_session
    _fire_app_started = coord_mod.AppleTVMgmtCoordinator._fire_app_started
    _fire_app_ended = coord_mod.AppleTVMgmtCoordinator._fire_app_ended

    def _handle_state_change(self, event):
        return None

    def __init__(self, profile, *, states):
        self._profile = profile
        self._last_known_bundle_id = None
        self._last_known_seen_at = None
        self._native_source_list_checked = False  # v0.21.1 (FIX 2b)
        self._unsub_state = None
        self.hass = MagicMock()
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


def test_resolve_native_active_when_tv_on_source_tv():
    p = _make_native_profile()
    states = {
        APPLE_TV: _state("off"),
        SAMSUNG: _state("on", attrs={"source": "TV"}),
    }
    c = _Coord(p, states=states)
    eff, sec = c._resolve_room_activity(NOW)
    assert eff == NATIVE_TV_BUNDLE_ID
    # from_secondary=True so the pyatv staleness gate is bypassed downstream.
    assert sec is True


def test_resolve_apple_tv_wins_over_native():
    """Apple TV playing + TV somehow reporting a native source → Apple TV owns
    the room (precedence apple_tv > native)."""
    p = _make_native_profile()
    states = {
        APPLE_TV: _state("playing", attrs={"app_id": "com.netflix.Netflix"}),
        SAMSUNG: _state("on", attrs={"source": "TV"}),
    }
    c = _Coord(p, states=states)
    eff, sec = c._resolve_room_activity(NOW)
    assert (eff, sec) == ("com.netflix.Netflix", False)


def test_resolve_xbox_wins_over_native():
    """Xbox home + TV reporting native source → the Xbox (secondary) wins over
    native TV (precedence apple_tv > secondary > native)."""
    p = _make_native_profile(secondaries=[_secondary_xbox()])
    states = {
        APPLE_TV: _state("off"),
        XBOX_TRACKER: _state("home"),
        SAMSUNG: _state("on", attrs={"source": "TV"}),
    }
    c = _Coord(p, states=states)
    eff, sec = c._resolve_room_activity(NOW)
    assert eff == XBOX_BUNDLE
    assert sec is True


def test_resolve_no_native_when_source_missing():
    p = _make_native_profile()
    states = {
        APPLE_TV: _state("off"),
        SAMSUNG: _state("on", attrs={}),  # no source attribute → fail closed
    }
    c = _Coord(p, states=states)
    assert c._resolve_room_activity(NOW) == (None, False)


def test_resolve_no_native_when_source_excluded():
    p = _make_native_profile()
    states = {
        APPLE_TV: _state("off"),
        SAMSUNG: _state("on", attrs={"source": "HDMI1"}),  # Apple TV input
    }
    c = _Coord(p, states=states)
    assert c._resolve_room_activity(NOW) == (None, False)


def test_resolve_no_native_when_feature_off():
    p = _make_native_profile(track_native_tv=False)
    states = {
        APPLE_TV: _state("off"),
        SAMSUNG: _state("on", attrs={"source": "TV"}),
    }
    c = _Coord(p, states=states)
    assert c._resolve_room_activity(NOW) == (None, False)


def test_resolve_none_when_tv_off_closes_open_native_event():
    """TV off → resolver returns None → _sync_open_event closes the open
    tv.native event."""
    p = _make_native_profile()
    open_evt = MagicMock(
        id="evt", bundle_id=NATIVE_TV_BUNDLE_ID,
        started_at=NOW - timedelta(minutes=10),
    )
    states = {APPLE_TV: _state("off"), SAMSUNG: _state("off")}
    c = _Coord(p, states=states)
    c._store.open_event_for.return_value = open_evt
    c._store.close_open_event.return_value = open_evt
    c._fire_app_ended = MagicMock()
    eff, sec = c._resolve_room_activity(NOW)
    assert eff is None
    asyncio.run(c._sync_open_event(effective=eff, from_secondary=sec, now=NOW))
    c._store.close_open_event.assert_called_once()


def test_stale_session_does_not_close_open_native_event():
    """Apple-TV pyatv freeze must NOT close an open native-TV event (the TV
    entity is authoritative for it)."""
    p = _make_native_profile()
    open_evt = MagicMock(
        id="evt", bundle_id=NATIVE_TV_BUNDLE_ID,
        started_at=NOW - timedelta(minutes=15),
    )
    states = {
        APPLE_TV: _state("playing", last_updated=NOW - timedelta(minutes=30)),
        SAMSUNG: _state("on", attrs={"source": "TV"}),
    }
    c = _Coord(p, states=states)
    c._store.open_event_for.return_value = open_evt
    asyncio.run(c._check_for_stale_session(now=NOW))
    c._store.close_open_event.assert_not_called()


# ===========================================================================
# coordinator watched-entities wiring
# ===========================================================================


def test_watched_includes_tv_entity_when_native_on():
    p = _make_native_profile()
    c = _Coord(p, states={})
    assert SAMSUNG in c._watched_entity_ids()
    assert c._native_tv_entity_id() == SAMSUNG


def test_watched_excludes_tv_entity_when_native_off():
    p = _make_native_profile(track_native_tv=False)
    c = _Coord(p, states={})
    assert SAMSUNG not in c._watched_entity_ids()
    assert c._native_tv_entity_id() is None


# ===========================================================================
# enforcer — native TV enforcement + anti-defeat
# ===========================================================================


def _make_enforcer(profile, *, states, turn_off_result=True):
    hass = MagicMock()
    hass.services.async_call = AsyncMock(return_value=None)
    hass.states.get = lambda eid: states.get(eid)
    adguard = MagicMock()
    adguard.set_blocked = AsyncMock(return_value=None)
    store = MagicMock()
    store.is_adult_mode_active = MagicMock(return_value=False)
    store.adult_mode_until = MagicMock(return_value=None)
    e = enforcer_mod.EnforcementController(hass, adguard, profile, store=store)

    calls: list[tuple[str, str]] = []

    async def fake_turn_off(entity_id, *, label):
        calls.append((entity_id, label))
        return turn_off_result

    e._call_turn_off_with_verify = fake_turn_off
    e._maybe_announce_enforce = AsyncMock()
    return e, hass, adguard, calls


def test_enter_enforcing_native_kills_tv_unconditionally():
    """Native TV active + linear_tv budget exhausted → media_player.turn_off on
    the TV, even though tv_shutdown_target is None (the switch is OFF). AdGuard
    is enabled but NOT called (native path skips it)."""
    p = _make_native_profile(tv_shutdown_target=None, enable_adguard_block=True)
    states = {
        APPLE_TV: _state("off"),
        SAMSUNG: _state("on", attrs={"source": "TV"}),
    }
    e, hass, adguard, calls = _make_enforcer(p, states=states)
    asyncio.run(e._enter_enforcing())
    tv_calls = [c for c in calls if c[0] == SAMSUNG]
    assert len(tv_calls) == 1, f"TV turn_off not fired; calls={calls}"
    # AdGuard + Apple-TV sleep are skipped on the native path.
    adguard.set_blocked.assert_not_called()
    assert all(c[0] != APPLE_TV for c in calls), "Apple TV sleep should be skipped"
    assert e._last_enforcement_failed is False
    assert e._is_blocked is True


def test_enter_enforcing_native_failed_flag_when_turn_off_fails():
    p = _make_native_profile()
    states = {
        APPLE_TV: _state("off"),
        SAMSUNG: _state("on", attrs={"source": "TV"}),
    }
    e, hass, adguard, calls = _make_enforcer(p, states=states, turn_off_result=False)
    asyncio.run(e._enter_enforcing())
    assert e._last_enforcement_failed is True
    e._maybe_announce_enforce.assert_not_awaited()


def test_enter_enforcing_falls_through_to_apple_path_when_not_native():
    """When native TV is NOT active (TV on the Apple TV input), the native
    branch is skipped and the normal Apple-TV/AdGuard path runs."""
    p = _make_native_profile(enable_adguard_block=True)
    states = {
        APPLE_TV: _state("playing", attrs={"app_id": "com.disney.disneyplus"}),
        SAMSUNG: _state("on", attrs={"source": "HDMI1"}),  # Apple TV input
    }
    e, hass, adguard, calls = _make_enforcer(p, states=states)
    asyncio.run(e._enter_enforcing())
    # Normal path: AdGuard blocked + Apple TV turn_off attempted.
    adguard.set_blocked.assert_awaited_once()
    assert any(c[0] == APPLE_TV for c in calls)


def test_reassert_native_watchdog_refires_on_native_source():
    """Anti-defeat: kid turns the TV back on with a native source under
    ENFORCING → reassert re-fires media_player.turn_off on the TV."""
    p = _make_native_profile()
    states = {
        APPLE_TV: _state("on"),  # not actively playing → pyatv watchdog no-op
        SAMSUNG: _state("on", attrs={"source": "TV"}),
    }
    e, hass, adguard, calls = _make_enforcer(p, states=states)
    e._state = STATE_ENFORCING
    e._is_blocked = True  # AdGuard flag agrees → Job 1 sees no drift
    asyncio.run(e.reassert())
    assert any(c[0] == SAMSUNG for c in calls), "native watchdog did not re-fire"


def test_reassert_native_watchdog_silent_on_excluded_source():
    """The kid switches the TV back to HDMI1 (Apple TV) under ENFORCING → NOT
    native, so the native watchdog stays quiet (no TV turn_off)."""
    p = _make_native_profile()
    states = {
        APPLE_TV: _state("on"),  # not actively playing → pyatv watchdog no-op
        SAMSUNG: _state("on", attrs={"source": "HDMI1"}),
    }
    e, hass, adguard, calls = _make_enforcer(p, states=states)
    e._state = STATE_ENFORCING
    e._is_blocked = True
    asyncio.run(e.reassert())
    assert all(c[0] != SAMSUNG for c in calls), "native watchdog fired on HDMI1"


def test_reassert_native_watchdog_off_when_feature_disabled():
    p = _make_native_profile(track_native_tv=False)
    states = {
        APPLE_TV: _state("on"),
        SAMSUNG: _state("on", attrs={"source": "TV"}),
    }
    e, hass, adguard, calls = _make_enforcer(p, states=states)
    e._state = STATE_ENFORCING
    e._is_blocked = True
    asyncio.run(e.reassert())
    assert all(c[0] != SAMSUNG for c in calls)


# ===========================================================================
# v0.21.1 FIX 1 — native-TV kill must respect WHY the room is enforcing.
# native TV is constrained only by room-wide limits (daily / quiet / sleep —
# non-"group:" reasons) or by its OWN cap ("group:linear_tv"). A SIBLING
# group's exhaustion (e.g. "group:movies") must NOT power off the TV.
# ===========================================================================


def test_native_should_enforce_gate_matrix():
    """The pure `_native_tv_should_enforce` gate, decoupled from side effects."""
    p = _make_native_profile()
    states = {
        APPLE_TV: _state("off"),
        SAMSUNG: _state("on", attrs={"source": "TV"}),  # native active
    }
    e, *_ = _make_enforcer(p, states=states)
    # Room-wide / own-cap reasons ⇒ enforce.
    for reason in (None, "", "daily_limit", "quiet:Bedtime", "sleep:22-07",
                   "group:linear_tv"):
        e._enforce_reason = reason
        assert e._native_tv_should_enforce() is True, f"reason={reason!r}"
    # Sibling group reasons ⇒ do NOT enforce (unlimited native keeps tracking).
    for reason in ("group:movies", "group:gaming", "group:tv_shows", "group:other"):
        e._enforce_reason = reason
        assert e._native_tv_should_enforce() is False, f"reason={reason!r}"
    # Gate is False whenever native isn't active, regardless of reason.
    states[SAMSUNG] = _state("on", attrs={"source": "HDMI1"})  # Apple TV input
    e._enforce_reason = "daily_limit"
    assert e._native_tv_should_enforce() is False


def test_native_enforces_when_own_cap_spent_under_sibling_reason():
    """v0.21.3 regression — live-reported 2026-08-17.

    linear_tv (158min/30) AND other (204min/60) were BOTH exhausted. The
    binding picker named the sibling ("group:other"), and the reason-only
    gate spared the TV — so Live TV ran ~5x past its own cap indefinitely,
    with enforcement_failed=False, i.e. silently. An exhausted own cap must
    win over a sibling reason.
    """
    p = _make_native_profile()
    states = {
        APPLE_TV: _state("off"),
        SAMSUNG: _state("on", attrs={"source": "TV"}),  # native active
    }
    e, *_ = _make_enforcer(p, states=states)

    # Sibling reason + native still in credit ⇒ spare (v0.21.1 intent kept).
    e._native_group_exhausted = False
    for reason in ("group:other", "group:movies", "group:gaming"):
        e._enforce_reason = reason
        assert e._native_tv_should_enforce() is False, f"reason={reason!r}"

    # Same sibling reasons, but native's OWN cap is spent ⇒ must enforce.
    e._native_group_exhausted = True
    for reason in ("group:other", "group:movies", "group:gaming"):
        e._enforce_reason = reason
        assert e._native_tv_should_enforce() is True, f"reason={reason!r}"


def test_binding_reason_prefers_current_group_when_it_is_exhausted():
    """v0.21.3 regression — the reason picker must not name an arbitrary
    sibling while the kid sits in an exhausted current_group.

    Reproduces the live totals. `group_totals_seconds` is ordered so the
    sibling ("other") is iterated FIRST — the pre-fix scan broke on it and
    reported "group:other" while the kid was watching linear_tv.
    """
    p = _make_native_profile()
    states = {
        APPLE_TV: _state("off"),
        SAMSUNG: _state("on", attrs={"source": "TV"}),
    }
    e, *_ = _make_enforcer(p, states=states)

    asyncio.run(e.evaluate(
        used_seconds=int(367.3 * 60),
        now=NOW,
        current_group="linear_tv",
        current_group_used_seconds=int(158.1 * 60),
        current_group_budget_seconds=30 * 60,
        # "other" deliberately first — the failing iteration order.
        group_totals_seconds={
            "other": int(203.6 * 60),
            "linear_tv": int(158.1 * 60),
            "gaming": int(6.2 * 60),
        },
        group_budgets_seconds={
            "other": 60 * 60, "linear_tv": 30 * 60, "gaming": 30 * 60,
        },
    ))

    assert e.enforce_reason == "group:linear_tv", (
        f"Expected the exhausted CURRENT group to bind, got {e.enforce_reason!r}. "
        "A sibling reason here suppresses the native-TV kill."
    )
    assert e._native_group_exhausted is True
    assert e._native_tv_should_enforce() is True


def test_enter_enforcing_native_sibling_group_does_not_kill_tv():
    """FIX 1: room enforcing for a SIBLING group (movies) while native TV is on
    → the TV is NOT powered off; control falls through to the normal path."""
    p = _make_native_profile(enable_adguard_block=True)
    states = {
        APPLE_TV: _state("off"),
        SAMSUNG: _state("on", attrs={"source": "TV"}),
    }
    e, hass, adguard, calls = _make_enforcer(p, states=states)
    e._enforce_reason = "group:movies"
    asyncio.run(e._enter_enforcing())
    assert all(c[0] != SAMSUNG for c in calls), "sibling-group enforce killed the TV"
    # The normal enforcement path ran instead (AdGuard blocked).
    adguard.set_blocked.assert_awaited_once()


def test_enter_enforcing_native_linear_tv_cap_kills_tv():
    """FIX 1: enforcing for native TV's OWN cap ("group:linear_tv") → TV off."""
    p = _make_native_profile()
    states = {
        APPLE_TV: _state("off"),
        SAMSUNG: _state("on", attrs={"source": "TV"}),
    }
    e, hass, adguard, calls = _make_enforcer(p, states=states)
    e._enforce_reason = "group:linear_tv"
    asyncio.run(e._enter_enforcing())
    assert any(c[0] == SAMSUNG for c in calls), "linear_tv cap did not kill the TV"
    adguard.set_blocked.assert_not_called()


def test_enter_enforcing_native_room_wide_reasons_kill_tv():
    """FIX 1: room-wide reasons (daily / quiet / sleep / None) → TV off."""
    for reason in ("daily_limit", "quiet:Bedtime", "sleep:22-07", None):
        p = _make_native_profile()
        states = {
            APPLE_TV: _state("off"),
            SAMSUNG: _state("on", attrs={"source": "TV"}),
        }
        e, hass, adguard, calls = _make_enforcer(p, states=states)
        e._enforce_reason = reason
        asyncio.run(e._enter_enforcing())
        assert any(c[0] == SAMSUNG for c in calls), f"reason={reason!r} spared the TV"


def test_reassert_native_watchdog_silent_on_sibling_group():
    """FIX 1: reassert Job 4 obeys the same gate — a sibling-group enforce must
    not re-kill an unlimited native TV."""
    p = _make_native_profile()
    states = {
        APPLE_TV: _state("on"),  # not actively playing → pyatv watchdog no-op
        SAMSUNG: _state("on", attrs={"source": "TV"}),
    }
    e, hass, adguard, calls = _make_enforcer(p, states=states)
    e._state = STATE_ENFORCING
    e._is_blocked = True
    e._enforce_reason = "group:movies"
    asyncio.run(e.reassert())
    assert all(c[0] != SAMSUNG for c in calls), "sibling-group watchdog killed the TV"


def test_reassert_native_watchdog_refires_on_linear_tv_cap():
    """FIX 1: reassert Job 4 still fires for native TV's own cap."""
    p = _make_native_profile()
    states = {
        APPLE_TV: _state("on"),
        SAMSUNG: _state("on", attrs={"source": "TV"}),
    }
    e, hass, adguard, calls = _make_enforcer(p, states=states)
    e._state = STATE_ENFORCING
    e._is_blocked = True
    e._enforce_reason = "group:linear_tv"
    asyncio.run(e.reassert())
    assert any(c[0] == SAMSUNG for c in calls), "linear_tv-cap watchdog did not re-fire"


# ===========================================================================
# v0.21.1 FIX 2 — case/whitespace-insensitive source matching + misconfig warn.
# ===========================================================================


def test_native_source_casefold_and_whitespace_treated_as_excluded():
    """FIX 2a: case + surrounding-whitespace variants of an excluded source are
    still excluded (NOT native)."""
    for src in ("hdmi1", " HDMI1 ", "Hdmi1", "HDMI2/dvi", "  hdmi2/DVI  "):
        assert ma_mod.native_tv_is_active(
            track_native_tv=True, tv_configured=True,
            tv_state="on", source=src, excluded_sources=EXCL) is False, src


def test_native_source_internal_space_still_mismatches():
    """FIX 2a: internal spacing is NOT normalized — "HDMI 1" != "HDMI1" so it
    reads as native. Documents the limitation the 2b warning exists to surface."""
    assert ma_mod.native_tv_is_active(
        track_native_tv=True, tv_configured=True,
        tv_state="on", source="HDMI 1", excluded_sources=EXCL) is True


def test_excluded_sources_match_source_list_predicate():
    """FIX 2b: the pure predicate — True iff a normalized excluded source appears
    in the normalized source_list."""
    f = ma_mod.excluded_sources_match_source_list
    # casefold + strip match.
    assert f(["HDMI1"], ["TV", "hdmi1", "HDMI2"]) is True
    assert f([" HDMI2/DVI "], ["TV", "HDMI2/DVI"]) is True
    # internal-space mismatch, no match.
    assert f(["HDMI1", "HDMI2/DVI"], ["TV", "HDMI 1", "Component"]) is False
    # empty either side ⇒ False.
    assert f([], ["HDMI1"]) is False
    assert f(["HDMI1"], []) is False
    assert f(["HDMI1"], None) is False
    assert f(None, ["HDMI1"]) is False


def _patch_logger(monkeypatch):
    warnings: list = []
    logger = MagicMock()
    logger.warning = lambda *a, **k: warnings.append(a)
    monkeypatch.setattr(coord_mod, "_LOGGER", logger)
    return warnings


def test_coord_warns_once_on_source_list_mismatch(monkeypatch):
    """FIX 2b: when NO configured excluded source matches the TV's source_list,
    log exactly one warning; the check is one-shot."""
    p = _make_native_profile(excluded=["HDMI1", "HDMI2/DVI"])
    states = {
        SAMSUNG: _state("on", attrs={
            "source": "TV",
            "source_list": ["TV", "HDMI 1", "Component"],  # internal space ⇒ no match
        }),
    }
    c = _Coord(p, states=states)
    warnings = _patch_logger(monkeypatch)
    c._maybe_warn_native_source_list_mismatch()
    c._maybe_warn_native_source_list_mismatch()  # 2nd call must be a no-op
    assert len(warnings) == 1
    assert c._native_source_list_checked is True


def test_coord_no_warn_when_source_list_matches(monkeypatch):
    """FIX 2b: a casefold match anywhere in source_list ⇒ no warning (but the
    one-shot flag is still set — we evaluated a real source_list)."""
    p = _make_native_profile(excluded=["HDMI1"])
    states = {
        SAMSUNG: _state("on", attrs={
            "source": "TV", "source_list": ["TV", "hdmi1"],
        }),
    }
    c = _Coord(p, states=states)
    warnings = _patch_logger(monkeypatch)
    c._maybe_warn_native_source_list_mismatch()
    assert warnings == []
    assert c._native_source_list_checked is True


def test_coord_retries_when_source_list_missing(monkeypatch):
    """FIX 2b: a missing/empty source_list is not a mismatch — skip and retry on
    a later tick (do NOT consume the one-shot)."""
    p = _make_native_profile(excluded=["HDMI1"])
    states = {SAMSUNG: _state("on", attrs={"source": "TV"})}  # no source_list
    c = _Coord(p, states=states)
    warnings = _patch_logger(monkeypatch)
    c._maybe_warn_native_source_list_mismatch()
    assert warnings == []
    assert c._native_source_list_checked is False  # retried next tick


def test_coord_no_warn_when_feature_off(monkeypatch):
    """FIX 2b: feature off ⇒ never warn (and don't consume the one-shot)."""
    p = _make_native_profile(track_native_tv=False, excluded=["HDMI1"])
    states = {
        SAMSUNG: _state("on", attrs={
            "source": "TV", "source_list": ["Component"],
        }),
    }
    c = _Coord(p, states=states)
    warnings = _patch_logger(monkeypatch)
    c._maybe_warn_native_source_list_mismatch()
    assert warnings == []
    assert c._native_source_list_checked is False


# ===========================================================================
# v0.21.1 FIX 3 — native-TV fields are PATCH/panel-authoritative, NOT in the
# options flow (removing the silent-no-op clobber).
# ===========================================================================


def test_config_flow_options_omit_native_tv_fields():
    """FIX 3: track_native_tv + native_tv_excluded_sources must be gone from the
    config-flow options schema (they are panel/PATCH-only). Guard against re-add.
    budget_linear_tv stays (per-group budgets loop) — asserted present."""
    src = (PKG / "config_flow.py").read_text()
    assert "CONF_TRACK_NATIVE_TV" not in src
    assert "CONF_NATIVE_TV_EXCLUDED_SOURCES" not in src
    assert "DEFAULT_TRACK_NATIVE_TV" not in src
    assert "DEFAULT_NATIVE_TV_EXCLUDED_SOURCES" not in src
    # linear_tv still flows through the generic ALL_GROUPS budget loop.
    assert "ALL_GROUPS" in src


def test_translations_omit_native_tv_option_labels():
    """FIX 3: the two option labels are removed from en.json + de.json; the
    budget_linear_tv label stays."""
    import json
    for fn in ("en.json", "de.json"):
        data = json.loads((PKG / "translations" / fn).read_text())
        init_data = data["options"]["step"]["init"]["data"]
        assert "track_native_tv" not in init_data
        assert "native_tv_excluded_sources" not in init_data
        assert "budget_linear_tv" in init_data


# ===========================================================================
# v0.21.1 FIX 4 — native session runaway backstop (frozen TV over-counts).
# ===========================================================================


def test_native_event_closed_past_runaway_ceiling():
    """FIX 4: a native event open past the runaway ceiling with the TV still
    "on" gets force-closed (bounded), not accrued indefinitely."""
    p = _make_native_profile()
    ceiling_s = ma_mod.STALE_RUNAWAY_CEILING_S
    open_evt = MagicMock(
        id="evt", bundle_id=NATIVE_TV_BUNDLE_ID,
        started_at=NOW - timedelta(seconds=ceiling_s + 60),  # past the cap
    )
    states = {
        APPLE_TV: _state("off"),
        SAMSUNG: _state("on", attrs={"source": "TV"}),  # TV stuck on
    }
    c = _Coord(p, states=states)
    c._store.open_event_for.return_value = open_evt
    c._store.close_open_event.return_value = open_evt
    c._fire_app_ended = MagicMock()
    asyncio.run(c._check_for_stale_session(now=NOW))
    c._store.close_open_event.assert_called_once()
    c._fire_app_ended.assert_called_once()


def test_native_event_kept_open_below_runaway_ceiling():
    """FIX 4 (regression): a native event still under the ceiling is NOT closed
    by the Apple-TV staleness machinery even when the pyatv mirror is stale."""
    p = _make_native_profile()
    ceiling_s = ma_mod.STALE_RUNAWAY_CEILING_S
    open_evt = MagicMock(
        id="evt", bundle_id=NATIVE_TV_BUNDLE_ID,
        started_at=NOW - timedelta(seconds=ceiling_s - 600),  # 10 min under cap
    )
    states = {
        APPLE_TV: _state("playing", last_updated=NOW - timedelta(minutes=30)),
        SAMSUNG: _state("on", attrs={"source": "TV"}),
    }
    c = _Coord(p, states=states)
    c._store.open_event_for.return_value = open_evt
    c._fire_app_ended = MagicMock()
    asyncio.run(c._check_for_stale_session(now=NOW))
    c._store.close_open_event.assert_not_called()


# ---------------------------------------------------------------------------
# v0.21.1 round-2 fixes (second adversarial review) — completeness of FIX 1 & 4
# ---------------------------------------------------------------------------


def test_enter_enforcing_native_sibling_group_does_not_kill_tv_with_tv_shutdown():
    """FIX 1 COMPLETION: with tv_shutdown_target set (the owner's live config), a
    SIBLING-group enforce (group:movies) while native TV is on must STILL not
    power off the TV — the generic tv_shutdown fall-through target IS that same
    Samsung TV. The first fix's test left tv_shutdown_target=None, so its
    fall-through target was None and this gap went uncaught."""
    p = _make_native_profile(tv_shutdown_target=SAMSUNG, enable_adguard_block=True)
    states = {
        APPLE_TV: _state("off"),
        SAMSUNG: _state("on", attrs={"source": "TV"}),
    }
    e, hass, adguard, calls = _make_enforcer(p, states=states)
    e._enforce_reason = "group:movies"
    asyncio.run(e._enter_enforcing())
    assert all(c[0] != SAMSUNG for c in calls), \
        f"sibling-group enforce blacked out Live TV via tv_shutdown; calls={calls}"
    adguard.set_blocked.assert_awaited_once()  # normal Apple-TV path still ran
    # Intentional spare is NOT an enforcement failure.
    assert e._last_enforcement_failed is False


def test_enter_enforcing_native_spare_does_not_flag_enforcement_failed():
    """v0.21.1 (3rd review, minor) — native profile with NO secondary + AdGuard
    OFF + tv_shutdown set. A sibling-group enforce spares Live TV; that
    intentional no-op must NOT set _last_enforcement_failed, else the
    effective_state sensor renders a sustained false `enforcing_failed`."""
    p = _make_native_profile(tv_shutdown_target=SAMSUNG, enable_adguard_block=False)
    states = {
        APPLE_TV: _state("off"),
        SAMSUNG: _state("on", attrs={"source": "TV"}),
    }
    e, hass, adguard, calls = _make_enforcer(p, states=states)
    e._enforce_reason = "group:movies"
    asyncio.run(e._enter_enforcing())
    assert all(c[0] != SAMSUNG for c in calls)   # Live TV spared
    assert e._last_enforcement_failed is False    # intentional no-op, not a failure


def test_enter_enforcing_non_native_still_uses_tv_shutdown_target():
    """Regression for the FIX 1 completion: when the room is NOT on native TV,
    tv_shutdown_target must STILL fire (the native suppression must not disable
    normal tv_shutdown enforcement of the Apple TV)."""
    p = _make_native_profile(tv_shutdown_target=SAMSUNG, enable_adguard_block=True)
    states = {
        APPLE_TV: _state("playing", attrs={"app_id": "com.disney.disneyplus"}),
        SAMSUNG: _state("on", attrs={"source": "HDMI1"}),  # Apple TV input, NOT native
    }
    e, hass, adguard, calls = _make_enforcer(p, states=states)
    e._enforce_reason = "group:movies"
    asyncio.run(e._enter_enforcing())
    assert any(c[0] == SAMSUNG for c in calls), \
        "tv_shutdown target should still fire when the room is not on native TV"


def test_native_sync_skips_stitch_opens_fresh():
    """FIX 4 COMPLETION: a native event never stitches. With no open event and
    native active, _sync_open_event opens a FRESH event instead of calling
    reopen_recent_event_if_match (which would restore the old started_at and
    neutralize the runaway ceiling)."""
    p = _make_native_profile()
    states = {APPLE_TV: _state("off"), SAMSUNG: _state("on", attrs={"source": "TV"})}
    c = _Coord(p, states=states)
    c._store.open_event_for.return_value = None  # previous native event just closed
    asyncio.run(c._sync_open_event(
        effective=NATIVE_TV_BUNDLE_ID, from_secondary=True, now=NOW))
    c._store.reopen_recent_event_if_match.assert_not_called()  # native never stitches
    c._store.open_event.assert_called_once()                   # fresh event opened
    assert c._store.open_event.call_args.kwargs.get("at") == NOW


def test_non_native_sync_still_stitches():
    """Control: a normal (non-native) bundle STILL uses the stitch path — the
    FIX-4 stitch-skip is native-only."""
    p = _make_native_profile()
    states = {APPLE_TV: _state("playing", attrs={"app_id": "com.netflix.Netflix"})}
    c = _Coord(p, states=states)
    c._store.open_event_for.return_value = None
    asyncio.run(c._sync_open_event(
        effective="com.netflix.Netflix", from_secondary=False, now=NOW))
    c._store.reopen_recent_event_if_match.assert_called_once()


def test_native_runaway_close_then_sync_no_restitch_full_tick():
    """FIX 4 COMPLETION, FULL TICK: the runaway-ceiling close of a stuck-'on'
    native event is followed on the SAME tick by _sync_open_event. Native must
    NOT re-stitch the just-closed event (which would restore the 2.5h+ started_at
    and re-close+re-stitch every tick). A fresh event opens at `now` instead. The
    prior FIX-4 test only ran _check_for_stale_session in isolation, missing this."""
    p = _make_native_profile()
    ceiling_s = ma_mod.STALE_RUNAWAY_CEILING_S
    old_evt = MagicMock(
        id="evt-old", bundle_id=NATIVE_TV_BUNDLE_ID,
        started_at=NOW - timedelta(seconds=ceiling_s + 600),  # 10 min past the cap
    )
    states = {APPLE_TV: _state("off"), SAMSUNG: _state("on", attrs={"source": "TV"})}
    c = _Coord(p, states=states)
    c._store.open_event_for.return_value = old_evt

    def _close(pid, *, at):
        c._store.open_event_for.return_value = None  # event is now closed
        return old_evt
    c._store.close_open_event.side_effect = _close
    c._fire_app_ended = MagicMock()
    c._fire_app_started = MagicMock()

    # 1) stale-check: the runaway ceiling force-closes the 3h-old native event.
    asyncio.run(c._check_for_stale_session(now=NOW))
    c._store.close_open_event.assert_called_once()

    # 2) SAME tick: resolve + sync — the TV is still on with a native source.
    eff, sec = c._resolve_room_activity(NOW)
    assert eff == NATIVE_TV_BUNDLE_ID
    asyncio.run(c._sync_open_event(effective=eff, from_secondary=sec, now=NOW))

    # NO re-stitch (that would restore the 3h-ago started_at); a FRESH event opens.
    c._store.reopen_recent_event_if_match.assert_not_called()
    c._store.open_event.assert_called_once()
    assert c._store.open_event.call_args.kwargs.get("at") == NOW
