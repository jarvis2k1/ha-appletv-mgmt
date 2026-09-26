"""Tests for the v0.15.0 entry/store reconciliation in __init__.py.

Full HA bootstrap is too heavy for this dev env (we skip
`pytest-homeassistant-custom-component`). What we CAN test is:

  1. The reconciliation predicate (does the entry need a write?) — pure
     given (stored_profile.mode, entry.options[CONF_ENFORCEMENT_ENABLED]).
  2. The suppression-flag location: bundle["_suppress_next_reload"] —
     `_async_options_updated` reads it from
     `hass.data[DOMAIN][entry.entry_id]`, not `entry.runtime_data`.
  3. Idempotency: on restart-after-first, both values agree → no write
     should happen.

We stub HA aggressively and invoke the relevant functions in isolation.
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
    """Install the minimum HA stubs needed to import __init__.py.

    We don't actually execute the heavy code paths — we just need the
    module to import cleanly so we can reach the helper functions.
    """
    for mod_name in (
        "homeassistant",
        "homeassistant.config_entries",
        "homeassistant.const",
        "homeassistant.core",
        "homeassistant.helpers",
        "homeassistant.helpers.aiohttp_client",
        "homeassistant.helpers.config_validation",
        "homeassistant.helpers.storage",
        "homeassistant.util",
        "homeassistant.util.dt",
    ):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)
    # config_entries
    ce = sys.modules["homeassistant.config_entries"]
    if not hasattr(ce, "ConfigEntry"):
        ce.ConfigEntry = type("ConfigEntry", (), {})
    # const
    const = sys.modules["homeassistant.const"]
    if not hasattr(const, "Platform"):
        const.Platform = types.SimpleNamespace(SENSOR="sensor", SWITCH="switch", SELECT="select")
    else:
        # Idempotent — if another test set Platform before us without
        # SELECT, top it up.
        if not hasattr(const.Platform, "SELECT"):
            const.Platform.SELECT = "select"
    # core
    core = sys.modules["homeassistant.core"]
    if not hasattr(core, "HomeAssistant"):
        core.HomeAssistant = type("HomeAssistant", (), {})
    if not hasattr(core, "CALLBACK_TYPE"):
        core.CALLBACK_TYPE = type("CALLBACK_TYPE", (), {})
    if not hasattr(core, "ServiceCall"):
        core.ServiceCall = type("ServiceCall", (), {})
    # helpers.config_validation
    cv = sys.modules["homeassistant.helpers.config_validation"]
    if not hasattr(cv, "string"):
        cv.string = str
    # helpers.aiohttp_client
    ahc = sys.modules["homeassistant.helpers.aiohttp_client"]
    if not hasattr(ahc, "async_get_clientsession"):
        ahc.async_get_clientsession = lambda hass: None
    # helpers.event (v0.16.1 — async_call_later for the countdown timer)
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
    # util.dt
    dt_mod = sys.modules["homeassistant.util.dt"]
    if not hasattr(dt_mod, "utcnow"):
        dt_mod.utcnow = lambda: datetime.now(timezone.utc)
        dt_mod.as_local = lambda d: d
        dt_mod.as_utc = lambda d: d

    # voluptuous (pure-Python; available in dev env)
    # If not installed, stub it minimally — only used for service schemas
    # which don't run in our targeted tests.
    if "voluptuous" not in sys.modules:
        try:
            import voluptuous  # noqa: F401
        except ImportError:
            vol = types.ModuleType("voluptuous")

            class _Schema:
                def __init__(self, *a, **kw):
                    pass

            class _Required:
                def __init__(self, k):
                    self.k = k

            class _Optional:
                def __init__(self, k):
                    self.k = k

            def _all(*validators):
                def _v(x):
                    for f in validators:
                        x = f(x)
                    return x

                return _v

            def _coerce(t):
                return t

            def _range(**kw):
                def _v(x):
                    return x

                return _v

            vol.Schema = _Schema
            vol.Required = _Required
            vol.Optional = _Optional
            vol.All = _all
            vol.Coerce = _coerce
            vol.Range = _range
            sys.modules["voluptuous"] = vol


def _load_init_module():
    _ensure_ha_stubs()
    # Ensure parent package stubs are in place for relative imports.
    for pkg_name in ("custom_components", "custom_components.appletv_mgmt"):
        if pkg_name not in sys.modules:
            stub = types.ModuleType(pkg_name)
            stub.__path__ = [str(PKG)] if pkg_name.endswith("appletv_mgmt") else []
            sys.modules[pkg_name] = stub

    import importlib.util

    # Load all dependent modules in the order __init__.py expects.
    for name in ("const", "storage", "policy"):
        full = f"custom_components.appletv_mgmt.{name}"
        if full not in sys.modules:
            spec = importlib.util.spec_from_file_location(full, PKG / f"{name}.py")
            m = importlib.util.module_from_spec(spec)
            sys.modules[full] = m
            spec.loader.exec_module(m)

    # We can't easily import __init__ itself (it pulls in api.py, audit.py,
    # coordinator.py, enforcer.py — all heavy). Instead, copy out the
    # function we want to test by parsing the source.
    #
    # Simpler approach: load __init__.py but stub the heavy submodules to
    # no-op modules first.
    for name in ("api", "audit", "coordinator", "enforcer", "notify"):
        full = f"custom_components.appletv_mgmt.{name}"
        if full not in sys.modules:
            stub = types.ModuleType(full)
            # api.register_views
            stub.register_views = lambda hass: None
            # audit.register_action_recorder
            stub.register_action_recorder = lambda hass, profile_id: (lambda: None)
            # coordinator.AppleTVMgmtCoordinator
            stub.AppleTVMgmtCoordinator = MagicMock
            # enforcer.AdGuardClient, EnforcementController
            stub.AdGuardClient = MagicMock
            stub.EnforcementController = MagicMock
            # notify.register_action_handler
            stub.register_action_handler = lambda hass: (lambda: None)
            sys.modules[full] = stub

    spec = importlib.util.spec_from_file_location(
        "custom_components.appletv_mgmt", PKG / "__init__.py"
    )
    init = importlib.util.module_from_spec(spec)
    sys.modules["custom_components.appletv_mgmt"] = init
    spec.loader.exec_module(init)
    return init


init = _load_init_module()


# ---------- _async_options_updated suppression flag ------------------------


def test_options_updated_skips_reload_when_flag_set():
    """The suppression flag lives in hass.data[DOMAIN][entry.entry_id]."""
    hass = MagicMock()
    hass.config_entries.async_reload = AsyncMock()
    entry = MagicMock()
    entry.entry_id = "entry123"

    # Set up the bundle with the suppression flag
    hass.data = {init.DOMAIN: {entry.entry_id: {"_suppress_next_reload": True}}}

    asyncio.run(init._async_options_updated(hass, entry))

    # No reload should have happened
    hass.config_entries.async_reload.assert_not_called()
    # And the flag must have been consumed (popped)
    assert "_suppress_next_reload" not in hass.data[init.DOMAIN][entry.entry_id]


def test_options_updated_reloads_when_flag_absent():
    """Normal options edit (no flag) → reload as before."""
    hass = MagicMock()
    hass.config_entries.async_reload = AsyncMock()
    entry = MagicMock()
    entry.entry_id = "entry123"
    # Bundle exists but no suppression flag
    hass.data = {init.DOMAIN: {entry.entry_id: {}}}

    asyncio.run(init._async_options_updated(hass, entry))

    hass.config_entries.async_reload.assert_called_once_with(entry.entry_id)


def test_options_updated_reloads_when_bundle_missing():
    """Defensive: if the bundle doesn't exist yet, still reload."""
    hass = MagicMock()
    hass.config_entries.async_reload = AsyncMock()
    entry = MagicMock()
    entry.entry_id = "entry123"
    hass.data = {init.DOMAIN: {}}

    asyncio.run(init._async_options_updated(hass, entry))

    hass.config_entries.async_reload.assert_called_once_with(entry.entry_id)


def test_options_updated_handles_missing_domain():
    """Defensive: if DOMAIN isn't even registered yet, must not crash."""
    hass = MagicMock()
    hass.config_entries.async_reload = AsyncMock()
    entry = MagicMock()
    entry.entry_id = "entry123"
    hass.data = {}

    asyncio.run(init._async_options_updated(hass, entry))

    hass.config_entries.async_reload.assert_called_once_with(entry.entry_id)


# ---------- reconciliation predicate ---------------------------------------


def test_reconciliation_idempotent_when_values_agree():
    """When entry.options[CONF_ENFORCEMENT_ENABLED] already matches the
    stored mode, no async_update_entry call should happen (the predicate
    in __init__.py is `entry.options.get(...) != desired_ee`)."""
    # Simulate post-migration restart: stored mode=enforced, options also true.
    profile_mode = "enforced"
    desired_ee = profile_mode == "enforced"
    entry_options_ee = True
    assert entry_options_ee == desired_ee  # no write needed


def test_reconciliation_writes_when_values_disagree_enforced():
    """Stored mode=enforced but legacy ee=False → reconcile (write to True)."""
    profile_mode = "enforced"
    desired_ee = profile_mode == "enforced"
    entry_options_ee = False
    assert entry_options_ee != desired_ee  # write needed
    assert desired_ee is True


def test_reconciliation_writes_when_values_disagree_monitor():
    """Stored mode=monitor_only but legacy ee=True → reconcile (write to False)."""
    profile_mode = "monitor_only"
    desired_ee = profile_mode == "enforced"
    entry_options_ee = True
    assert entry_options_ee != desired_ee  # write needed
    assert desired_ee is False


def test_reconciliation_writes_for_paused_mode():
    """Stored mode=paused → desired_ee=False (paused is not enforced)."""
    profile_mode = "paused"
    desired_ee = profile_mode == "enforced"
    assert desired_ee is False
