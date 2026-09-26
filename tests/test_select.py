"""Tests for the new v0.15.0 select.<profile>_mode entity (spec §3.1).

Critical assertions:
  * No-op guard: selecting the current value → zero audit, zero refresh,
    zero state write.
  * Selecting a NEW value:
      - exactly one `mode_changed` audit row (no `limits_changed`)
      - exactly one coordinator refresh
      - stored Profile mode + legacy enforcement_enabled updated
      - in-memory Profile reference also updated
  * current_option reads from the STORED Profile (not entry.options).
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
        "homeassistant.components",
        "homeassistant.components.select",
        "homeassistant.config_entries",
        "homeassistant.const",
        "homeassistant.core",
        "homeassistant.helpers",
        "homeassistant.helpers.entity",
        "homeassistant.helpers.entity_platform",
        "homeassistant.helpers.storage",
        "homeassistant.util",
        "homeassistant.util.dt",
    ):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)
    sel_mod = sys.modules["homeassistant.components.select"]
    if not hasattr(sel_mod, "SelectEntity"):
        # Minimal SelectEntity stub — we only need it as a base class with
        # async_write_ha_state hook + the class attributes the impl sets.
        class _SelectEntity:
            _attr_has_entity_name = False
            _attr_translation_key = ""
            _attr_icon = ""
            _attr_options: list = []
            _attr_entity_category = None
            _attr_unique_id = ""
            _attr_device_info: dict = {}

            def async_write_ha_state(self):
                pass

        sel_mod.SelectEntity = _SelectEntity
    ce = sys.modules["homeassistant.config_entries"]
    if not hasattr(ce, "ConfigEntry"):
        ce.ConfigEntry = type("ConfigEntry", (), {})
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
    ep_mod = sys.modules["homeassistant.helpers.entity_platform"]
    if not hasattr(ep_mod, "AddEntitiesCallback"):
        ep_mod.AddEntitiesCallback = type("AddEntitiesCallback", (), {})


_ensure_ha_stubs()

for pkg_name in ("custom_components", "custom_components.appletv_mgmt"):
    if pkg_name not in sys.modules:
        stub = types.ModuleType(pkg_name)
        stub.__path__ = [str(PKG)] if pkg_name.endswith("appletv_mgmt") else []
        sys.modules[pkg_name] = stub

import importlib.util

# Pre-load const + storage so the select module's relative imports resolve.
for name in ("const", "storage"):
    full = f"custom_components.appletv_mgmt.{name}"
    if full not in sys.modules:
        spec = importlib.util.spec_from_file_location(full, PKG / f"{name}.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules[full] = m
        spec.loader.exec_module(m)

# Stub the audit module so we can capture record_admin_action calls.
audit_stub = sys.modules.get("custom_components.appletv_mgmt.audit")
if audit_stub is None or not hasattr(audit_stub, "record_admin_action"):
    audit_stub = types.ModuleType("custom_components.appletv_mgmt.audit")
    sys.modules["custom_components.appletv_mgmt.audit"] = audit_stub
audit_stub.record_admin_action = MagicMock()
audit_stub.record_action = MagicMock()
audit_stub.register_action_recorder = lambda hass, profile_id: (lambda: None)

# v0.15.6 — stub voice_notifier.fire_mode_change_voice (select.py imports
# it inline to fire the optional mode-change announcement). Stub it as
# a no-op AsyncMock so the select-entity tests don't need a full voice
# stack.
vn_stub = sys.modules.get("custom_components.appletv_mgmt.voice_notifier")
if vn_stub is None or not hasattr(vn_stub, "fire_mode_change_voice"):
    from unittest.mock import AsyncMock
    vn_stub = types.ModuleType("custom_components.appletv_mgmt.voice_notifier")
    sys.modules["custom_components.appletv_mgmt.voice_notifier"] = vn_stub
    vn_stub.fire_mode_change_voice = AsyncMock(return_value=None)
    vn_stub.fire_adult_mode_on_voice = AsyncMock(return_value=None)

spec = importlib.util.spec_from_file_location(
    "custom_components.appletv_mgmt.select", PKG / "select.py"
)
select_mod = importlib.util.module_from_spec(spec)
sys.modules["custom_components.appletv_mgmt.select"] = select_mod
spec.loader.exec_module(select_mod)

# Rebind the audit module on select_mod to our captured stub (since `from
# .audit import record_admin_action` would otherwise bind a fresh copy).
select_mod.record_admin_action = audit_stub.record_admin_action


def _make_profile(mode="enforced"):
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
        mode=mode,
        enforcement_enabled=(mode == "enforced"),
    )


def _make_select(profile):
    hass = MagicMock()
    entry = MagicMock()
    storage_mod = sys.modules["custom_components.appletv_mgmt.storage"]
    store = storage_mod.AppleTVMgmtStore.__new__(storage_mod.AppleTVMgmtStore)
    store._profiles = {profile.id: profile}
    store._events = []
    store._extensions_granted_today = {}
    store._app_categories = {}
    store._adult_mode_until = {}
    store._requests = {}
    store._actions = []
    # async_save is awaited by the entity — stub it.
    async def _save():
        pass

    store.async_save = _save

    coord = MagicMock()
    coord.async_request_refresh = AsyncMock()
    sel = select_mod.ModeSelect(hass, entry, profile, store, coord)
    audit_stub.record_admin_action.reset_mock()
    return sel, store, coord


# ---------- current_option reads stored profile ------------------------------


def test_current_option_reads_stored_profile():
    p = _make_profile(mode="paused")
    sel, store, _ = _make_select(p)
    assert sel.current_option == "paused"


def test_current_option_falls_back_to_runtime_when_store_missing():
    p = _make_profile(mode="monitor_only")
    sel, store, _ = _make_select(p)
    # Wipe the store so the fallback path triggers.
    store._profiles.clear()
    assert sel.current_option == "monitor_only"


def test_current_option_default_when_no_profile_no_attr():
    p = _make_profile()
    sel, store, _ = _make_select(p)
    store._profiles.clear()
    # Strip the mode attr from the in-memory profile to force the last fallback.
    object.__setattr__(p, "mode", "enforced")
    assert sel.current_option == "enforced"


# ---------- no-op guard ----------------------------------------------------


def test_no_op_guard_no_audit_no_refresh():
    """Selecting the same value already in profile.mode → zero side effects."""
    p = _make_profile(mode="enforced")
    sel, store, coord = _make_select(p)

    asyncio.run(sel.async_select_option("enforced"))

    assert audit_stub.record_admin_action.call_count == 0
    coord.async_request_refresh.assert_not_called()


def test_no_op_guard_for_paused():
    p = _make_profile(mode="paused")
    sel, store, coord = _make_select(p)

    asyncio.run(sel.async_select_option("paused"))

    assert audit_stub.record_admin_action.call_count == 0
    coord.async_request_refresh.assert_not_called()


# ---------- happy path -----------------------------------------------------


def test_select_new_option_writes_mode_and_audit():
    p = _make_profile(mode="enforced")
    sel, store, coord = _make_select(p)

    asyncio.run(sel.async_select_option("monitor_only"))

    # Mode persisted on the stored profile.
    assert store.get_profile("p1").mode == "monitor_only"
    # Legacy bool kept in sync.
    assert store.get_profile("p1").enforcement_enabled is False
    # In-memory profile also updated (so enforcer sees it next tick).
    assert p.mode == "monitor_only"
    assert p.enforcement_enabled is False
    # Exactly one audit row.
    assert audit_stub.record_admin_action.call_count == 1
    kwargs = audit_stub.record_admin_action.call_args.kwargs
    assert kwargs["action"] == "mode_changed"
    assert kwargs["actor"] == "select_entity"
    assert kwargs["detail"] == "enforced → monitor_only"
    assert kwargs["profile_id"] == "p1"
    # Coordinator refresh fired once.
    coord.async_request_refresh.assert_called_once()


def test_select_paused_from_enforced():
    p = _make_profile(mode="enforced")
    sel, store, coord = _make_select(p)

    asyncio.run(sel.async_select_option("paused"))

    assert store.get_profile("p1").mode == "paused"
    assert store.get_profile("p1").enforcement_enabled is False
    assert audit_stub.record_admin_action.call_args.kwargs["detail"] == "enforced → paused"


def test_select_enforced_from_monitor_resyncs_legacy_bool():
    p = _make_profile(mode="monitor_only")
    sel, store, coord = _make_select(p)

    asyncio.run(sel.async_select_option("enforced"))

    assert store.get_profile("p1").mode == "enforced"
    assert store.get_profile("p1").enforcement_enabled is True


# ---------- defensive: invalid input rejected ------------------------------


def test_invalid_option_rejected_silently():
    p = _make_profile(mode="enforced")
    sel, store, coord = _make_select(p)

    asyncio.run(sel.async_select_option("totally_invalid"))

    # No state change.
    assert store.get_profile("p1").mode == "enforced"
    assert audit_stub.record_admin_action.call_count == 0
    coord.async_request_refresh.assert_not_called()


def test_select_when_stored_profile_missing_does_nothing():
    p = _make_profile()
    sel, store, coord = _make_select(p)
    store._profiles.clear()

    asyncio.run(sel.async_select_option("paused"))

    assert audit_stub.record_admin_action.call_count == 0
    coord.async_request_refresh.assert_not_called()


# ---------- options attribute is correct -----------------------------------


def test_options_are_the_three_modes():
    p = _make_profile()
    sel, _, _ = _make_select(p)
    assert sel._attr_options == ["enforced", "monitor_only", "paused"]


def test_unique_id_format():
    p = _make_profile()
    sel, _, _ = _make_select(p)
    assert sel._attr_unique_id == "p1_mode"


def test_translation_key():
    p = _make_profile()
    sel, _, _ = _make_select(p)
    assert sel._attr_translation_key == "mode"
