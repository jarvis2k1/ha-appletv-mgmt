"""Tests for the v0.16.0 countdown voice (the 30s pre-enforce cue).

The static cue lands BEFORE the enforce_message — the kid gets a final
"wrap it up NOW" beat after the existing 5-min / 2-min warnings. Single-
fire latch (`_countdown_fired`) prevents the voice from re-firing every
30s tick while we stay in WARNING/GRACE; the latch is reset on
transition back to OK and on `_exit_enforcing`.

The test uses the same stub-the-world pattern as test_enforcement_failed.py
to drive the enforcer's `evaluate()` and `_apply` paths without an HA
runtime.
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone
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
    ev_mod = sys.modules["homeassistant.helpers.event"]
    if not hasattr(ev_mod, "async_call_later"):
        ev_mod.async_call_later = lambda hass, delay, callback: (lambda: None)
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

# Ensure the package stubs are in place before importing enforcer/storage.
for pkg_name in ("custom_components", "custom_components.appletv_mgmt"):
    if pkg_name not in sys.modules:
        stub = types.ModuleType(pkg_name)
        stub.__path__ = [str(PKG)] if pkg_name.endswith("appletv_mgmt") else []
        sys.modules[pkg_name] = stub

import importlib.util

for name in ("const", "storage", "policy", "state", "quiet", "adguard"):
    full = f"custom_components.appletv_mgmt.{name}"
    if full not in sys.modules:
        spec = importlib.util.spec_from_file_location(full, PKG / f"{name}.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules[full] = m
        spec.loader.exec_module(m)

# Stub the heavy submodules the enforcer transitively imports (audit, voice).
# IMPORTANT: stub voice_notifier.speak with an AsyncMock we can spy on per-test.
_VOICE_SPEAK = AsyncMock(return_value={"status": "spoken", "message": "stub"})


def _install_audit_voice_stubs():
    for name in ("audit", "voice_notifier", "media_attribution"):
        full = f"custom_components.appletv_mgmt.{name}"
        if full not in sys.modules:
            stub = types.ModuleType(full)
            if name == "audit":
                stub.record_admin_action = MagicMock()
                stub.record_action = MagicMock()
                stub.register_action_recorder = lambda hass, profile_id: (lambda: None)
            elif name == "voice_notifier":
                stub.should_speak = lambda profile, template: bool(template)
                stub.speak = _VOICE_SPEAK
            elif name == "media_attribution":
                stub.INACTIVE_MEDIA_STATES = {"off", "standby", "unavailable", "idle"}
                stub.UNKNOWN_APP_BUNDLE_ID = "unknown"
            sys.modules[full] = stub
        else:
            # Already loaded by an earlier test — patch our spy in if voice_notifier.
            if name == "voice_notifier":
                sys.modules[full].speak = _VOICE_SPEAK
                # And the should_speak guard
                sys.modules[full].should_speak = lambda profile, template: bool(template)


_install_audit_voice_stubs()

spec = importlib.util.spec_from_file_location(
    "custom_components.appletv_mgmt.enforcer", PKG / "enforcer.py"
)
if "custom_components.appletv_mgmt.enforcer" not in sys.modules:
    enforcer_mod = importlib.util.module_from_spec(spec)
    sys.modules["custom_components.appletv_mgmt.enforcer"] = enforcer_mod
    spec.loader.exec_module(enforcer_mod)
else:
    enforcer_mod = sys.modules["custom_components.appletv_mgmt.enforcer"]


def _make_profile(*, countdown_message="Achtung! Noch 30 Sekunden Bildschirmzeit."):
    storage_mod = sys.modules["custom_components.appletv_mgmt.storage"]
    return storage_mod.Profile(
        id="p1",
        display_name="Living Room",
        apple_tv_entity_id="media_player.living_room_apple_tv",
        adguard_client_name="AppleTV",
        daily_budget_min=60,
        grace_seconds=60,
        warn_thresholds_min=[5],
        idle_grace_minutes=5,
        notify_media_player_entity_id="media_player.sonos_kitchen",
        countdown_message=countdown_message,
    )


def _make_enforcer(profile, *, apple_state="playing", tv_state=None):
    hass = MagicMock()
    # v0.19.2 — the countdown voice now checks someone_could_be_watching,
    # which reads hass.states.get(apple_tv_entity_id).state. Default to an
    # actively-playing Apple TV (the realistic "kid is watching, about to be
    # cut off" scenario these tests model). Pass apple_state="idle"/"off"
    # + tv_state=None to exercise the empty-room suppression path.
    import types as _types

    def _states_get(entity_id):
        if entity_id == getattr(profile, "tv_entity_id", None):
            return None if tv_state is None else _types.SimpleNamespace(state=tv_state)
        return None if apple_state is None else _types.SimpleNamespace(state=apple_state)

    hass.states.get = _states_get
    adguard = MagicMock()
    adguard.set_blocked = AsyncMock(return_value=None)
    store = MagicMock()
    store.is_adult_mode_active = MagicMock(return_value=False)
    store.adult_mode_until = MagicMock(return_value=None)
    e = enforcer_mod.EnforcementController(hass, adguard, profile, store=store)
    # Reset the shared speak spy so each test starts clean. Re-bind
    # speak onto the voice_notifier stub module — sibling test files
    # may have overwritten it during their own collection (they all
    # share sys.modules), so we re-claim it per test to make ordering
    # independent.
    _VOICE_SPEAK.reset_mock()
    sys.modules["custom_components.appletv_mgmt.voice_notifier"].speak = _VOICE_SPEAK
    return e


# Convenience: STATE_WARNING / STATE_GRACE / STATE_OK / STATE_ENFORCING from const
from custom_components.appletv_mgmt.const import (  # noqa: E402
    STATE_ENFORCING, STATE_GRACE, STATE_OK, STATE_WARNING,
)


# ---------- happy path ----------


def test_countdown_fires_in_warning_at_30s_remaining():
    """30s before enforce, state=WARNING, message configured → voice fires once."""
    p = _make_profile()
    e = _make_enforcer(p)
    e._state = STATE_WARNING
    # remaining = 30s
    asyncio.run(e._maybe_fire_countdown(remaining_s=30))
    assert _VOICE_SPEAK.call_count == 1
    assert e._countdown_fired is True


def test_countdown_fires_in_grace_at_35s_remaining():
    """At the upper bound (35s) and state=GRACE, fires once."""
    p = _make_profile()
    e = _make_enforcer(p)
    e._state = STATE_GRACE
    asyncio.run(e._maybe_fire_countdown(remaining_s=35))
    assert _VOICE_SPEAK.call_count == 1


def test_countdown_does_not_fire_in_ok_state():
    """OK is too early — message would land minutes before enforce."""
    p = _make_profile()
    e = _make_enforcer(p)
    e._state = STATE_OK
    asyncio.run(e._maybe_fire_countdown(remaining_s=30))
    assert _VOICE_SPEAK.call_count == 0
    assert e._countdown_fired is False


def test_countdown_does_not_fire_in_enforcing_state():
    """ENFORCING is too late — the lights are already going off."""
    p = _make_profile()
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    asyncio.run(e._maybe_fire_countdown(remaining_s=10))
    assert _VOICE_SPEAK.call_count == 0


def test_countdown_does_not_fire_when_remaining_too_high():
    """remaining > 35s → outside the window."""
    p = _make_profile()
    e = _make_enforcer(p)
    e._state = STATE_WARNING
    asyncio.run(e._maybe_fire_countdown(remaining_s=60))
    assert _VOICE_SPEAK.call_count == 0


def test_countdown_does_not_fire_when_remaining_zero_or_negative():
    """remaining <= 0 means budget already gone — the enforce_message
    pipeline takes over; we don't pile on with a redundant countdown."""
    p = _make_profile()
    e = _make_enforcer(p)
    e._state = STATE_WARNING
    asyncio.run(e._maybe_fire_countdown(remaining_s=0))
    assert _VOICE_SPEAK.call_count == 0
    asyncio.run(e._maybe_fire_countdown(remaining_s=-5))
    assert _VOICE_SPEAK.call_count == 0


# ---------- opt-in (empty message = silent) ----------


def test_countdown_silent_when_message_empty():
    """Default v0.15.9-upgrade behavior: countdown_message="" → never speak."""
    p = _make_profile(countdown_message="")
    e = _make_enforcer(p)
    e._state = STATE_WARNING
    asyncio.run(e._maybe_fire_countdown(remaining_s=30))
    assert _VOICE_SPEAK.call_count == 0
    assert e._countdown_fired is False


# ---------- single-fire latch ----------


def test_countdown_fires_only_once_per_cycle():
    """Even if evaluate() ticks every 30s and the state stays WARNING,
    the countdown voice fires once and stays quiet for the rest of the
    wind-down."""
    p = _make_profile()
    e = _make_enforcer(p)
    e._state = STATE_WARNING

    # 3 successive ticks within the window
    asyncio.run(e._maybe_fire_countdown(remaining_s=30))
    asyncio.run(e._maybe_fire_countdown(remaining_s=20))
    asyncio.run(e._maybe_fire_countdown(remaining_s=10))

    assert _VOICE_SPEAK.call_count == 1, (
        "Latch failed — voice fired more than once during a single "
        "WARNING/GRACE cycle"
    )


# ---------- latch reset on transition back to OK ----------


def test_countdown_latch_resets_on_apply_to_ok():
    """When _apply transitions WARNING/GRACE → OK (e.g. kid stopped
    watching and used dropped below warn threshold), the latch resets so
    the next wind-down cycle can fire its own countdown."""
    p = _make_profile()
    e = _make_enforcer(p)
    e._state = STATE_WARNING
    asyncio.run(e._maybe_fire_countdown(remaining_s=30))
    assert e._countdown_fired is True

    # Transition back to OK
    StateDecision = enforcer_mod.StateDecision
    asyncio.run(e._apply(StateDecision(STATE_OK, None)))
    assert e._countdown_fired is False

    # Now back into WARNING — should fire again
    e._state = STATE_WARNING
    asyncio.run(e._maybe_fire_countdown(remaining_s=30))
    assert _VOICE_SPEAK.call_count == 2


def test_countdown_latch_resets_on_exit_enforcing():
    """When _exit_enforcing runs (ENFORCING → OK/WARNING transition),
    the latch resets along with the other per-cycle state."""
    p = _make_profile()
    e = _make_enforcer(p)
    e._countdown_fired = True  # simulate already-fired
    asyncio.run(e._exit_enforcing())
    assert e._countdown_fired is False


# ---------- bypass: should_act != ACT silences the countdown ----------


def test_countdown_silent_under_adult_mode():
    """Adult mode → BYPASS → no voice (parity with the other voices)."""
    from datetime import timedelta
    p = _make_profile()
    e = _make_enforcer(p)
    e._state = STATE_WARNING
    # Adult mode active: adult_mode_until in the future
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    e._store.adult_mode_until = MagicMock(return_value=future)
    asyncio.run(e._maybe_fire_countdown(remaining_s=30))
    assert _VOICE_SPEAK.call_count == 0
    assert e._countdown_fired is False


def test_countdown_silent_under_paused_mode():
    """mode=paused → BYPASS → no voice."""
    p = _make_profile()
    p.mode = "paused"
    e = _make_enforcer(p)
    e._state = STATE_WARNING
    asyncio.run(e._maybe_fire_countdown(remaining_s=30))
    assert _VOICE_SPEAK.call_count == 0


def test_countdown_silent_under_monitor_only_mode():
    """mode=monitor_only with default warn_in_monitor_mode=False → no
    countdown voice. Monitor mode keeps tracking + audit but stays
    silent unless the parent opts into the calibration heads-up."""
    p = _make_profile()
    p.mode = "monitor_only"
    e = _make_enforcer(p)
    e._state = STATE_WARNING
    asyncio.run(e._maybe_fire_countdown(remaining_s=30))
    assert _VOICE_SPEAK.call_count == 0


def test_countdown_fires_under_monitor_only_with_warn_opt_in():
    """v0.16.4 regression guard — monitor_only + warn_in_monitor_mode=True
    speaks the countdown cue. The bare `decision.kind != "ACT"` gate
    used to silently drop it (live 2026-05-30 13:13 grace window had
    a warn voice at 13:11:50 but no countdown audit row before
    enforce_start at 13:14:33). The fix routes the gate through the
    same warn_in_monitor_mode policy the warn voice uses."""
    p = _make_profile()
    p.mode = "monitor_only"
    p.warn_in_monitor_mode = True
    e = _make_enforcer(p)
    e._state = STATE_GRACE
    asyncio.run(e._maybe_fire_countdown(remaining_s=30))
    assert _VOICE_SPEAK.call_count == 1
    assert e._countdown_fired is True


# ---------- production timer path (_fire_countdown_now) ----------


def test_fire_countdown_now_fires_under_monitor_only_with_warn_opt_in():
    """v0.16.4 — the HA-timer path (production codepath the live bug hit)
    must also respect warn_in_monitor_mode. _fire_countdown_now is what
    the scheduled timer fires from GRACE entry; the bare ACT-only gate
    was the silent-drop site at enforcer.py:1007-1013."""
    p = _make_profile()
    p.mode = "monitor_only"
    p.warn_in_monitor_mode = True
    e = _make_enforcer(p)
    e._state = STATE_GRACE
    asyncio.run(e._fire_countdown_now())
    assert _VOICE_SPEAK.call_count == 1


def test_fire_countdown_now_silent_under_monitor_only_default():
    """Default opt-out: monitor_only with warn_in_monitor_mode=False
    keeps the timer-fired countdown silent — same as the warn voice."""
    p = _make_profile()
    p.mode = "monitor_only"
    e = _make_enforcer(p)
    e._state = STATE_GRACE
    asyncio.run(e._fire_countdown_now())
    assert _VOICE_SPEAK.call_count == 0


def test_fire_countdown_now_silent_under_adult_mode():
    """Adult mode (BYPASS) silences the timer-fired countdown
    regardless of warn_in_monitor_mode."""
    from datetime import timedelta
    p = _make_profile()
    p.warn_in_monitor_mode = True  # would not matter — adult mode wins
    e = _make_enforcer(p)
    e._state = STATE_GRACE
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    e._store.adult_mode_until = MagicMock(return_value=future)
    asyncio.run(e._fire_countdown_now())
    assert _VOICE_SPEAK.call_count == 0


def test_fire_countdown_now_suppressed_when_device_idle_and_tv_off():
    """v0.19.2 — the exact live bug: countdown must NOT fire when the Apple
    TV is idle AND the TV is off (nobody watching). Enforced mode, GRACE
    state, voice fully configured — only the empty-room gate stops it."""
    p = _make_profile()
    p.tv_entity_id = "media_player.samsung_tv"
    e = _make_enforcer(p, apple_state="idle", tv_state="off")
    e._state = STATE_GRACE
    asyncio.run(e._fire_countdown_now())
    assert _VOICE_SPEAK.call_count == 0


def test_fire_countdown_now_fires_when_idle_but_tv_on():
    """Fail-safe: pyatv-stale `idle` while the TV is genuinely on → the
    countdown STILL fires (don't miss a legit heads-up)."""
    p = _make_profile()
    p.tv_entity_id = "media_player.samsung_tv"
    e = _make_enforcer(p, apple_state="idle", tv_state="on")
    e._state = STATE_GRACE
    asyncio.run(e._fire_countdown_now())
    assert _VOICE_SPEAK.call_count == 1


# ---------- initial state ----------


def test_initial_countdown_fired_is_false():
    p = _make_profile()
    e = _make_enforcer(p)
    assert e._countdown_fired is False
