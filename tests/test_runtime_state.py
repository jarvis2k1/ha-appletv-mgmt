"""Tests for v0.17.0 F-E — enforce-cycle state persistence across HA restart.

Bug being prevented (Opus BA audit 2026-05-30 P1):

    Pre-v0.17.0: HA restart erased EnforcementController state
    (_state=OK, _grace_started_at=None, _reactivation_count=0, etc.).
    A kid past their daily budget could earn ~75 s of free TV per
    restart because the first post-restart evaluate() saw used >
    budget with current_state=OK and walked a fresh
    OK → WARN → GRACE → ENFORCING cycle from scratch (warn voice
    replays, full 45 s grace window). Worst case for the owner's setup
    (v0.15.5 default `enable_adguard_block=False`) because that
    disabled the only other state-recovery path (`seed_from_adguard`).

    Reactivation counter loss compounded the issue: a kid at #2 stern
    voice + parent push could restart HA to get back to #1 friendly
    voice with no push.

Fix: persist (state, grace_started_at, enforce_reason,
reactivation_count, was_apple_tv_active_last_tick,
last_enforcement_failed) in storage on every transition; restore on
controller startup via `seed_from_runtime_state` (runs BEFORE
seed_from_adguard, takes precedence). Sanity check: if restored
grace_started_at is ≥ 2× grace_seconds old, drop to ENFORCING.
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
            stub.speak = AsyncMock(return_value={"status": "spoken", "message": "x"})
        elif name == "media_attribution":
            stub.INACTIVE_MEDIA_STATES = {"off", "standby", "unavailable", "idle"}
            stub.UNKNOWN_APP_BUNDLE_ID = "unknown"
        sys.modules[full] = stub

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
const_mod = sys.modules["custom_components.appletv_mgmt.const"]
STATE_OK = const_mod.STATE_OK
STATE_WARNING = const_mod.STATE_WARNING
STATE_GRACE = const_mod.STATE_GRACE
STATE_ENFORCING = const_mod.STATE_ENFORCING


# ============================================================================
# RuntimeState dataclass round-trip
# ============================================================================


def test_runtime_state_to_dict_from_dict_round_trips():
    """All fields survive serialize → deserialize. Critical for survival
    across HA restart since the storage backend round-trips through JSON."""
    started = datetime(2026, 5, 30, 18, 0, 0, tzinfo=timezone.utc)
    updated = datetime(2026, 5, 30, 18, 30, 0, tzinfo=timezone.utc)
    rs = storage_mod.RuntimeState(
        state="enforcing",
        grace_started_at=started,
        enforce_reason="group:movies",
        reactivation_count=2,
        was_apple_tv_active_last_tick=True,
        last_enforcement_failed=False,
        updated_at=updated,
    )
    d = rs.to_dict()
    rs2 = storage_mod.RuntimeState.from_dict(d)
    assert rs2.state == "enforcing"
    assert rs2.grace_started_at == started
    assert rs2.enforce_reason == "group:movies"
    assert rs2.reactivation_count == 2
    assert rs2.was_apple_tv_active_last_tick is True
    assert rs2.last_enforcement_failed is False
    assert rs2.updated_at == updated


def test_runtime_state_from_dict_with_missing_keys_uses_defaults():
    """Forward-compat with older persisted dicts that pre-date new fields."""
    rs = storage_mod.RuntimeState.from_dict({})
    assert rs.state == "ok"
    assert rs.grace_started_at is None
    assert rs.enforce_reason is None
    assert rs.reactivation_count == 0
    assert rs.was_apple_tv_active_last_tick is False
    assert rs.last_enforcement_failed is False
    assert rs.updated_at is None


def test_runtime_state_from_dict_with_bad_timestamps_drops_them():
    """Defense-in-depth: corrupted JSON shouldn't crash the restore path."""
    rs = storage_mod.RuntimeState.from_dict({
        "state": "warning",
        "grace_started_at": "not-a-date",
        "updated_at": None,
    })
    assert rs.state == "warning"
    assert rs.grace_started_at is None
    assert rs.updated_at is None


# ============================================================================
# seed_from_runtime_state: restore + sanity check
# ============================================================================


def _make_profile(grace_seconds=60):
    return storage_mod.Profile(
        id="p1",
        display_name="Living Room",
        apple_tv_entity_id="media_player.heimkinoaaa",
        adguard_client_name="AppleTV",
        daily_budget_min=60,
        grace_seconds=grace_seconds,
        warn_thresholds_min=[5],
        idle_grace_minutes=5,
    )


def _make_enforcer(profile, *, persisted: storage_mod.RuntimeState | None = None):
    hass = MagicMock()
    adguard = MagicMock()
    adguard.set_blocked = AsyncMock(return_value=None)
    adguard.get_client = AsyncMock(return_value={"blocked_services": [], "use_global_blocked_services": True})
    store = MagicMock()
    store.is_adult_mode_active = MagicMock(return_value=False)
    store.adult_mode_until = MagicMock(return_value=None)
    # Wire RuntimeState accessors
    store.get_runtime_state = MagicMock(return_value=persisted)
    store.set_runtime_state = MagicMock()
    return enforcer_mod.EnforcementController(hass, adguard, profile, store=store)


def test_seed_from_runtime_state_restores_grace_window():
    """Persisted GRACE state with a recent grace_started_at restores
    the exact state — kid resumes exactly where the cycle left off,
    no fresh WARN voice replay, no fresh 45 s grace window.
    """
    profile = _make_profile(grace_seconds=60)
    # Persisted state: kid is mid-GRACE, started 10 s ago.
    now = datetime.now(timezone.utc)
    started = now - timedelta(seconds=10)
    persisted = storage_mod.RuntimeState(
        state="grace",
        grace_started_at=started,
        enforce_reason="group:movies",
        reactivation_count=0,
        was_apple_tv_active_last_tick=False,
        last_enforcement_failed=False,
        updated_at=now - timedelta(seconds=5),
    )
    e = _make_enforcer(profile, persisted=persisted)

    e.seed_from_runtime_state()

    assert e._state == STATE_GRACE
    assert e._grace_started_at == started
    assert e._enforce_reason == "group:movies"


def test_seed_from_runtime_state_preserves_reactivation_count():
    """The reactivation counter survives restart so a kid at #2 stern
    voice can't reset back to #1 friendly by restarting HA."""
    profile = _make_profile()
    persisted = storage_mod.RuntimeState(
        state="enforcing",
        grace_started_at=None,
        enforce_reason="daily_limit",
        reactivation_count=2,                   # mid-attempt
        was_apple_tv_active_last_tick=True,
        last_enforcement_failed=False,
        updated_at=datetime.now(timezone.utc),
    )
    e = _make_enforcer(profile, persisted=persisted)

    e.seed_from_runtime_state()

    assert e._reactivation_count == 2
    assert e._was_apple_tv_active_last_tick is True


def test_seed_from_runtime_state_falls_through_to_enforcing_on_stale_grace():
    """Adversarial-required sanity check: if restored grace_started_at
    is more than 2× grace_seconds old (HA was down for a long time),
    treating it as live would compute a negative grace delta. Drop to
    ENFORCING instead.
    """
    profile = _make_profile(grace_seconds=60)
    # GRACE started 200s ago — way past the 2*60s = 120s stale threshold.
    now = datetime.now(timezone.utc)
    started = now - timedelta(seconds=200)
    persisted = storage_mod.RuntimeState(
        state="grace",
        grace_started_at=started,
        enforce_reason="group:movies",
        reactivation_count=1,
        was_apple_tv_active_last_tick=True,
        last_enforcement_failed=False,
        updated_at=now - timedelta(seconds=200),
    )
    e = _make_enforcer(profile, persisted=persisted)

    e.seed_from_runtime_state()

    # State dropped to ENFORCING (would have computed a negative grace
    # delta otherwise).
    assert e._state == STATE_ENFORCING
    # Other fields preserved (counter, reason).
    assert e._enforce_reason == "group:movies"
    assert e._reactivation_count == 1


def test_seed_from_runtime_state_no_persisted_state_is_a_noop():
    """Fresh boot (no prior persistence) → controller keeps constructor
    defaults. Equivalent to pre-v0.17.0 behavior."""
    profile = _make_profile()
    e = _make_enforcer(profile, persisted=None)

    e.seed_from_runtime_state()

    # All fields are still at constructor defaults.
    assert e._state == STATE_OK
    assert e._grace_started_at is None
    assert e._enforce_reason is None
    assert e._reactivation_count == 0
    assert e._was_apple_tv_active_last_tick is False


def test_seed_from_runtime_state_grace_just_under_stale_threshold_survives():
    """Boundary: grace_started_at is 1.99× grace_seconds old — still
    technically resumable (the state machine's compute_next_state will
    fire ENFORCING on the next tick anyway since now ≥ grace_started_at
    + grace_seconds, but we don't pre-empt the live evaluation)."""
    profile = _make_profile(grace_seconds=60)
    now = datetime.now(timezone.utc)
    # 100s old < 120s (2*60s) stale threshold → survives.
    started = now - timedelta(seconds=100)
    persisted = storage_mod.RuntimeState(
        state="grace",
        grace_started_at=started,
        enforce_reason="daily_limit",
        reactivation_count=0,
        was_apple_tv_active_last_tick=False,
        last_enforcement_failed=False,
        updated_at=now,
    )
    e = _make_enforcer(profile, persisted=persisted)

    e.seed_from_runtime_state()

    # GRACE preserved (the next live tick will transition to ENFORCING).
    assert e._state == STATE_GRACE


# ============================================================================
# Persistence: state changes write to the store
# ============================================================================


def test_persist_runtime_state_writes_snapshot():
    """`_persist_runtime_state()` calls store.set_runtime_state with
    the current values."""
    profile = _make_profile()
    e = _make_enforcer(profile)
    e._state = STATE_ENFORCING
    e._enforce_reason = "group:movies"
    e._reactivation_count = 3
    e._was_apple_tv_active_last_tick = True

    e._persist_runtime_state()

    e._store.set_runtime_state.assert_called_once()
    args, kwargs = e._store.set_runtime_state.call_args
    profile_id_arg, runtime_arg = args
    assert profile_id_arg == "p1"
    assert runtime_arg.state == STATE_ENFORCING
    assert runtime_arg.enforce_reason == "group:movies"
    assert runtime_arg.reactivation_count == 3
    assert runtime_arg.was_apple_tv_active_last_tick is True
    assert runtime_arg.updated_at is not None  # timestamp stamped


def test_persist_with_no_store_is_a_noop():
    """Tests pass enforcer with store=None; persist must not crash."""
    profile = _make_profile()
    hass = MagicMock()
    adguard = MagicMock()
    e = enforcer_mod.EnforcementController(hass, adguard, profile, store=None)

    # Should not raise.
    e._persist_runtime_state()
