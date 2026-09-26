"""Tests for the v0.16.0 re-on detection (`watch_reactivation`).

The highest-value v0.16.0 feature: detects Apple TV inactive→active
transitions under ENFORCING and fires a social-pressure voice (and a
parent push on the 2nd+ event) to deter the kid restarting the device.

Stubs HA + sibling modules the same way test_enforcement_failed.py /
test_countdown_voice.py do, then drives `watch_reactivation()` directly.
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

# Per-test voice spy. We claim it on `_make_enforcer` so it survives
# any sibling test file that overwrote the voice_notifier stub.
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


def _get_audit_record() -> MagicMock:
    """Return the live audit.record_admin_action — whichever mock the
    audit-stub module currently exposes. We DON'T overwrite it: sibling
    test files (test_select.py) capture a reference to the audit stub
    at import time and assume it never changes identity."""
    return sys.modules["custom_components.appletv_mgmt.audit"].record_admin_action

spec = importlib.util.spec_from_file_location(
    "custom_components.appletv_mgmt.enforcer", PKG / "enforcer.py"
)
if "custom_components.appletv_mgmt.enforcer" not in sys.modules:
    enforcer_mod = importlib.util.module_from_spec(spec)
    sys.modules["custom_components.appletv_mgmt.enforcer"] = enforcer_mod
    spec.loader.exec_module(enforcer_mod)
else:
    enforcer_mod = sys.modules["custom_components.appletv_mgmt.enforcer"]


from custom_components.appletv_mgmt.const import STATE_ENFORCING, STATE_OK  # noqa: E402


def _make_profile(
    *,
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
        daily_budget_min=60,
        grace_seconds=60,
        warn_thresholds_min=[5],
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
    # Reclaim the voice spy in case a sibling test file overwrote it
    # during ITS module-load. Do NOT touch the audit module's
    # record_admin_action — test_select.py captures it by reference at
    # import time and breaks if we swap it.
    sys.modules["custom_components.appletv_mgmt.voice_notifier"].speak = _VOICE_SPEAK
    # Reset the LIVE audit mock so each test starts with a clean count.
    _get_audit_record().reset_mock()
    return e


def _set_apple_tv_state(e, state_str: str | None):
    """Make hass.states.get return a state object with `.state == state_str`,
    or None if state_str is None."""
    if state_str is None:
        e._hass.states.get = MagicMock(return_value=None)
    else:
        s = MagicMock()
        s.state = state_str
        e._hass.states.get = MagicMock(return_value=s)


# ---------- happy path: inactive→active under ENFORCING fires once ----------


def test_first_reactivation_under_enforcing_increments_count_and_fires_friendly():
    p = _make_profile()
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._was_apple_tv_active_last_tick = False  # baseline: inactive last tick
    _set_apple_tv_state(e, "playing")  # transitioned to active

    asyncio.run(e.watch_reactivation())

    assert e._reactivation_count == 1
    assert e._was_apple_tv_active_last_tick is True
    # Friendly voice fired (the only template that matches count==1)
    assert _VOICE_SPEAK.call_count == 1


def test_second_reactivation_fires_stern_message():
    p = _make_profile()
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._reactivation_count = 1  # already had 1st event
    e._was_apple_tv_active_last_tick = False  # kid turned off briefly, back on

    _set_apple_tv_state(e, "playing")
    asyncio.run(e.watch_reactivation())

    assert e._reactivation_count == 2
    # Stern voice fired (count >= 2)
    assert _VOICE_SPEAK.call_count == 1


def test_third_plus_reactivation_keeps_using_stern():
    p = _make_profile()
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._reactivation_count = 2  # already had 2 events
    e._was_apple_tv_active_last_tick = False

    _set_apple_tv_state(e, "playing")
    asyncio.run(e.watch_reactivation())

    assert e._reactivation_count == 3
    assert _VOICE_SPEAK.call_count == 1  # stern fires again


# ---------- no transition: active→active doesn't re-fire ----------


def test_active_then_active_does_not_re_fire():
    """Two ticks both with the Apple TV in 'playing': only the first
    edge counts. Without the flag the voice would re-fire every 30s
    while the kid kept watching — defeating the whole social-pressure
    intent (he'd just tune it out)."""
    p = _make_profile()
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._was_apple_tv_active_last_tick = False
    _set_apple_tv_state(e, "playing")

    # Tick 1: inactive → active → fires
    asyncio.run(e.watch_reactivation())
    assert _VOICE_SPEAK.call_count == 1
    assert e._reactivation_count == 1

    # Tick 2: still playing → no transition → no fire
    asyncio.run(e.watch_reactivation())
    assert _VOICE_SPEAK.call_count == 1
    assert e._reactivation_count == 1


def test_active_then_inactive_then_active_counts_as_two_events():
    """Kid turns Apple TV off (or pyatv successfully sleeps it), then
    turns it back on — that's a fresh edge, count++."""
    p = _make_profile()
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._was_apple_tv_active_last_tick = False

    # Tick 1: inactive → active (1st event)
    _set_apple_tv_state(e, "playing")
    asyncio.run(e.watch_reactivation())
    assert e._reactivation_count == 1

    # Tick 2: active → inactive (off — flag drops to False, count unchanged)
    _set_apple_tv_state(e, "off")
    asyncio.run(e.watch_reactivation())
    assert e._reactivation_count == 1
    assert e._was_apple_tv_active_last_tick is False

    # Tick 3: inactive → active again (2nd event)
    _set_apple_tv_state(e, "playing")
    asyncio.run(e.watch_reactivation())
    assert e._reactivation_count == 2


# ---------- gate: state must be ENFORCING ----------


def test_not_enforcing_resets_edge_flag_and_skips():
    p = _make_profile()
    e = _make_enforcer(p)
    e._state = STATE_OK
    e._was_apple_tv_active_last_tick = True  # stale carry-over
    _set_apple_tv_state(e, "playing")

    asyncio.run(e.watch_reactivation())

    # Reset the flag to avoid stale True from a prior cycle.
    assert e._was_apple_tv_active_last_tick is False
    assert e._reactivation_count == 0
    assert _VOICE_SPEAK.call_count == 0


# ---------- gate: should_act bypass silences ----------


def test_adult_mode_bypass_silences_reactivation():
    """The watchdog already respects adult_mode; the re-on detector
    must too — otherwise the kid getting a "you're not supposed to be
    watching" voice while the parent has explicitly granted bypass time
    is absurd and breaks trust."""
    p = _make_profile()
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._was_apple_tv_active_last_tick = False
    _set_apple_tv_state(e, "playing")

    # Adult mode active
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    e._store.adult_mode_until = MagicMock(return_value=future)

    asyncio.run(e.watch_reactivation())

    assert e._reactivation_count == 0
    assert _VOICE_SPEAK.call_count == 0


def test_paused_mode_silences_reactivation():
    p = _make_profile()
    p.mode = "paused"
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._was_apple_tv_active_last_tick = False
    _set_apple_tv_state(e, "playing")

    asyncio.run(e.watch_reactivation())

    assert e._reactivation_count == 0
    assert _VOICE_SPEAK.call_count == 0


# ---------- gate: Apple TV must be active ----------


def test_apple_tv_inactive_sets_flag_false_and_skips():
    p = _make_profile()
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._was_apple_tv_active_last_tick = True  # was active
    _set_apple_tv_state(e, "off")

    asyncio.run(e.watch_reactivation())

    assert e._was_apple_tv_active_last_tick is False
    assert e._reactivation_count == 0
    assert _VOICE_SPEAK.call_count == 0


def test_apple_tv_entity_missing_skips_quietly():
    """During an HA restart or pyatv config change the Apple TV entity
    might be temporarily None. Treat as inactive (won't fire, won't
    crash)."""
    p = _make_profile()
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._was_apple_tv_active_last_tick = True
    _set_apple_tv_state(e, None)  # hass.states.get returns None

    asyncio.run(e.watch_reactivation())
    assert e._was_apple_tv_active_last_tick is False
    assert e._reactivation_count == 0


# ---------- opt-in: empty messages stay silent but still count ----------


def test_empty_friendly_message_still_counts_and_logs_audit():
    """Even if reactivation_message_friendly is empty (the v0.15.9-upgrade
    default), the count must increment and the audit row must land so the
    dashboard shows the attempt."""
    p = _make_profile(friendly="")
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._was_apple_tv_active_last_tick = False
    _set_apple_tv_state(e, "playing")

    asyncio.run(e.watch_reactivation())

    assert e._reactivation_count == 1
    assert _VOICE_SPEAK.call_count == 0  # no voice (no template)
    # But the reactivation audit row landed.
    audit_calls = [
        c for c in _get_audit_record().call_args_list
        if c.kwargs.get("action") == "reactivation"
    ]
    assert len(audit_calls) == 1
    assert audit_calls[0].kwargs.get("detail") == "#1"


# ---------- audit rows ----------


def test_first_reactivation_records_audit_row():
    p = _make_profile()
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._was_apple_tv_active_last_tick = False
    _set_apple_tv_state(e, "playing")

    asyncio.run(e.watch_reactivation())

    audit_calls = [
        c for c in _get_audit_record().call_args_list
        if c.kwargs.get("action") == "reactivation"
    ]
    assert len(audit_calls) == 1
    assert audit_calls[0].kwargs.get("detail") == "#1"
    assert audit_calls[0].kwargs.get("actor") == "system"


# ---------- latch reset on _exit_enforcing ----------


def test_exit_enforcing_resets_reactivation_count_and_flag():
    """When the kid eventually gives up (or midnight rollover drops out
    of ENFORCING), the next ENFORCING cycle must start at "first re-on"
    again — otherwise tomorrow's 1st event would already be stern."""
    p = _make_profile()
    e = _make_enforcer(p)
    e._reactivation_count = 7
    e._was_apple_tv_active_last_tick = True

    asyncio.run(e._exit_enforcing())

    assert e._reactivation_count == 0
    assert e._was_apple_tv_active_last_tick is False


# ---------- v0.16.0 Phase D — parent push notification ----------


def test_notify_parent_uses_explicit_target_when_set():
    """notify_parent_target='mobile_app_your_phone' → notify.mobile_app_your_phone."""
    p = _make_profile()
    p.notify_parent_target = "mobile_app_your_phone"
    e = _make_enforcer(p)
    e._reactivation_count = 2
    e._hass.services.async_call = AsyncMock()

    asyncio.run(e._notify_parent())

    e._hass.services.async_call.assert_called_once()
    call = e._hass.services.async_call.call_args
    # services.async_call(domain, service, data, blocking=...)
    assert call.args[0] == "notify"
    assert call.args[1] == "mobile_app_your_phone"
    payload = call.args[2]
    assert "title" in payload and "message" in payload
    assert payload["title"] == "Living Room: TV defeat"
    assert "Apple TV 2" in payload["message"]


def test_notify_parent_falls_back_to_notify_when_target_empty():
    """Empty notify_parent_target → use HA's default 'notify.notify' fanout."""
    p = _make_profile()
    p.notify_parent_target = ""  # default
    e = _make_enforcer(p)
    e._reactivation_count = 2
    e._hass.services.async_call = AsyncMock()

    asyncio.run(e._notify_parent())

    call = e._hass.services.async_call.call_args
    assert call.args[0] == "notify"
    assert call.args[1] == "notify"


def test_notify_parent_records_audit_row():
    """parent_notified audit row lands even if notify succeeds (so the
    panel's dashboard can count parent notifications across the cycle)."""
    p = _make_profile()
    p.notify_parent_target = "mobile_app_marc"
    e = _make_enforcer(p)
    e._reactivation_count = 3
    e._hass.services.async_call = AsyncMock()

    asyncio.run(e._notify_parent())

    audit_calls = [
        c for c in _get_audit_record().call_args_list
        if c.kwargs.get("action") == "parent_notified"
    ]
    assert len(audit_calls) == 1
    detail = audit_calls[0].kwargs.get("detail")
    assert "#3" in detail
    assert "notify.mobile_app_marc" in detail
    assert audit_calls[0].kwargs.get("actor") == "system"


def test_notify_parent_swallows_service_failure_and_records_failed_audit():
    """If notify.<target> isn't installed (or HA raises for any reason),
    _notify_parent must NOT crash — would prevent subsequent re-on
    events from being detected. Audit row still lands, marked failed."""
    p = _make_profile()
    p.notify_parent_target = "missing_service"
    e = _make_enforcer(p)
    e._reactivation_count = 2
    e._hass.services.async_call = AsyncMock(
        side_effect=RuntimeError("notify service not found")
    )

    # Must not raise.
    asyncio.run(e._notify_parent())

    audit_calls = [
        c for c in _get_audit_record().call_args_list
        if c.kwargs.get("action") == "parent_notified"
    ]
    assert len(audit_calls) == 1
    assert "(failed)" in audit_calls[0].kwargs.get("detail")


# ---------- end-to-end: 2nd re-on triggers BOTH stern voice + parent push ----------


def test_second_reactivation_triggers_voice_and_parent_push():
    p = _make_profile()
    p.notify_parent_target = "mobile_app_marc"
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._reactivation_count = 1  # 2nd event coming up
    e._was_apple_tv_active_last_tick = False
    _set_apple_tv_state(e, "playing")
    e._hass.services.async_call = AsyncMock()

    asyncio.run(e.watch_reactivation())

    # Stern voice fired
    assert _VOICE_SPEAK.call_count == 1
    # Parent push fired
    e._hass.services.async_call.assert_called_once()
    call = e._hass.services.async_call.call_args
    assert call.args[1] == "mobile_app_marc"


def test_first_reactivation_does_not_trigger_parent_push():
    """1st event is friendly + audit only — no parent notification.
    The parent only gets pinged on the *defeat attempt* (count >= 2)."""
    p = _make_profile()
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._was_apple_tv_active_last_tick = False
    _set_apple_tv_state(e, "playing")
    e._hass.services.async_call = AsyncMock()

    asyncio.run(e.watch_reactivation())

    # Friendly voice fired
    assert _VOICE_SPEAK.call_count == 1
    # But NO parent push
    e._hass.services.async_call.assert_not_called()


def test_third_plus_reactivation_keeps_pushing_parent():
    """Each defeat attempt past the 2nd keeps notifying the parent."""
    p = _make_profile()
    p.notify_parent_target = "mobile_app_marc"
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._reactivation_count = 2
    e._was_apple_tv_active_last_tick = False
    _set_apple_tv_state(e, "playing")
    e._hass.services.async_call = AsyncMock()

    asyncio.run(e.watch_reactivation())

    e._hass.services.async_call.assert_called_once()
    # Detail should say #3 (we incremented from 2 to 3 in the call)
    audit_calls = [
        c for c in _get_audit_record().call_args_list
        if c.kwargs.get("action") == "parent_notified"
    ]
    assert "#3" in audit_calls[0].kwargs.get("detail")


# ---------- v0.17.0 F-B: adult mode preserves the counter ----------


def test_reactivation_count_survives_adult_mode_AND_increments_on_post_adult_re_arm():
    """v0.17.0 F-B (Sonnet BA audit P0).

    Defeat mechanic the chip-spec didn't fully cover: kid earns
    reactivation #1 → parent grants adult mode (to watch a movie) →
    adult mode ends → kid restarts ATV.

    Pre-fix: `_exit_enforcing` resets `_reactivation_count = 0` on
    EVERY exit, including the adult-mode-induced one. So the kid's
    next re-arm reads as "reactivation #1" again — friendly voice,
    no parent push.

    Post-fix: `_exit_enforcing` accepts a `reason` kwarg. Adult-mode
    exits preserve the counter. The next re-arm (a real inactive→active
    edge AFTER adult mode ends and ENFORCING resumes) increments
    `_reactivation_count` to 2 → stern voice + parent push fire as
    designed.

    The edge-detector seed in `_enter_enforcing` is critical here: if
    the kid is *already* watching when adult mode ends (continued from
    parent's session), `_was_apple_tv_active_last_tick` seeds to True
    → no False→True edge on the next tick → counter stays at 1. So
    the test simulates the realistic case: kid stops watching during
    the adult-mode window (or pyatv finally sleeps the ATV when state
    re-enters ENFORCING), THEN restarts. That gives the edge detector
    something to fire on.
    """
    p = _make_profile()
    p.notify_parent_target = "mobile_app_marc"
    e = _make_enforcer(p)

    # ----- Step 1: kid already at reactivation #1 under ENFORCING -----
    e._state = STATE_ENFORCING
    e._reactivation_count = 1
    e._was_apple_tv_active_last_tick = True  # kid watching post-#1
    assert e._reactivation_count == 1

    # ----- Step 2: adult mode kicks in mid-cycle -----
    # The evaluate() adult-mode branch (enforcer.py:236-256) sets state
    # to OK and calls `_apply(StateDecision(STATE_OK, None),
    # exit_reason="adult_mode")`. We exercise the same hook directly
    # so the test is unit-scoped (no full evaluate() needed).
    asyncio.run(e._exit_enforcing(reason="adult_mode"))

    # Counter MUST survive. Pre-fix asserted == 0; post-fix == 1.
    assert e._reactivation_count == 1, (
        f"Expected count=1 after adult-mode exit (preserved), "
        f"got {e._reactivation_count} — F-B regression."
    )
    # Edge-flag also preserved so the next watch_reactivation doesn't
    # phantom-fire (kid's still watching from the parent's session
    # if no inactive transition happened).
    assert e._was_apple_tv_active_last_tick is True

    # ----- Step 3: adult mode ends, budget still exhausted → ENFORCING -----
    # state.py at v0.16.2 forces a WARNING tick first then GRACE then
    # ENFORCING; for the unit test we fast-forward and assert that
    # whatever path returns the kid to ENFORCING leaves the counter
    # alone.
    e._state = STATE_ENFORCING

    # Realistic scenario for the edge: kid stops watching during the
    # adult-mode interlude (movie ends, ATV goes to standby, OR pyatv
    # turn_off finally lands when ENFORCING resumes). Set the flag
    # back to False so the next tick has something to detect.
    e._was_apple_tv_active_last_tick = False

    # ----- Step 4: kid restarts ATV → fresh inactive→active edge -----
    _set_apple_tv_state(e, "playing")
    e._hass.services.async_call = AsyncMock()

    asyncio.run(e.watch_reactivation())

    # Counter increments from 1 → 2 (stern threshold).
    assert e._reactivation_count == 2, (
        f"Expected count=2 (1 preserved across adult mode + 1 new "
        f"edge), got {e._reactivation_count}."
    )
    # Stern voice fires (count >= 2 path), not the friendly one.
    assert _VOICE_SPEAK.call_count == 1
    # Parent push fires (count >= 2 triggers it).
    e._hass.services.async_call.assert_called_once()


def test_non_adult_mode_exit_still_resets_counter():
    """Sanity check the inverse: a regular exit (extension granted,
    midnight reset, etc.) still resets the counter so the next cycle
    starts at 0. Without this, the F-B fix would accidentally make the
    counter persist across legitimate end-of-cycle exits."""
    p = _make_profile()
    e = _make_enforcer(p)
    e._reactivation_count = 3

    # No reason supplied → treated as a real cycle-over exit.
    asyncio.run(e._exit_enforcing())

    assert e._reactivation_count == 0
    assert e._was_apple_tv_active_last_tick is False


# ---------- v0.17.0 F-C: monitor_only records audit but no voice ----------


def test_reactivation_in_monitor_only_increments_count_and_records_audit_but_no_voice():
    """v0.17.0 F-C (Opus BA audit P1).

    Pre-v0.17.0 `_watch_reactivation_locked` bailed on `decision.kind !=
    "ACT"` — meaning monitor_only silenced not just the voice but also
    the counter increment AND the `reactivation` audit row. A parent
    calibrating thresholds in monitor mode therefore couldn't see "the
    kid would have defeated enforcement N times" — exactly the signal
    they need to decide whether to flip to enforced.

    Post-v0.17.0: under OBSERVE we still detect the edge, increment the
    counter, and emit the `reactivation` audit row for the dashboard.
    Voice + parent push remain ACT-only so the kid hears nothing during
    calibration."""
    p = _make_profile()
    p.mode = "monitor_only"
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._was_apple_tv_active_last_tick = False
    _set_apple_tv_state(e, "playing")

    asyncio.run(e.watch_reactivation())

    # Edge WAS detected and counter WAS incremented.
    assert e._reactivation_count == 1
    # But voice did NOT fire (monitor mode is OBSERVE).
    assert _VOICE_SPEAK.call_count == 0
    # And the `reactivation` audit row WAS recorded (dashboard signal).
    audit_calls = [
        c for c in _get_audit_record().call_args_list
        if c.kwargs.get("action") == "reactivation"
    ]
    assert len(audit_calls) == 1
    assert audit_calls[0].kwargs.get("detail") == "#1"


def test_reactivation_count_2_in_monitor_only_does_not_push_parent():
    """Monitor_only at count=2 — stern voice STILL skipped, parent push
    STILL skipped. The audit row records that the kid would have
    triggered the stern interventions, but nothing audible / mobile
    fires under calibration."""
    p = _make_profile()
    p.mode = "monitor_only"
    p.notify_parent_target = "mobile_app_marc"
    e = _make_enforcer(p)
    e._state = STATE_ENFORCING
    e._reactivation_count = 1  # already had #1
    e._was_apple_tv_active_last_tick = False
    _set_apple_tv_state(e, "playing")
    e._hass.services.async_call = AsyncMock()

    asyncio.run(e.watch_reactivation())

    assert e._reactivation_count == 2
    # Stern voice: silent.
    assert _VOICE_SPEAK.call_count == 0
    # Parent push: silent.
    e._hass.services.async_call.assert_not_called()
    # Audit row: present.
    audit_calls = [
        c for c in _get_audit_record().call_args_list
        if c.kwargs.get("action") == "reactivation"
    ]
    assert len(audit_calls) == 1
    assert audit_calls[0].kwargs.get("detail") == "#2"
