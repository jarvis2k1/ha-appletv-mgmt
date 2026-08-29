"""Tests for v0.16.3 — binding-constraint awareness in enforcer + audit.

Bug being prevented (live-reported 2026-05-29, Living Room profile):

    Group "movies" budget = 60 min, daily budget = 200 min.
    Kid watched Netflix for 55 min → group remaining = 5 min, daily = 145 min.
    Warn voice fired and announced: "Achtung! Noch 145 Minuten Bildschirmzeit
    übrig" — when actually only 5 minutes of movies were left.

The fix introduces:
  - `enforcer.effective_remaining_seconds` (binding constraint's remaining)
  - `enforcer.enforce_reason` set from WARN onwards (was only at exhaustion)
  - `audit._remaining_min` reads effective_remaining_seconds (falls back to daily)
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
                stub.speak = AsyncMock(return_value={"status": "spoken", "message": "stub"})
            elif name == "media_attribution":
                stub.INACTIVE_MEDIA_STATES = {"off", "standby", "unavailable", "idle"}
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


def _make_profile(*, daily_budget_min=200, warn_thresholds_min=[5], grace_seconds=60):
    storage_mod = sys.modules["custom_components.appletv_mgmt.storage"]
    return storage_mod.Profile(
        id="p1",
        display_name="Living Room",
        apple_tv_entity_id="media_player.living_room_apple_tv",
        adguard_client_name="AppleTV",
        daily_budget_min=daily_budget_min,
        grace_seconds=grace_seconds,
        warn_thresholds_min=warn_thresholds_min,
        idle_grace_minutes=5,
    )


def _make_enforcer(profile):
    hass = MagicMock()
    adguard = MagicMock()
    adguard.set_blocked = AsyncMock(return_value=None)
    store = MagicMock()
    store.is_adult_mode_active = MagicMock(return_value=False)
    store.adult_mode_until = MagicMock(return_value=None)
    return enforcer_mod.EnforcementController(hass, adguard, profile, store=store)


NOW = datetime(2026, 5, 29, 19, 35, 39, tzinfo=timezone.utc)


# ---------- Bug repro: the 145-min announcement ----------


def test_group_in_warn_window_sets_binding_remaining_to_group_not_daily():
    """The exact live scenario: group movies has 5 min left, daily has 145.

    `effective_remaining_seconds` MUST be the group's 5 min (300s), not
    daily's 145 min (8700s). Audit voice substitution reads this — wrong
    value here means the kid hears "Noch 145 Minuten" 5 min before being
    cut off. (Live-reported 2026-05-29.)
    """
    p = _make_profile(daily_budget_min=200, warn_thresholds_min=[5])
    e = _make_enforcer(p)
    asyncio.run(e.evaluate(
        used_seconds=55 * 60,            # 55 min used overall
        now=NOW,
        current_group="movies",
        current_group_used_seconds=55 * 60,
        current_group_budget_seconds=60 * 60,   # 60 min budget
    ))
    # Group remaining = 5 min = 300s; daily remaining = 145 min = 8700s.
    assert e.effective_remaining_seconds == 300, (
        f"Expected 300s (group remaining), got {e.effective_remaining_seconds}s "
        f"— the 145-min announcement bug is back."
    )
    # State machine should have transitioned to WARNING (5 min ≤ warn_threshold 5).
    assert e.state == "warning"
    # Reason should now be set at the WARN boundary (was None before v0.16.3).
    assert e.enforce_reason == "group:movies"


def test_group_in_warn_window_sets_enforce_reason_correctly():
    """`enforce_reason` must surface the binding constraint from WARN onwards.

    Pre-v0.16.3 the reason was only set when group_remaining ≤ 0, so the
    `warn` audit row had reason=None and downstream consumers couldn't
    tell which budget was about to expire.
    """
    p = _make_profile(daily_budget_min=200, warn_thresholds_min=[5])
    e = _make_enforcer(p)
    asyncio.run(e.evaluate(
        used_seconds=55 * 60,
        now=NOW,
        current_group="movies",
        current_group_used_seconds=58 * 60,    # group has 2 min left
        current_group_budget_seconds=60 * 60,
    ))
    assert e.enforce_reason == "group:movies"
    assert e.effective_remaining_seconds == 120


# ---------- Daily-only binding ----------


def test_daily_in_warn_window_sets_enforce_reason_daily_limit():
    """When no group is active, the daily budget is the binding constraint."""
    p = _make_profile(daily_budget_min=60, warn_thresholds_min=[5])
    e = _make_enforcer(p)
    asyncio.run(e.evaluate(
        used_seconds=56 * 60,         # 4 min remaining on daily, no group
        now=NOW,
    ))
    assert e.state == "warning"
    assert e.enforce_reason == "daily_limit"
    assert e.effective_remaining_seconds == 4 * 60


def test_ok_state_keeps_enforce_reason_none():
    """OK ticks must not carry a reason — downstream consumers (audit,
    UI) treat None as 'nothing approaching'.
    """
    p = _make_profile(daily_budget_min=60, warn_thresholds_min=[5])
    e = _make_enforcer(p)
    asyncio.run(e.evaluate(
        used_seconds=10 * 60,         # 50 min remaining — well above warn
        now=NOW,
    ))
    assert e.state == "ok"
    assert e.enforce_reason is None
    assert e.effective_remaining_seconds == 50 * 60


def test_group_active_but_ok_no_reason_surfaced():
    """Group is active and binding, but well above warn → reason stays None."""
    p = _make_profile(daily_budget_min=200, warn_thresholds_min=[5])
    e = _make_enforcer(p)
    asyncio.run(e.evaluate(
        used_seconds=30 * 60,
        now=NOW,
        current_group="movies",
        current_group_used_seconds=30 * 60,
        current_group_budget_seconds=60 * 60,   # group has 30 min left
    ))
    assert e.state == "ok"
    assert e.enforce_reason is None
    # Effective remaining is still surfaced — UI may consume it for
    # progress bars even in OK.
    assert e.effective_remaining_seconds == 30 * 60


# ---------- Quiet windows ----------


def test_quiet_window_sets_effective_remaining_zero():
    """Quiet window forces enforcement immediately — effective remaining
    is 0 (the binding constraint is the wall-clock window itself).
    """
    # Build a profile with a quiet window covering NOW.
    storage_mod = sys.modules["custom_components.appletv_mgmt.storage"]
    profile = storage_mod.Profile(
        id="p1",
        display_name="Living Room",
        apple_tv_entity_id="media_player.living_room_apple_tv",
        adguard_client_name="AppleTV",
        daily_budget_min=60,
        grace_seconds=60,
        warn_thresholds_min=[5],
        idle_grace_minutes=5,
        quiet_windows="00:00-23:59:sleep",
    )
    e = _make_enforcer(profile)
    asyncio.run(e.evaluate(used_seconds=5 * 60, now=NOW))   # plenty of budget left
    assert e.effective_remaining_seconds == 0
    assert e.enforce_reason is not None
    assert e.enforce_reason.startswith("quiet:")


# ---------- Adult mode ----------


def test_adult_mode_sets_effective_remaining_to_daily_no_binding():
    """Adult mode bypasses all enforcement → effective remaining tracks
    raw daily (no constraint binds) and reason stays None.
    """
    p = _make_profile(daily_budget_min=60, warn_thresholds_min=[5])
    e = _make_enforcer(p)
    asyncio.run(e.evaluate(
        used_seconds=55 * 60,    # would normally be in warn
        now=NOW,
        adult_mode_active=True,
    ))
    assert e.state == "ok"
    assert e.enforce_reason is None
    assert e.effective_remaining_seconds == 5 * 60   # raw daily, no scaling
