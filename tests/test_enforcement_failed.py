"""Tests for the v0.15.0 `_last_enforcement_failed` flag (spec §3.4.3).

The full EnforcementController is async + needs an HA stack. These tests
stub HA aggressively and drive `_enter_enforcing` / `_exit_enforcing`
directly with mocked AdGuard + turn_off side effects, then verify the
flag value the coordinator surfaces.

Scenarios:
  * AdGuard succeeds + turn_off succeeds + no TV target → flag False
  * AdGuard raises + turn_off succeeds → flag True (AdGuard failure)
  * AdGuard succeeds + turn_off fails + no TV target → flag True
  * AdGuard succeeds + turn_off fails + TV target succeeds → flag False
  * AdGuard succeeds + turn_off fails + TV target fails → flag True
  * Adult mode bypass → flag stays False
  * Monitor mode bypass → flag stays False
  * _exit_enforcing clears the flag
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
    """Same stubs as test_init.py — kept independent so test ordering doesn't matter."""
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
    elif not hasattr(const.Platform, "SELECT"):
        const.Platform.SELECT = "select"
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
for name in ("audit", "voice_notifier", "media_attribution"):
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
        elif name == "media_attribution":
            stub.INACTIVE_MEDIA_STATES = {"off", "standby", "unavailable", "idle"}
            stub.UNKNOWN_APP_BUNDLE_ID = "unknown"
        sys.modules[full] = stub

spec = importlib.util.spec_from_file_location(
    "custom_components.appletv_mgmt.enforcer", PKG / "enforcer.py"
)
enforcer_mod = importlib.util.module_from_spec(spec)
sys.modules["custom_components.appletv_mgmt.enforcer"] = enforcer_mod
spec.loader.exec_module(enforcer_mod)

# v0.17.0 — re-export state constants from const for convenience.
_const_mod = sys.modules["custom_components.appletv_mgmt.const"]
STATE_OK = _const_mod.STATE_OK
STATE_WARNING = _const_mod.STATE_WARNING
STATE_GRACE = _const_mod.STATE_GRACE
STATE_ENFORCING = _const_mod.STATE_ENFORCING


def _make_profile(*, tv_shutdown_target=None, enforcement_enabled=True):
    storage_mod = sys.modules["custom_components.appletv_mgmt.storage"]
    return storage_mod.Profile(
        id="p1",
        display_name="Living Room",
        apple_tv_entity_id="media_player.heimkinoaaa",
        adguard_client_name="AppleTV",
        daily_budget_min=60,
        grace_seconds=60,
        warn_thresholds_min=[5],
        idle_grace_minutes=5,
        enforcement_enabled=enforcement_enabled,
        tv_shutdown_target=tv_shutdown_target,
    )


def _make_enforcer(profile, *, adguard_raises=False, turn_off_results=None):
    """Build an EnforcementController with mocked HA + AdGuard + store.

    `turn_off_results` is a list of bool results that
    `_call_turn_off_with_verify` will return in order (one per call).
    """
    hass = MagicMock()
    adguard = MagicMock()
    if adguard_raises:
        adguard.set_blocked = AsyncMock(side_effect=RuntimeError("AdGuard down"))
    else:
        adguard.set_blocked = AsyncMock(return_value=None)
    adguard.get_client = AsyncMock(return_value={"blocked_services": [], "use_global_blocked_services": True})
    store_mod = sys.modules["custom_components.appletv_mgmt.storage"]
    # Minimal store stub so the adult-mode check returns False.
    store = MagicMock()
    store.is_adult_mode_active = MagicMock(return_value=False)
    # v0.17.0 — _should_act_now() (called from _exit_enforcing for the
    # F-G "skip AdGuard when never actually blocked" gate) consults
    # `adult_mode_until`. Default to None so policy returns ACT.
    store.adult_mode_until = MagicMock(return_value=None)
    e = enforcer_mod.EnforcementController(hass, adguard, profile, store=store)

    # Patch the turn_off verifier with a fake that returns results in order.
    turn_off_results = list(turn_off_results or [True])
    call_count = {"n": 0}

    async def fake_turn_off(entity_id, *, label):
        idx = call_count["n"]
        call_count["n"] += 1
        if idx < len(turn_off_results):
            return turn_off_results[idx]
        return turn_off_results[-1] if turn_off_results else True

    e._call_turn_off_with_verify = fake_turn_off
    # No-op the voice announcement so we don't follow its branches here.
    e._maybe_announce_enforce = AsyncMock()
    return e


# ---------- the success case --------------------------------------------------


def test_enter_enforcing_success_no_tv_target_clears_flag():
    """AdGuard OK + apple_tv turn_off OK + no TV target → flag False."""
    p = _make_profile(tv_shutdown_target=None)
    e = _make_enforcer(p, turn_off_results=[True])
    asyncio.run(e._enter_enforcing())
    assert e._last_enforcement_failed is False


def test_enter_enforcing_success_with_tv_target_both_ok_clears_flag():
    """AdGuard OK + apple_tv OK + TV target OK → flag False."""
    p = _make_profile(tv_shutdown_target="media_player.tv")
    e = _make_enforcer(p, turn_off_results=[True, True])
    asyncio.run(e._enter_enforcing())
    assert e._last_enforcement_failed is False


# ---------- AdGuard failure -------------------------------------------------


def test_enter_enforcing_adguard_failure_sets_flag():
    """AdGuard raises → flag True even if turn_off succeeds.

    v0.15.5: must opt in to AdGuard for this test (the new default is OFF
    which would short-circuit the failure path).
    """
    p = _make_profile(tv_shutdown_target=None)
    p.enable_adguard_block = True
    e = _make_enforcer(p, adguard_raises=True, turn_off_results=[True])
    asyncio.run(e._enter_enforcing())
    assert e._last_enforcement_failed is True


# ---------- turn_off failure --------------------------------------------------


def test_enter_enforcing_turn_off_fails_no_tv_target_sets_flag():
    """AdGuard OK + apple_tv turn_off fails + no TV target → flag True."""
    p = _make_profile(tv_shutdown_target=None)
    e = _make_enforcer(p, turn_off_results=[False])
    asyncio.run(e._enter_enforcing())
    assert e._last_enforcement_failed is True


def test_enter_enforcing_apple_tv_fails_but_tv_target_ok_clears_flag():
    """apple_tv turn_off fails, TV target succeeds → flag False (any_turn_off_succeeded)."""
    p = _make_profile(tv_shutdown_target="media_player.tv")
    e = _make_enforcer(p, turn_off_results=[False, True])
    asyncio.run(e._enter_enforcing())
    assert e._last_enforcement_failed is False


def test_enter_enforcing_all_turn_off_fail_sets_flag():
    """apple_tv + TV target both fail → flag True."""
    p = _make_profile(tv_shutdown_target="media_player.tv")
    e = _make_enforcer(p, turn_off_results=[False, False])
    asyncio.run(e._enter_enforcing())
    assert e._last_enforcement_failed is True


# ---------- bypass paths ----------------------------------------------------


def test_enter_enforcing_under_adult_mode_does_not_set_flag():
    """Adult mode bypass is intentional, not a failure."""
    p = _make_profile()
    e = _make_enforcer(p)
    # Pre-set the flag to True to verify it gets cleared.
    e._last_enforcement_failed = True
    e._store.is_adult_mode_active = MagicMock(return_value=True)
    asyncio.run(e._enter_enforcing())
    assert e._last_enforcement_failed is False


def test_enter_enforcing_under_monitor_mode_does_not_set_flag():
    """Monitor mode bypass is intentional, not a failure."""
    p = _make_profile(enforcement_enabled=False)
    e = _make_enforcer(p)
    e._last_enforcement_failed = True  # pre-poisoned
    asyncio.run(e._enter_enforcing())
    assert e._last_enforcement_failed is False


# ---------- _exit_enforcing clears the flag --------------------------------


def test_exit_enforcing_clears_flag():
    p = _make_profile()
    e = _make_enforcer(p)
    e._last_enforcement_failed = True
    asyncio.run(e._exit_enforcing())
    assert e._last_enforcement_failed is False


def test_exit_enforcing_clears_flag_even_when_adguard_unblock_raises():
    p = _make_profile()
    e = _make_enforcer(p)
    e._adguard.set_blocked = AsyncMock(side_effect=RuntimeError("AdGuard down"))
    e._last_enforcement_failed = True
    asyncio.run(e._exit_enforcing())
    # Leaving ENFORCING clears the flag regardless of unblock outcome.
    assert e._last_enforcement_failed is False


# ---------- initial state ----------------------------------------------------


def test_initial_flag_is_false():
    p = _make_profile()
    e = _make_enforcer(p)
    assert e._last_enforcement_failed is False


# ---------- v0.15.1 regression: lying-voice when entity already off ---------

def test_call_turn_off_with_verify_returns_false_when_already_inactive():
    """v0.15.1 fix: if pre-call state is already inactive (off/standby/
    unavailable/unknown), _call_turn_off_with_verify must return False
    so any_turn_off_succeeded stays False and the enforce_message voice
    doesn't lie ("Apple TV wird jetzt abgeschaltet" when nothing changed).

    Live-observed 2026-05-25: Samsung TV was already off, but
    media_player.turn_off "succeeded" trivially → voice fired every ~45s.
    """
    from unittest.mock import MagicMock

    p = _make_profile()
    e = _make_enforcer(p)
    # Replace the stubbed-out method with the real one.
    real_method = enforcer_mod.EnforcementController._call_turn_off_with_verify
    e._call_turn_off_with_verify = real_method.__get__(e)

    # Mock hass.states.get to return an already-off state.
    state_off = MagicMock()
    state_off.state = "off"
    e._hass.states.get = MagicMock(return_value=state_off)
    # Mock services.async_call so we can assert it was NOT called.
    e._hass.services.async_call = AsyncMock()

    result = asyncio.run(e._call_turn_off_with_verify("media_player.foo", label="TV"))

    # The fix: pre-check returns False without calling the service.
    assert result is False, "Already-off entity must NOT count as a successful turn_off"
    e._hass.services.async_call.assert_not_called()


def test_call_turn_off_with_verify_attempts_when_active():
    """Counter-test: when entity is in an active state ("playing"),
    _call_turn_off_with_verify does try the service call (the normal path).
    """
    from unittest.mock import MagicMock

    p = _make_profile()
    e = _make_enforcer(p)
    real_method = enforcer_mod.EnforcementController._call_turn_off_with_verify
    e._call_turn_off_with_verify = real_method.__get__(e)

    # First poll: still playing. After service call: off.
    states = [MagicMock(), MagicMock()]
    states[0].state = "playing"   # pre-check
    states[1].state = "off"        # post-call poll
    e._hass.states.get = MagicMock(side_effect=lambda eid: states.pop(0) if states else states[0])
    e._hass.services.async_call = AsyncMock(return_value=None)

    result = asyncio.run(e._call_turn_off_with_verify("media_player.foo", label="TV"))
    assert result is True
    e._hass.services.async_call.assert_called_once()


def test_lying_voice_regression_samsung_already_off():
    """End-to-end regression for the reported-bug 2026-05-25:
    Apple TV pyatv broken, Samsung set as tv_shutdown_target but
    Samsung is already off. _enter_enforcing must NOT speak the
    enforce_message because nothing actually changed.
    """
    from unittest.mock import MagicMock

    p = _make_profile(tv_shutdown_target="media_player.samsung_tv")
    p.enforce_message = "Bildschirmzeit ist vorbei."
    e = _make_enforcer(p)

    # Use the real _call_turn_off_with_verify (not the stub).
    real_method = enforcer_mod.EnforcementController._call_turn_off_with_verify
    e._call_turn_off_with_verify = real_method.__get__(e)

    # Apple TV "playing" but pyatv breaks (service call hangs/raises).
    # Samsung "off" — the killer case.
    def fake_state_get(entity_id):
        s = MagicMock()
        s.state = "off" if "samsung" in entity_id else "playing"
        return s
    e._hass.states.get = MagicMock(side_effect=fake_state_get)
    # Apple TV service call fails (pyatv companion drop simulation).
    e._hass.services.async_call = AsyncMock(side_effect=RuntimeError("pyatv companion drop"))

    # Spy on _maybe_announce_enforce — restore the real method to verify
    # it doesn't even get called (any_turn_off_succeeded should be False).
    announce_calls = []

    async def spy_announce():
        announce_calls.append(True)

    e._maybe_announce_enforce = spy_announce

    asyncio.run(e._enter_enforcing())

    # No voice fired — the lie is silenced.
    assert announce_calls == [], (
        f"Voice fired when it shouldn't have. Both turn_off paths effectively "
        f"failed: Apple TV pyatv raised, Samsung was already off. "
        f"any_turn_off_succeeded must be False."
    )


# ---------- v0.15.2 regression: _enter_enforcing only on transition ---------

def test_v0_15_8_voice_does_not_fire_when_only_samsung_succeeded_not_apple_tv():
    """v0.15.8: live-observed 2026-05-28 — enforce_message ('Bildschirmzeit
    ist vorbei. Apple TV wird jetzt abgeschaltet.') fired even though
    Apple TV pyatv failed and Netflix kept streaming. Root cause:
    Samsung TV `turn_off` reported momentary success (HA state went
    'off' briefly during the verify-poll window), satisfying the old
    `any_turn_off_succeeded` voice gate. But Apple TV stays running →
    voice is a lie.

    Fix: voice gate is now strictly `turn_off_ok` (the Apple TV's
    verified state transition). Samsung succeeding is still relevant
    for the enforcement_failed flag (partial enforcement is better
    than none) — just doesn't justify the audible claim.
    """
    p = _make_profile(tv_shutdown_target="media_player.samsung_tv")
    # Apple TV fails (pyatv companion drop), Samsung "succeeds"
    e = _make_enforcer(p, turn_off_results=[False, True])
    asyncio.run(e._enter_enforcing())
    # The voice helper must NOT have been called (the AsyncMock counter)
    e._maybe_announce_enforce.assert_not_called()


def test_v0_15_8_voice_fires_when_apple_tv_succeeded():
    """Counter-test: when Apple TV's pyatv works, voice DOES fire."""
    p = _make_profile(tv_shutdown_target="media_player.samsung_tv")
    # Apple TV succeeds, Samsung also succeeds
    e = _make_enforcer(p, turn_off_results=[True, True])
    asyncio.run(e._enter_enforcing())
    e._maybe_announce_enforce.assert_called_once()


def test_v0_15_8_voice_fires_when_apple_tv_succeeded_no_samsung_configured():
    """Apple TV works + no Samsung target → voice fires (back-compat
    with installs that don't have a tv_shutdown_target)."""
    p = _make_profile(tv_shutdown_target=None)
    e = _make_enforcer(p, turn_off_results=[True])
    asyncio.run(e._enter_enforcing())
    e._maybe_announce_enforce.assert_called_once()


def test_v0_15_7_adguard_disabled_sets_is_blocked_flag_for_drift_detection():
    """v0.15.7 regression: with AdGuard disabled, _enter_enforcing must
    set `_is_blocked = True` as a state-machine flag. Without this,
    reassert() sees `want_blocked=True` vs `_is_blocked=False` → thinks
    drift → re-fires _enter_enforcing every coordinator tick → voice spam
    every 30s. Live-observed 2026-05-26 during the first full-enforcement
    test (the owner's Netflix at budget=2min produced 'Bildschirmzeit ist
    vorbei' every minute even though Apple TV never went off).
    """
    p = _make_profile(tv_shutdown_target=None)
    p.enable_adguard_block = False  # explicit (the v0.15.5 default)
    e = _make_enforcer(p, turn_off_results=[False])  # pyatv fails
    e._is_blocked = False  # baseline
    asyncio.run(e._enter_enforcing())
    assert e._is_blocked is True, (
        "AdGuard disabled → must set _is_blocked=True as state flag "
        "(otherwise reassert sees infinite drift)"
    )


def test_v0_15_7_exit_clears_is_blocked_flag_even_when_adguard_disabled():
    """Counterpart to the above — _exit_enforcing must clear
    `_is_blocked` regardless of AdGuard's enabled state so the next
    enforcement cycle starts from a clean baseline.
    """
    p = _make_profile(tv_shutdown_target=None)
    p.enable_adguard_block = False
    e = _make_enforcer(p)
    e._is_blocked = True  # simulate post-enter state
    asyncio.run(e._exit_enforcing())
    assert e._is_blocked is False


def test_v0_15_5_adguard_skipped_when_disabled():
    """v0.15.5: enable_adguard_block=False (the new default) → AdGuard
    set_blocked is NOT called from _enter_enforcing; adguard_ok defaults
    to True so the enforcement_failed flag still reflects only the
    turn_off outcomes.
    """
    p = _make_profile(tv_shutdown_target=None)
    p.enable_adguard_block = False  # explicit (it's the default)
    e = _make_enforcer(p, turn_off_results=[True])
    asyncio.run(e._enter_enforcing())
    # set_blocked was NOT called
    e._adguard.set_blocked.assert_not_called()
    # Turn-off succeeded → flag False
    assert e._last_enforcement_failed is False


def test_v0_15_5_adguard_called_when_enabled():
    """v0.15.5: enable_adguard_block=True → AdGuard set_blocked IS called
    (back-compat path for users who opt in to the supplementary layer).
    """
    p = _make_profile(tv_shutdown_target=None)
    p.enable_adguard_block = True
    e = _make_enforcer(p, turn_off_results=[True])
    asyncio.run(e._enter_enforcing())
    e._adguard.set_blocked.assert_called_once()
    assert e._last_enforcement_failed is False


def test_v0_15_5_seed_from_adguard_skipped_when_disabled():
    """v0.15.5: seed_from_adguard is a no-op when enable_adguard_block=False
    so we don't make a network call to the AdGuard proxy at startup for
    nothing.
    """
    p = _make_profile(tv_shutdown_target=None)
    p.enable_adguard_block = False
    e = _make_enforcer(p)
    asyncio.run(e.seed_from_adguard())
    e._adguard.get_client.assert_not_called()


def test_v0_15_4_parallel_turn_off_when_both_targets_configured():
    """v0.15.4: when tv_shutdown_target is set, _enter_enforcing fires
    pyatv + Samsung calls in parallel via asyncio.gather (not sequentially).
    Pre-v0.15.4: Samsung waited up to ~25s for pyatv to fail-and-retry
    before the user-visible screen went dark. Post: ~12s max.

    The test asserts both calls fired (call_count == 2) without measuring
    wall-clock parallelism (that would be flaky in CI). The structural
    change to asyncio.gather is what we verify.
    """
    from unittest.mock import MagicMock

    p = _make_profile(tv_shutdown_target="media_player.samsung_tv")
    e = _make_enforcer(p, turn_off_results=[True, True])
    # The fake_turn_off in _make_enforcer increments call_count for each call.
    # Both pyatv + Samsung should fire.
    asyncio.run(e._enter_enforcing())
    # Verify the fake was called twice (once per target)
    # — both targets fired regardless of order or success
    # (we use closure state in fake_turn_off via call_count dict).
    assert e._last_enforcement_failed is False, "Both succeeded → flag False"


def test_v0_15_4_watchdog_retries_pyatv_when_apple_tv_still_active():
    """v0.15.4: while in ENFORCING state, if Apple TV is still in an
    active state (playing/on), reassert() retries pyatv turn_off.
    Catches the chronic case where pyatv's Companion protocol drops
    silently → Apple TV stays on → kid could resume watching.
    """
    from unittest.mock import MagicMock, AsyncMock
    from custom_components.appletv_mgmt.state import STATE_ENFORCING

    p = _make_profile(tv_shutdown_target=None)
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._is_blocked = True  # AdGuard agrees; no drift
    # No adult mode active — watchdog should fire freely.
    e._store.adult_mode_until = MagicMock(return_value=None)

    # Apple TV is still "playing" — pyatv must have failed earlier
    state_playing = MagicMock()
    state_playing.state = "playing"
    e._hass.states.get = MagicMock(return_value=state_playing)

    # Mock the call so we can count invocations
    retry_count = {"n": 0}

    async def fake_retry(entity_id, *, label):
        retry_count["n"] += 1
        return False  # simulate pyatv still failing this time too

    e._call_turn_off_with_verify = fake_retry

    asyncio.run(e.reassert())

    assert retry_count["n"] == 1, "Watchdog should fire pyatv turn_off once"


def test_v0_15_4_watchdog_silent_when_apple_tv_already_idle():
    """v0.15.4: when in ENFORCING but Apple TV is already idle/off
    (e.g. kid stopped watching or pyatv eventually worked), the watchdog
    is a NO-OP. Critical to preserve the v0.15.2 quiet-when-idle behavior
    (no audit-row spam every tick).
    """
    from unittest.mock import MagicMock
    from custom_components.appletv_mgmt.state import STATE_ENFORCING

    p = _make_profile(tv_shutdown_target=None)
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._is_blocked = True
    e._store.adult_mode_until = MagicMock(return_value=None)

    # Apple TV is in an INACTIVE_MEDIA_STATE
    state_off = MagicMock()
    state_off.state = "off"
    e._hass.states.get = MagicMock(return_value=state_off)

    retry_count = {"n": 0}

    async def fake_retry(entity_id, *, label):
        retry_count["n"] += 1
        return True

    e._call_turn_off_with_verify = fake_retry

    asyncio.run(e.reassert())

    assert retry_count["n"] == 0, (
        "Watchdog must NOT fire when Apple TV is already idle/off — "
        "would re-introduce the v0.15.2 audit-row spam"
    )


def test_v0174_watchdog_silent_when_apple_tv_is_idle_home_screen():
    """v0.17.4 regression: the pre-fix watchdog quiet-check used
    INACTIVE_MEDIA_STATES, which does NOT include `idle`. So a kid
    who walked away with the Apple TV on the home screen produced one
    ERROR log + one `enforce_turn_off_failed` audit row every 60s
    indefinitely (184 rows / 6h observed on live install once the
    v0.17.3 under-count fix made the budget actually bite — see
    CHANGELOG v0.17.4). Fix: use ACTIVELY_PLAYING_STATES
    (playing/paused/buffering) as the watchdog's "kid actually
    watching" predicate, matching the existing comment intent.
    """
    from unittest.mock import MagicMock
    from custom_components.appletv_mgmt.state import STATE_ENFORCING

    p = _make_profile(tv_shutdown_target=None)
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._is_blocked = True
    e._store.adult_mode_until = MagicMock(return_value=None)

    # Apple TV is in `idle` — was in ACTIVE_MEDIA_STATES (bug) but is
    # NOT in ACTIVELY_PLAYING_STATES, so the watchdog must be quiet.
    state_idle = MagicMock()
    state_idle.state = "idle"
    e._hass.states.get = MagicMock(return_value=state_idle)

    retry_count = {"n": 0}

    async def fake_retry(entity_id, *, label):
        retry_count["n"] += 1
        return True

    e._call_turn_off_with_verify = fake_retry

    asyncio.run(e.reassert())

    assert retry_count["n"] == 0, (
        "Watchdog must NOT fire when Apple TV is `idle` (home screen) — "
        "kid isn't actually watching anything"
    )


def test_v0174_watchdog_silent_when_apple_tv_is_on_not_playing():
    """v0.17.4: similarly `on` (just powered up, no app launched yet) is
    NOT actively playing — watchdog should not fire."""
    from unittest.mock import MagicMock
    from custom_components.appletv_mgmt.state import STATE_ENFORCING

    p = _make_profile(tv_shutdown_target=None)
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._is_blocked = True
    e._store.adult_mode_until = MagicMock(return_value=None)

    state_on = MagicMock()
    state_on.state = "on"
    e._hass.states.get = MagicMock(return_value=state_on)

    retry_count = {"n": 0}

    async def fake_retry(entity_id, *, label):
        retry_count["n"] += 1
        return True

    e._call_turn_off_with_verify = fake_retry

    asyncio.run(e.reassert())

    assert retry_count["n"] == 0


def test_v0174_watchdog_still_fires_when_actually_playing():
    """v0.17.4 must NOT regress the intended watchdog behavior: when
    the kid IS actually playing media under ENFORCING, the watchdog
    still fires (the v0.15.4 behavior — pyatv probably dropped, retry)."""
    from unittest.mock import MagicMock
    from custom_components.appletv_mgmt.state import STATE_ENFORCING

    p = _make_profile(tv_shutdown_target=None)
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._is_blocked = True
    e._store.adult_mode_until = MagicMock(return_value=None)

    for active_state in ("playing", "paused", "buffering"):
        state = MagicMock()
        state.state = active_state
        e._hass.states.get = MagicMock(return_value=state)
        retry_count = {"n": 0}

        async def fake_retry(entity_id, *, label, _rc=retry_count):
            _rc["n"] += 1
            return True

        e._call_turn_off_with_verify = fake_retry
        asyncio.run(e.reassert())
        assert retry_count["n"] == 1, (
            f"Watchdog must fire when Apple TV is `{active_state}`"
        )


def test_v0_15_4_watchdog_skipped_under_bypass():
    """v0.15.4: if the user toggled to adult_mode / monitor_only / paused
    while we're still nominally in ENFORCING state machine, the watchdog
    MUST NOT punch through and turn off the Apple TV. The bypass intent
    wins. (Practically: adult_mode usually drops state out of ENFORCING
    on the next tick, but reassert can run before that.)
    """
    from datetime import datetime, timedelta, timezone
    from unittest.mock import MagicMock
    from custom_components.appletv_mgmt.state import STATE_ENFORCING

    p = _make_profile(tv_shutdown_target=None)
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._is_blocked = True

    # Bypass active: adult_mode_until in the future
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    e._store.adult_mode_until = MagicMock(return_value=future)

    state_playing = MagicMock()
    state_playing.state = "playing"
    e._hass.states.get = MagicMock(return_value=state_playing)

    retry_count = {"n": 0}

    async def fake_retry(entity_id, *, label):
        retry_count["n"] += 1
        return False

    e._call_turn_off_with_verify = fake_retry

    asyncio.run(e.reassert())

    assert retry_count["n"] == 0, (
        "Watchdog must respect adult_mode bypass — should_act != ACT"
    )


def test_apply_only_calls_enter_enforcing_on_transition_into():
    """v0.15.2 fix: _apply must only call _enter_enforcing on the
    transition INTO ENFORCING. Calling it every tick while we stay
    in ENFORCING was producing 1 enforce_turn_off_failed audit row +
    (pre-v0.15.1) 1 voice_announcement per coordinator tick (~30s).

    Live-observed 2026-05-25: kid hit budget, state stayed ENFORCING
    for hours, audit log filled with repeated turn_off_failed rows.
    """
    from unittest.mock import MagicMock

    p = _make_profile()
    e = _make_enforcer(p)

    enter_calls = []
    exit_calls = []

    async def spy_enter():
        enter_calls.append(True)

    async def spy_exit(*, reason=None):
        exit_calls.append(reason)

    e._enter_enforcing = spy_enter
    e._exit_enforcing = spy_exit
    e._emit_state_event = MagicMock()

    from custom_components.appletv_mgmt.state import STATE_OK, STATE_ENFORCING

    StateDecision = enforcer_mod.StateDecision

    # Tick 1: OK → ENFORCING. Should call _enter_enforcing once.
    asyncio.run(e._apply(StateDecision(STATE_ENFORCING, None)))
    assert len(enter_calls) == 1, f"transition OK→ENFORCING: expected 1 enter call, got {len(enter_calls)}"

    # Ticks 2-5: STAY in ENFORCING. Should NOT re-call _enter_enforcing.
    for _ in range(4):
        asyncio.run(e._apply(StateDecision(STATE_ENFORCING, None)))
    assert len(enter_calls) == 1, (
        f"staying in ENFORCING: enter_enforcing fired {len(enter_calls)}× "
        f"(should still be 1). This was the bug — fired every tick."
    )
    assert len(exit_calls) == 0

    # Tick 6: ENFORCING → OK. Should call _exit_enforcing once.
    asyncio.run(e._apply(StateDecision(STATE_OK, None)))
    assert len(exit_calls) == 1

    # Tick 7: OK → ENFORCING again. Should fire enter (now total 2).
    asyncio.run(e._apply(StateDecision(STATE_ENFORCING, None)))
    assert len(enter_calls) == 2, "Re-entry into ENFORCING from OK should fire enter again"


# ---------- v0.17.0 F-F: reassert gates BOTH jobs on policy ----------


def test_reassert_in_monitor_mode_no_ops_even_with_drift():
    """v0.17.0 F-F (Sonnet BA audit P1).

    Pre-v0.17.0: reassert() Job 1 (AdGuard drift heal) called
    _enter_enforcing() whenever want_blocked != _is_blocked, REGARDLESS
    of policy. In monitor mode with enable_adguard_block=True the
    drift between state=ENFORCING and _is_blocked=False is permanent
    (monitor mode never sets is_blocked=True), so reassert fired every
    60s: log spam + spurious enforce attempts.

    Post-v0.17.0: policy gate moves to the top, applies to BOTH jobs.
    """
    p, e = _make_pe()
    p.mode = "monitor_only"
    p.enable_adguard_block = True   # narrow F-F repro condition
    e._state = STATE_ENFORCING
    e._is_blocked = False           # drift relative to state

    enter_calls = []

    async def spy_enter():
        enter_calls.append(True)

    e._enter_enforcing = spy_enter

    asyncio.run(e.reassert())

    # Pre-fix: spy_enter would have been called.
    assert len(enter_calls) == 0


# ---------- v0.17.0 F-G: skip AdGuard unblock when never blocked ----------


def test_exit_enforcing_skips_adguard_when_never_blocked_in_monitor():
    """v0.17.0 F-G (Sonnet BA audit P1).

    Pre-v0.17.0: _exit_enforcing always called AdGuard set_blocked(False)
    when enable_adguard_block=True, even when monitor mode had never
    actually set the block. Spurious round-trip to AdGuard + confusing
    log line.

    Post-v0.17.0: if _is_blocked=False AND policy != ACT, skip the
    AdGuard call. The defensive cleanup path (was blocked, user
    toggled enforcement_enabled OFF mid-block) is preserved because
    that case has _is_blocked=True.
    """
    p, e = _make_pe()
    p.mode = "monitor_only"
    p.enable_adguard_block = True
    e._is_blocked = False   # never actually blocked

    e._adguard.set_blocked = AsyncMock()

    asyncio.run(e._exit_enforcing())

    e._adguard.set_blocked.assert_not_called()


def test_exit_enforcing_still_calls_adguard_when_was_actually_blocked():
    """Defensive cleanup path preserved: if _is_blocked=True (real block
    happened), unblock fires regardless of current policy. Covers the
    "user toggled enforcement_enabled off while we were enforcing"
    scenario."""
    p, e = _make_pe()
    p.mode = "monitor_only"  # current mode but we WERE blocked previously
    p.enable_adguard_block = True
    e._is_blocked = True     # we had set the block in a prior enforce cycle

    e._adguard.set_blocked = AsyncMock()

    asyncio.run(e._exit_enforcing())

    e._adguard.set_blocked.assert_called_once()


# ---------- v0.17.0 F-N: force_block under BYPASS no-ops + warns ----------


def test_force_block_under_adult_mode_no_ops_silently_with_warning_log(caplog):
    """v0.17.0 F-N (Sonnet BA audit P2, the owner Q2 decision: silent no-op
    + warning log).

    Pre-v0.17.0: force_block under adult mode set _state=STATE_ENFORCING,
    called _enter_enforcing which hit the adult-mode guard, and left
    _state=ENFORCING + _is_blocked=False → drift-detected spam every
    60s.

    Post-v0.17.0: detect the BYPASS up front, log a warning, return
    without touching state.
    """
    import logging
    p, e = _make_pe()
    # Adult mode active.
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    e._store.adult_mode_until = MagicMock(return_value=future)
    prev_state = e._state

    with caplog.at_level(logging.WARNING):
        asyncio.run(e.force_block())

    # State unchanged.
    assert e._state == prev_state
    # Warning emitted.
    assert any(
        "force_block service called" in rec.message
        for rec in caplog.records
    )


def test_force_block_under_paused_no_ops_silently():
    """Same as adult mode but for paused mode."""
    p, e = _make_pe()
    p.mode = "paused"
    prev_state = e._state

    asyncio.run(e.force_block())

    assert e._state == prev_state


def test_force_block_under_act_still_works():
    """Sanity: when policy IS ACT, force_block works as documented."""
    p, e = _make_pe()
    # ACT path: enforced mode, no adult mode.
    p.mode = "enforced"
    e._store.adult_mode_until = MagicMock(return_value=None)
    e._store.is_adult_mode_active = MagicMock(return_value=False)
    # Stub _enter_enforcing so we don't go through the real side-effects path.
    enter_calls = []

    async def spy_enter():
        enter_calls.append(True)

    e._enter_enforcing = spy_enter

    asyncio.run(e.force_block())

    assert e._state == STATE_ENFORCING
    assert len(enter_calls) == 1


def _make_pe():
    """Helper: build a (profile, enforcer) pair using the existing fixtures.
    Lives here so the v0.17.0 tests don't have to thread through the
    original `_run` helper's argument set."""
    p = _make_profile()
    e = _make_enforcer(p)
    # Tests assume the standard ACT decision unless they override.
    e._store.is_adult_mode_active = MagicMock(return_value=False)
    e._store.adult_mode_until = MagicMock(return_value=None)
    return p, e
