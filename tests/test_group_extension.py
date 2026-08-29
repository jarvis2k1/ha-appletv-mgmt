"""v0.19.1 — extension grants lift the BINDING GROUP budget, not just daily.

Bug being prevented (live-reported 2026-06-19, Living Room profile):

    Friday movies group budget = 30 min, daily budget = 60 min.
    Kid watched Netflix (movies group) for ~30 min → movies group exhausted.
    Parent granted +60, then +30 (today total 90) via the panel.
    Those went into the DAILY pool only (33 of 150 min used — nowhere near
    empty). The movies GROUP cap stayed at 30, so grace_start fired on
    `group:movies` anyway and the kid kept getting nagged until the parent
    gave up and used adult mode.

The fix tags each manual extension to the binding/active group and the
enforcer adds that group's extension to its budget via the new
`group_extensions_seconds` evaluate() parameter.

Tests mirror the stub-bootstrap pattern in test_binding_constraint.py.
"""
from __future__ import annotations

import asyncio
import importlib.util
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

storage_mod = sys.modules["custom_components.appletv_mgmt.storage"]


def _make_profile(*, daily_budget_min=60, warn_thresholds_min=[5], grace_seconds=60):
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


NOW = datetime(2026, 6, 19, 19, 25, 45, tzinfo=timezone.utc)
M = 60  # seconds per minute


# ---------------------------------------------------------------------------
# Enforcer — group extension lifts the binding group budget
# ---------------------------------------------------------------------------


def test_movies_exhausted_no_extension_is_binding_at_zero():
    """Baseline (the bug): movies 30m budget, 30m used, daily 60+90=150
    effective with only 33m used. WITHOUT a group extension, movies is the
    binding constraint at 0 remaining → enforcing. This is what kept nagging
    the kid on 2026-06-19."""
    p = _make_profile(daily_budget_min=60)
    e = _make_enforcer(p)
    asyncio.run(e.evaluate(
        used_seconds=33 * M,                  # daily used (ex-extension)
        now=NOW,
        today_budget_min=150,                 # 60 base + 90 daily extension
        current_group="movies",
        current_group_used_seconds=33 * M,
        current_group_budget_seconds=30 * M,  # movies cap = 30 min
        group_totals_seconds={"movies": 33 * M},
        group_budgets_seconds={"movies": 30 * M},
        # No group_extensions_seconds → the bug reproduces.
    ))
    assert e.enforce_reason == "group:movies"
    assert e.effective_remaining_seconds <= 0


def test_movies_extension_lifts_group_budget_no_longer_binding():
    """The fix: +90 tagged to movies → effective movies budget 30+90=120,
    used 33 → 87 min left. Movies no longer binding; state OK."""
    p = _make_profile(daily_budget_min=60)
    e = _make_enforcer(p)
    asyncio.run(e.evaluate(
        used_seconds=33 * M,
        now=NOW,
        today_budget_min=150,
        current_group="movies",
        current_group_used_seconds=33 * M,
        current_group_budget_seconds=30 * M,
        group_totals_seconds={"movies": 33 * M},
        group_budgets_seconds={"movies": 30 * M},
        group_extensions_seconds={"movies": 90 * M},   # the fix
    ))
    assert e.state == "ok", f"expected OK, got {e.state} ({e.enforce_reason})"
    # Binding is now whichever has less: movies 87m vs daily 117m → movies 87m.
    assert e.effective_remaining_seconds == 87 * M


def test_extension_tagged_to_movies_does_not_lift_gaming_antidefeat():
    """Anti-defeat: a +90 grant tagged to movies must NOT lift the gaming
    budget. Kid switches to a game after the movies extension → gaming's own
    30m cap still binds. (Otherwise 'ask for a movie extension, then game'
    would be free gaming time.)"""
    p = _make_profile(daily_budget_min=60)
    e = _make_enforcer(p)
    asyncio.run(e.evaluate(
        used_seconds=63 * M,
        now=NOW,
        today_budget_min=150,
        current_group="gaming",
        current_group_used_seconds=30 * M,         # gaming fully used
        current_group_budget_seconds=30 * M,
        group_totals_seconds={"movies": 33 * M, "gaming": 30 * M},
        group_budgets_seconds={"movies": 30 * M, "gaming": 30 * M},
        group_extensions_seconds={"movies": 90 * M},   # only movies extended
    ))
    assert e.enforce_reason == "group:gaming"
    assert e.effective_remaining_seconds <= 0


def test_exhaustion_pin_respects_group_extension():
    """The v0.16.5 exhaustion-pin loop must use the extension-adjusted budget.
    Movies used 33m, base cap 30m (would pin), but +90 extension → effective
    120m → NOT exhausted → no pin even when current_group is None (idle)."""
    p = _make_profile(daily_budget_min=60)
    e = _make_enforcer(p)
    asyncio.run(e.evaluate(
        used_seconds=33 * M,
        now=NOW,
        today_budget_min=150,
        current_group=None,                        # kid idle / home screen
        group_totals_seconds={"movies": 33 * M},
        group_budgets_seconds={"movies": 30 * M},
        group_extensions_seconds={"movies": 90 * M},
    ))
    assert e.state == "ok", f"pin fired despite extension: {e.enforce_reason}"


def test_extension_smaller_than_overage_still_binds_but_relieved():
    """A partial extension still helps: movies 30m cap, 33m used, +5 → cap 35,
    2 min left → WARNING (warn_threshold 5), not full enforcing. Confirms the
    extension is additive, not all-or-nothing."""
    p = _make_profile(daily_budget_min=60, warn_thresholds_min=[5])
    e = _make_enforcer(p)
    asyncio.run(e.evaluate(
        used_seconds=33 * M,
        now=NOW,
        today_budget_min=150,
        current_group="movies",
        current_group_used_seconds=33 * M,
        current_group_budget_seconds=30 * M,
        group_totals_seconds={"movies": 33 * M},
        group_budgets_seconds={"movies": 30 * M},
        group_extensions_seconds={"movies": 5 * M},
    ))
    # 35 - 33 = 2 min left → within the 5-min warn window.
    assert e.effective_remaining_seconds == 2 * M
    assert e.state == "warning"


def test_zero_budget_group_stays_unlimited_despite_extension():
    """REGRESSION (adversarial review 2026-06-19): a group with budget 0 is
    'unlimited' by convention. An extension tagged to it must NOT convert it
    to 'limited to the extension amount' — that would turn a +time grant into
    a DENIAL. The active-group auto-tagging can route a grant to an unlimited
    group, so this guard is load-bearing.

    movies budget=0 (unlimited), kid watched 500 min, parent grants +30 to
    movies → movies must STAY unlimited; state OK (daily is the only ceiling)."""
    p = _make_profile(daily_budget_min=600)
    e = _make_enforcer(p)
    asyncio.run(e.evaluate(
        used_seconds=500 * M,
        now=NOW,
        today_budget_min=600,
        current_group="movies",
        current_group_used_seconds=500 * M,
        current_group_budget_seconds=0,            # unlimited
        group_totals_seconds={"movies": 500 * M},
        group_budgets_seconds={"movies": 0},       # unlimited
        group_extensions_seconds={"movies": 30 * M},
    ))
    assert e.state == "ok", (
        f"unlimited movies wrongly capped by extension: {e.state} "
        f"({e.enforce_reason})"
    )
    assert e.enforce_reason != "group:movies"


def test_no_group_extensions_param_is_back_compat():
    """Omitting group_extensions_seconds entirely behaves exactly as before
    (the param defaults to None). Guards the v0.16.x contract."""
    p = _make_profile(daily_budget_min=200, warn_thresholds_min=[5])
    e = _make_enforcer(p)
    asyncio.run(e.evaluate(
        used_seconds=55 * M,
        now=NOW,
        current_group="movies",
        current_group_used_seconds=55 * M,
        current_group_budget_seconds=60 * M,
    ))
    # Same as the pre-existing binding-constraint test: 5 min group remaining.
    assert e.effective_remaining_seconds == 5 * M
    assert e.enforce_reason == "group:movies"


# ---------------------------------------------------------------------------
# Storage — group-tagged extension pool
# ---------------------------------------------------------------------------


def _make_store():
    return storage_mod.AppleTVMgmtStore(MagicMock())


def test_add_extension_with_group_tags_both_pools():
    s = _make_store()
    daily_total = s.add_extension_minutes("p1", 30, group="movies")
    assert daily_total == 30
    assert s.extension_minutes_today("p1") == 30
    assert s.group_extension_minutes_today("p1", "movies") == 30
    assert s.group_extension_minutes_today("p1", "gaming") == 0


def test_add_extension_without_group_is_daily_only():
    s = _make_store()
    s.add_extension_minutes("p1", 30)
    assert s.extension_minutes_today("p1") == 30
    assert s.group_extensions_today("p1") == {}


def test_multiple_group_grants_accumulate_independently():
    s = _make_store()
    s.add_extension_minutes("p1", 60, group="movies")
    s.add_extension_minutes("p1", 30, group="movies")
    s.add_extension_minutes("p1", 15, group="gaming")
    assert s.extension_minutes_today("p1") == 105        # daily pool sums all
    assert s.group_extension_minutes_today("p1", "movies") == 90
    assert s.group_extension_minutes_today("p1", "gaming") == 15


def test_negative_grant_floors_group_pool_at_zero():
    s = _make_store()
    s.add_extension_minutes("p1", 30, group="movies")
    s.add_extension_minutes("p1", -50, group="movies")
    assert s.group_extension_minutes_today("p1", "movies") == 0
    assert s.extension_minutes_today("p1") == 0


def test_midnight_reset_clears_group_pool():
    s = _make_store()
    s.add_extension_minutes("p1", 30, group="movies")
    s.reset_daily_state()
    assert s.extension_minutes_today("p1") == 0
    assert s.group_extensions_today("p1") == {}


def test_reset_daily_state_profile_scoped_does_not_wipe_siblings():
    """v0.20.1 — the store is shared across config entries, so a per-profile
    reset (reset_usage service, per-coordinator midnight) MUST only clear the
    named profile. Resetting kid A must not zero kid B's granted extensions."""
    s = _make_store()
    s.add_extension_minutes("kidA", 60, group="movies")
    s.add_extension_minutes("kidB", 45, group="gaming")

    s.reset_daily_state("kidA")

    assert s.extension_minutes_today("kidA") == 0
    assert s.group_extensions_today("kidA") == {}
    # kidB untouched.
    assert s.extension_minutes_today("kidB") == 45
    assert s.group_extension_minutes_today("kidB", "gaming") == 45


def test_reset_daily_state_no_arg_clears_all_profiles():
    """The no-arg (global) form still clears everyone — kept for a true
    full-reset caller (none in-tree today, but the contract is preserved)."""
    s = _make_store()
    s.add_extension_minutes("kidA", 60)
    s.add_extension_minutes("kidB", 45)
    s.reset_daily_state()
    assert s.extension_minutes_today("kidA") == 0
    assert s.extension_minutes_today("kidB") == 0


def test_group_extensions_survive_save_load_roundtrip():
    """The per-group pool must serialize so a HA restart mid-day doesn't
    silently revert a parent's group-targeted grant."""
    s = _make_store()
    s.add_extension_minutes("p1", 90, group="movies")
    s.add_extension_minutes("p1", 30)  # daily-only too

    captured = {}

    async def _fake_save(payload):
        captured.update(payload)

    s._store.async_save = _fake_save
    asyncio.run(s.async_save())
    assert captured["group_extensions_today"] == {"p1": {"movies": 90}}
    assert captured["extensions_granted_today"] == {"p1": 120}

    # Load into a fresh store via the real async_load deserialize path.
    s2 = _make_store()

    async def _fake_load():
        return captured

    s2._store.async_load = _fake_load
    asyncio.run(s2.async_load())
    assert s2.group_extension_minutes_today("p1", "movies") == 90
    assert s2.extension_minutes_today("p1") == 120
