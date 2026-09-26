"""Tests for v0.16.5 — group-budget enforcement persists across off-periods.

Bug being prevented (live-reported 2026-05-30, Heimkinoaaa profile):

    Movies group budget = 60 min, daily budget = 200 min.
    Kid watched 60 min of Netflix → group exhausted → state=ENFORCING.
    Kid stops watching (or pyatv's turn_off succeeds). current_group becomes
    None. Daily binding still has 140 min left → state relaxes to OK →
    `_exit_enforcing` fires → `_reactivation_count` resets to 0.

    Kid restarts the Apple TV → opens Netflix → state cycles through a
    FRESH WARN→GRACE→ENFORCING (because group is exhausted again). The
    `watch_reactivation` edge detector never sees an inactive→active
    transition WHILE state=ENFORCING, so the reactivation voices never
    fire and the parent push never lands.

The fix: when ANY group is exhausted today, pin enforcement during
periods where the kid is NOT actively inside a different (non-exhausted)
group. This keeps state=ENFORCING across the standby gap so the
reactivation counter survives and the edge detector lands the voice.
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
                stub.INACTIVE_MEDIA_STATES = {
                    "off", "standby", "unavailable", "idle", "unknown", None,
                }
                stub.UNKNOWN_APP_BUNDLE_ID = "unknown"
            sys.modules[full] = stub


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


from custom_components.appletv_mgmt.const import (  # noqa: E402
    STATE_ENFORCING,
    STATE_GRACE,
    STATE_OK,
    STATE_WARNING,
)


def _make_profile(
    *,
    daily_budget_min=200,
    warn_thresholds_min=[5],
    grace_seconds=60,
    friendly="Bildschirmzeit ist vorbei. Apple TV bitte aus lassen.",
    stern="Apple TV bleibt aus. Die Eltern wurden jetzt informiert.",
    notify_media_player="media_player.sonos_kitchen",
):
    storage_mod = sys.modules["custom_components.appletv_mgmt.storage"]
    return storage_mod.Profile(
        id="p1",
        display_name="Living Room",
        apple_tv_entity_id="media_player.heimkinoaaa",
        adguard_client_name="AppleTV",
        daily_budget_min=daily_budget_min,
        grace_seconds=grace_seconds,
        warn_thresholds_min=warn_thresholds_min,
        idle_grace_minutes=5,
        notify_media_player_entity_id=notify_media_player,
        reactivation_message_friendly=friendly,
        reactivation_message_stern=stern,
    )


def _make_enforcer(profile):
    hass = MagicMock()
    adguard = MagicMock()
    adguard.set_blocked = AsyncMock(return_value=None)
    store = MagicMock()
    store.is_adult_mode_active = MagicMock(return_value=False)
    store.adult_mode_until = MagicMock(return_value=None)
    e = enforcer_mod.EnforcementController(hass, adguard, profile, store=store)
    _VOICE_SPEAK.reset_mock()
    sys.modules["custom_components.appletv_mgmt.voice_notifier"].speak = _VOICE_SPEAK
    sys.modules["custom_components.appletv_mgmt.audit"].record_admin_action.reset_mock()
    return e


def _set_apple_tv_state(e, state_str: str | None):
    if state_str is None:
        e._hass.states.get = MagicMock(return_value=None)
    else:
        s = MagicMock()
        s.state = state_str
        e._hass.states.get = MagicMock(return_value=s)


NOW = datetime(2026, 5, 30, 13, 14, 0, tzinfo=timezone.utc)


# ---------- Bug repro: enforcement persists when kid stops watching ----------


def test_group_exhausted_pins_enforcement_when_kid_stops_watching():
    """Live bug repro 2026-05-30.

    Scenario: kid maxed out the movies group (60/60). State=ENFORCING.
    Apple TV goes to standby → current_group becomes None. Daily budget
    still has 140 min remaining. With the pre-fix evaluator the state
    relaxes to OK because the daily binding wins. After the fix, state
    stays ENFORCING because the movies group is exhausted for today.
    """
    p = _make_profile(daily_budget_min=200, warn_thresholds_min=[5])
    p.group_budgets = {"movies": 60}
    e = _make_enforcer(p)

    # Set initial state to ENFORCING (kid hit the group limit while watching).
    e._state = STATE_ENFORCING

    # Apple TV now in standby; kid stopped watching.
    asyncio.run(e.evaluate(
        used_seconds=60 * 60,                 # 60 min used overall (all movies)
        now=NOW,
        current_group=None,                   # kid no longer in any app
        current_group_used_seconds=0,
        current_group_budget_seconds=None,
        group_totals_seconds={"movies": 60 * 60},
        group_budgets_seconds={"movies": 60 * 60},
    ))

    assert e.state == STATE_ENFORCING, (
        f"Expected ENFORCING after kid stops watching, got {e.state}. "
        "Group movies is exhausted (60/60 min); state must persist so "
        "the reactivation cycle works when kid restarts the Apple TV."
    )
    assert e.enforce_reason == "group:movies"


def test_group_exhausted_does_not_pin_when_kid_switches_to_other_group_with_budget():
    """The "switch groups to gain time" pathway stays open.

    Scenario: movies exhausted (60/60), YouTube has full 30 min budget,
    kid is currently in YouTube. State should be OK — YouTube usage is
    permitted even though movies is exhausted. Each group budget is
    independent.
    """
    p = _make_profile(daily_budget_min=200, warn_thresholds_min=[5])
    p.group_budgets = {"movies": 60, "youtube": 30}
    e = _make_enforcer(p)

    asyncio.run(e.evaluate(
        used_seconds=60 * 60,
        now=NOW,
        current_group="youtube",
        current_group_used_seconds=0,
        current_group_budget_seconds=30 * 60,    # YouTube budget fresh
        group_totals_seconds={"movies": 60 * 60, "youtube": 0},
        group_budgets_seconds={"movies": 60 * 60, "youtube": 30 * 60},
    ))

    assert e.state == STATE_OK
    # YouTube binds (30 min remaining > warn threshold).
    assert e.enforce_reason is None


def test_group_exhausted_pins_enforcement_when_kid_in_other_group_with_no_budget():
    """Notes / unbudgeted apps don't unlock the device after group exhaustion.

    Scenario: movies exhausted (60/60). Kid opens an unbudgeted app —
    current_group resolves to "other" with no budget set. Without the
    pin this would relax to OK (daily budget has plenty), then the kid
    could swap back to Netflix and burn another fresh WARN→GRACE cycle.
    """
    p = _make_profile(daily_budget_min=200, warn_thresholds_min=[5])
    p.group_budgets = {"movies": 60}
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING

    asyncio.run(e.evaluate(
        used_seconds=60 * 60,
        now=NOW,
        current_group="other",
        current_group_used_seconds=5 * 60,
        current_group_budget_seconds=None,     # no budget for "other"
        group_totals_seconds={"movies": 60 * 60, "other": 5 * 60},
        group_budgets_seconds={"movies": 60 * 60},
    ))

    assert e.state == STATE_ENFORCING
    assert e.enforce_reason == "group:movies"


def test_no_exhausted_group_does_not_pin():
    """Without the bug condition, evaluator stays back-compatible.

    Two groups, both with remaining budget, current_group=None: state
    should follow the daily binding (back-compat, no pinning).
    """
    p = _make_profile(daily_budget_min=200, warn_thresholds_min=[5])
    p.group_budgets = {"movies": 60, "youtube": 30}
    e = _make_enforcer(p)

    asyncio.run(e.evaluate(
        used_seconds=20 * 60,
        now=NOW,
        current_group=None,
        group_totals_seconds={"movies": 30 * 60, "youtube": 10 * 60},
        group_budgets_seconds={"movies": 60 * 60, "youtube": 30 * 60},
    ))

    assert e.state == STATE_OK
    assert e.enforce_reason is None


def test_no_group_dicts_passed_falls_back_to_legacy_behavior():
    """Coordinator on an older code path or test fixture that doesn't
    pass the all-groups dicts must keep working — daily binding only.
    """
    p = _make_profile(daily_budget_min=60, warn_thresholds_min=[5])
    e = _make_enforcer(p)

    asyncio.run(e.evaluate(used_seconds=10 * 60, now=NOW))
    assert e.state == STATE_OK


def test_pin_holds_state_machine_across_repeated_ticks():
    """Three consecutive ticks during the standby period must all keep
    state at ENFORCING (the bug pattern: first tick relaxed to OK, then
    the cycle restarted on next active app)."""
    p = _make_profile(daily_budget_min=200, warn_thresholds_min=[5])
    p.group_budgets = {"movies": 60}
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING

    for _ in range(3):
        asyncio.run(e.evaluate(
            used_seconds=60 * 60,
            now=NOW,
            current_group=None,
            group_totals_seconds={"movies": 60 * 60},
            group_budgets_seconds={"movies": 60 * 60},
        ))
        assert e.state == STATE_ENFORCING


# ---------- Cycle test: reactivation counter survives on→off→on ----------


def test_reactivation_counter_survives_off_period_when_group_exhausted():
    """The end-to-end bug: kid in ENFORCING, stops, restarts, counter
    must persist so the friendly voice fires on first re-on.

    Pre-fix: state relaxes to OK when kid stops → _exit_enforcing →
    counter=0 → first re-on counts as #1 fresh but the kid is now back
    in a fresh enforcing cycle so the audit/voice timing is wrong AND
    (worse) repeated cycles would never escalate to stern.

    Post-fix: state stays ENFORCING across the off period → counter
    persists → first edge detected after kid restarts the Apple TV
    counts as #1 (friendly) for THIS cycle, then a 2nd off→on counts
    as #2 (stern + parent push).
    """
    p = _make_profile(daily_budget_min=200, warn_thresholds_min=[5])
    p.group_budgets = {"movies": 60}
    p.notify_parent_target = "mobile_app_marc"
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._was_apple_tv_active_last_tick = True   # kid was watching Netflix
    e._hass.services.async_call = AsyncMock()

    audit_record = sys.modules[
        "custom_components.appletv_mgmt.audit"
    ].record_admin_action

    # 1. Kid stops → standby. Pre-fix: state relaxed to OK. Post-fix: stays.
    _set_apple_tv_state(e, "standby")
    asyncio.run(e.evaluate(
        used_seconds=60 * 60,
        now=NOW,
        current_group=None,
        group_totals_seconds={"movies": 60 * 60},
        group_budgets_seconds={"movies": 60 * 60},
    ))
    asyncio.run(e.watch_reactivation())
    assert e.state == STATE_ENFORCING
    assert e._reactivation_count == 0
    assert _VOICE_SPEAK.call_count == 0  # no voice fires during standby

    # 2. Kid restarts Apple TV → inactive→active edge → 1st reactivation.
    _set_apple_tv_state(e, "playing")
    asyncio.run(e.watch_reactivation())
    assert e._reactivation_count == 1, (
        "First re-on under ENFORCING should count as reactivation #1 "
        "(friendly voice). If state had relaxed to OK during standby "
        "the counter would have reset and this edge would be missed."
    )
    # Friendly voice fired (count == 1 → friendly template).
    assert _VOICE_SPEAK.call_count == 1
    # Reactivation audit row #1 recorded.
    react_audit = [
        c for c in audit_record.call_args_list
        if c.kwargs.get("action") == "reactivation"
    ]
    assert len(react_audit) == 1
    assert react_audit[0].kwargs.get("detail") == "#1"
    # No parent push on first event.
    e._hass.services.async_call.assert_not_called()

    # 3. Kid stops again → standby. State stays ENFORCING (movies still 60/60).
    _set_apple_tv_state(e, "standby")
    asyncio.run(e.evaluate(
        used_seconds=60 * 60,
        now=NOW,
        current_group=None,
        group_totals_seconds={"movies": 60 * 60},
        group_budgets_seconds={"movies": 60 * 60},
    ))
    asyncio.run(e.watch_reactivation())
    assert e.state == STATE_ENFORCING
    assert e._reactivation_count == 1

    # 4. Kid restarts AGAIN → 2nd reactivation → stern + parent push.
    _set_apple_tv_state(e, "playing")
    asyncio.run(e.watch_reactivation())
    assert e._reactivation_count == 2
    # Stern voice fired (count >= 2 → stern template).
    assert _VOICE_SPEAK.call_count == 2
    # Parent push fired on the stern event.
    e._hass.services.async_call.assert_called_once()
    push_call = e._hass.services.async_call.call_args
    assert push_call.args[0] == "notify"
    assert push_call.args[1] == "mobile_app_marc"
    # parent_notified audit row recorded.
    parent_audit = [
        c for c in audit_record.call_args_list
        if c.kwargs.get("action") == "parent_notified"
    ]
    assert len(parent_audit) == 1
    assert "#2" in parent_audit[0].kwargs.get("detail")


# ---------- Quiet windows still take precedence ----------


def test_quiet_window_still_pins_when_no_group_exhausted():
    """Pre-existing behavior preserved: an active quiet window pins
    enforcement regardless of group state."""
    storage_mod = sys.modules["custom_components.appletv_mgmt.storage"]
    profile = storage_mod.Profile(
        id="p1",
        display_name="Living Room",
        apple_tv_entity_id="media_player.heimkinoaaa",
        adguard_client_name="AppleTV",
        daily_budget_min=200,
        grace_seconds=60,
        warn_thresholds_min=[5],
        idle_grace_minutes=5,
        quiet_windows="00:00-23:59:sleep",
        group_budgets={"movies": 60},
    )
    e = _make_enforcer(profile)

    asyncio.run(e.evaluate(
        used_seconds=10 * 60,
        now=NOW,
        current_group=None,
        group_totals_seconds={"movies": 10 * 60},
        group_budgets_seconds={"movies": 60 * 60},
    ))
    assert e.enforce_reason is not None
    assert e.enforce_reason.startswith("quiet:")


# ---------- Adult mode bypasses pin (same as before) ----------


def test_adult_mode_bypasses_group_exhausted_pin():
    """Adult mode short-circuits to OK regardless of group exhaustion —
    parent has explicitly granted bypass time."""
    p = _make_profile(daily_budget_min=200, warn_thresholds_min=[5])
    p.group_budgets = {"movies": 60}
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING

    asyncio.run(e.evaluate(
        used_seconds=60 * 60,
        now=NOW,
        current_group=None,
        group_totals_seconds={"movies": 60 * 60},
        group_budgets_seconds={"movies": 60 * 60},
        adult_mode_active=True,
    ))
    assert e.state == STATE_OK
    assert e.enforce_reason is None


# ============================================================================
# v0.17.0 Cohort 3 — F-D: pre-exhaustion latch (close-reopen defeat vector)
# ============================================================================


def test_warn_or_grace_persists_when_kid_closes_app_with_group_binding():
    """v0.17.0 F-D (Opus BA audit P1).

    Pre-fix defeat: kid is at GRACE because group:movies has 1 min left.
    Kid closes Netflix (current_group → None) → daily binding has hours
    left → state relaxes to OK → `_exit_enforcing` fires → `_grace_started_at`
    cleared. Kid reopens Netflix → fresh WARN voice → fresh GRACE window
    → ~75 s of free time gained per cycle.

    Post-fix: F-D latch promotes current_group=None to the latched
    group:movies. Group remaining is now `binding_remaining_s` for the
    state machine. With 1 min (60 s) remaining and warn_threshold 5 min,
    `state.compute_next_state` returns WARNING (not OK) — the state
    machine doesn't preserve GRACE when remaining bounces above 0 — but
    the critical anti-defeat property holds: state is NOT relaxed to OK,
    so a subsequent app-reopen doesn't get to walk a fresh WARN→GRACE
    cycle from scratch.
    """
    p = _make_profile(daily_budget_min=200, warn_thresholds_min=[5])
    p.group_budgets = {"movies": 60}
    e = _make_enforcer(p)

    # Set up: kid is at GRACE (in the v0.17.0 latch precondition).
    e._state = STATE_GRACE
    e._enforce_reason = "group:movies"

    # Now the kid closes the app — current_group=None.
    asyncio.run(e.evaluate(
        used_seconds=59 * 60,                 # 59 min used overall
        now=NOW,
        current_group=None,                   # kid stopped watching
        current_group_used_seconds=0,
        current_group_budget_seconds=None,
        group_totals_seconds={"movies": 59 * 60},   # 1 min remaining
        group_budgets_seconds={"movies": 60 * 60},
    ))

    # The critical property: state DID NOT relax to OK. F-D latch
    # preserved the binding so a subsequent re-open of the app
    # continues from WARN territory, not from a fresh OK→WARN→GRACE
    # walk.
    assert e.state != STATE_OK, (
        f"Expected state to NOT relax to OK (F-D latch), got {e.state}. "
        f"Without the latch the kid earns ~75 s of free time by closing "
        f"the app for one tick mid-grace and reopening."
    )
    assert e.state in (STATE_WARNING, STATE_GRACE)
    assert e.enforce_reason == "group:movies"


def test_warning_persists_when_kid_closes_app_with_group_binding():
    """Same as above but at WARN level — kid still has 3 min of movies
    left, closes the app, latch keeps state at WARNING."""
    p = _make_profile(daily_budget_min=200, warn_thresholds_min=[5])
    p.group_budgets = {"movies": 60}
    e = _make_enforcer(p)

    e._state = STATE_WARNING
    e._enforce_reason = "group:movies"

    asyncio.run(e.evaluate(
        used_seconds=57 * 60,
        now=NOW,
        current_group=None,
        current_group_used_seconds=0,
        current_group_budget_seconds=None,
        group_totals_seconds={"movies": 57 * 60},   # 3 min remaining
        group_budgets_seconds={"movies": 60 * 60},
    ))

    assert e.state == STATE_WARNING
    assert e.enforce_reason == "group:movies"


def test_latch_clears_when_kid_switches_to_non_binding_group():
    """When the kid actually picks a different group with budget, the
    latch defers to the new group's binding (normal flow). Without this
    the latch would falsely keep state at WARN/GRACE based on a stale
    group binding."""
    p = _make_profile(daily_budget_min=200, warn_thresholds_min=[5])
    p.group_budgets = {"movies": 60, "youtube": 30}
    e = _make_enforcer(p)

    e._state = STATE_WARNING
    e._enforce_reason = "group:movies"

    asyncio.run(e.evaluate(
        used_seconds=57 * 60,
        now=NOW,
        # Kid is now in YouTube (different group) with 30 min remaining.
        current_group="youtube",
        current_group_used_seconds=0,
        current_group_budget_seconds=30 * 60,
        group_totals_seconds={"movies": 57 * 60, "youtube": 0},
        group_budgets_seconds={"movies": 60 * 60, "youtube": 30 * 60},
    ))

    # YouTube binds (30 min ≫ warn_threshold) → state relaxes to OK.
    assert e.state == STATE_OK
    # And enforce_reason is cleared (no more binding constraint).
    assert e.enforce_reason is None


def test_latch_does_not_engage_when_group_fully_exhausted():
    """When the group is fully exhausted, v0.16.5 pin takes precedence —
    state stays at ENFORCING, not at WARN/GRACE. F-D's latch only
    activates for the pre-exhaustion case (group_used < group_budget)."""
    p = _make_profile(daily_budget_min=200, warn_thresholds_min=[5])
    p.group_budgets = {"movies": 60}
    e = _make_enforcer(p)

    # Movies fully exhausted; kid was at GRACE briefly before hitting 0.
    e._state = STATE_GRACE
    e._enforce_reason = "group:movies"

    asyncio.run(e.evaluate(
        used_seconds=60 * 60,
        now=NOW,
        current_group=None,
        group_totals_seconds={"movies": 60 * 60},   # exhausted
        group_budgets_seconds={"movies": 60 * 60},
    ))

    # v0.16.5 pin engaged: state moves to ENFORCING (or stays in
    # GRACE→ENFORCING flow). Either way, NOT relaxed to OK.
    assert e.state in (STATE_GRACE, STATE_ENFORCING)
    assert e.enforce_reason == "group:movies"


def test_latch_does_not_engage_when_state_is_ok():
    """Sanity: latch only activates when state is WARN/GRACE. From OK
    with no group passed in, normal evaluation runs and (with no group
    exhausted) we get OK."""
    p = _make_profile(daily_budget_min=200, warn_thresholds_min=[5])
    p.group_budgets = {"movies": 60}
    e = _make_enforcer(p)

    e._state = STATE_OK
    e._enforce_reason = None  # was OK, no binding

    asyncio.run(e.evaluate(
        used_seconds=30 * 60,
        now=NOW,
        current_group=None,
        group_totals_seconds={"movies": 30 * 60},
        group_budgets_seconds={"movies": 60 * 60},
    ))

    assert e.state == STATE_OK
    assert e.enforce_reason is None
