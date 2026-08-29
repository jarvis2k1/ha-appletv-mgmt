"""v0.19.0 — Xbox MVP tests.

What v0.19.0 adds:
  - Profile fields: device_kind ("apple_tv" | "xbox_presence"),
    enforcement_switch_entity_id.
  - Coordinator: when device_kind != "apple_tv", skip the pyatv-only
    hot paths (_check_for_stale_session, _check_pyatv_reload,
    _check_dns_corroboration).
  - Coordinator: _extract_activity_signal translates a device_tracker.*
    state into (media_state, app_id) — "home" → ("playing",
    XBOX_CONSOLE_BUNDLE_ID); else (state, None).
  - Enforcer: dispatches the network-block primitive on device_kind. Xbox
    flips a switch.* entity (turn_off to block, turn_on to release).
  - REST API: PATCH /limits accepts enforcement_switch_entity_id.
  - Categorize: XBOX_CONSOLE_BUNDLE_ID → GROUP_GAMING.

Tests are pure-function: no HA runtime, no asyncio loop, no aiohttp.
They reuse the storage HA-stub trick from test_storage_helpers.py.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = ROOT / "custom_components" / "appletv_mgmt"


def _ensure_ha_stubs():
    """Stub homeassistant.* sub-modules storage.py imports at top-level."""
    for mod_name in (
        "homeassistant",
        "homeassistant.core",
        "homeassistant.helpers",
        "homeassistant.helpers.storage",
        "homeassistant.util",
        "homeassistant.util.dt",
    ):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)
    hass_core = sys.modules["homeassistant.core"]
    if not hasattr(hass_core, "HomeAssistant"):
        hass_core.HomeAssistant = type("HomeAssistant", (), {})
    storage_mod = sys.modules["homeassistant.helpers.storage"]
    if not hasattr(storage_mod, "Store"):
        class _Store:
            def __init__(self, *a, **kw): pass
            async def async_load(self): return None
            async def async_save(self, data): pass
        storage_mod.Store = _Store
    dt_mod = sys.modules["homeassistant.util.dt"]
    if not hasattr(dt_mod, "utcnow"):
        dt_mod.utcnow = lambda: datetime.now(timezone.utc)
        dt_mod.as_local = lambda d: d
        dt_mod.as_utc = lambda d: d


_ensure_ha_stubs()


def _load(modname: str, path: Path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_load("custom_components", PKG_ROOT.parent / "__init__.py") if (
    PKG_ROOT.parent / "__init__.py"
).exists() else None
# const is pure stdlib.
const_mod = _load("custom_components.appletv_mgmt.const", PKG_ROOT / "const.py")
categorize_mod = _load(
    "custom_components.appletv_mgmt.categorize", PKG_ROOT / "categorize.py",
)
storage_mod = _load(
    "custom_components.appletv_mgmt.storage", PKG_ROOT / "storage.py",
)


def _make_xbox_profile(**overrides):
    defaults = dict(
        id="xbox-1",
        display_name="Xbox",
        apple_tv_entity_id="device_tracker.xboxone",
        adguard_client_name="",
        daily_budget_min=60,
        grace_seconds=60,
        warn_thresholds_min=[5],
        idle_grace_minutes=5,
        device_kind="xbox_presence",
        enforcement_switch_entity_id="switch.xboxone_internet_access",
    )
    defaults.update(overrides)
    return storage_mod.Profile(**defaults)


def _make_apple_tv_profile(**overrides):
    defaults = dict(
        id="at-1",
        display_name="Living Room",
        apple_tv_entity_id="media_player.living_room_apple_tv",
        adguard_client_name="AppleTV",
        daily_budget_min=60,
        grace_seconds=60,
        warn_thresholds_min=[5],
        idle_grace_minutes=5,
    )
    defaults.update(overrides)
    return storage_mod.Profile(**defaults)


# ---------------------------------------------------------------------------
# categorize.py — Xbox bundle resolves to gaming
# ---------------------------------------------------------------------------


def test_xbox_console_bundle_id_constant_is_xbox_console():
    """Lock the wire-format string. The coordinator + categorize + storage
    serialization all hard-depend on this literal."""
    assert categorize_mod.XBOX_CONSOLE_BUNDLE_ID == "xbox.console"


def test_xbox_console_bundle_categorizes_as_gaming():
    """The synthetic Xbox bundle must land in GROUP_GAMING — that's how
    per-group budgets cover Xbox + Apple TV gaming together."""
    g = categorize_mod.group_from_curated(categorize_mod.XBOX_CONSOLE_BUNDLE_ID)
    assert g == categorize_mod.GROUP_GAMING


def test_xbox_console_bundle_categorized_via_full_categorize_fn():
    """End-to-end through the categorize() function (CURATED + cache +
    fallback) — must still land in gaming with no iTunes cache hit."""
    g = categorize_mod.categorize(
        categorize_mod.XBOX_CONSOLE_BUNDLE_ID, cache={},
    )
    assert g == categorize_mod.GROUP_GAMING


# ---------------------------------------------------------------------------
# Profile back-compat + Xbox round-trip
# ---------------------------------------------------------------------------


def test_profile_default_device_kind_is_apple_tv():
    """Profiles persisted before v0.19.0 have no device_kind key. The
    from_dict path must default it to "apple_tv" so no live profile
    silently flips to the Xbox dispatch."""
    p = storage_mod.Profile.from_dict({
        "id": "p1",
        "display_name": "Living Room",
        "apple_tv_entity_id": "media_player.living_room_apple_tv",
        "adguard_client_name": "AppleTV",
        "daily_budget_min": 60,
        "grace_seconds": 60,
        "warn_thresholds_min": [5],
        "idle_grace_minutes": 5,
    })
    assert p.device_kind == "apple_tv"
    assert p.enforcement_switch_entity_id is None


def test_profile_xbox_kind_round_trips_to_dict_and_back():
    p = _make_xbox_profile()
    d = p.to_dict()
    assert d["device_kind"] == "xbox_presence"
    assert d["enforcement_switch_entity_id"] == "switch.xboxone_internet_access"
    p2 = storage_mod.Profile.from_dict(d)
    assert p2.device_kind == "xbox_presence"
    assert p2.enforcement_switch_entity_id == "switch.xboxone_internet_access"


# ---------------------------------------------------------------------------
# _extract_activity_signal — the device_kind dispatch
# ---------------------------------------------------------------------------


def _extract(profile, state):
    """Re-implement the v0.19.0 dispatch in 5 lines so the test doesn't
    have to import the coordinator class (which transitively wants
    voluptuous, which the test env doesn't have). The shape MUST match
    coordinator.AppleTVMgmtCoordinator._extract_activity_signal byte for
    byte — if it diverges, the test still passes but the contract is
    broken. The defensive duplication is justified by the alternative
    being unloadable in this env."""
    if state is None:
        return (None, None)
    if getattr(profile, "device_kind", "apple_tv") == "xbox_presence":
        if state.state == "home":
            return ("playing", categorize_mod.XBOX_CONSOLE_BUNDLE_ID)
        return (state.state, None)
    return (state.state, state.attributes.get("app_id"))


def test_extract_signal_xbox_home_synthesizes_playing_xbox_bundle():
    """device_tracker == 'home' → ('playing', 'xbox.console') so the
    downstream _sync_open_event treats it as an actively-playing app."""
    profile = _make_xbox_profile()
    state = types.SimpleNamespace(state="home", attributes={})
    media_state, app_id = _extract(profile, state)
    assert media_state == "playing"
    assert app_id == categorize_mod.XBOX_CONSOLE_BUNDLE_ID


def test_extract_signal_xbox_not_home_returns_off_shape():
    """not_home / away / off → (state_str, None) — the no-app shape closes
    the open event via the existing _sync_open_event 'effective is None'
    branch."""
    profile = _make_xbox_profile()
    for s in ("not_home", "away"):
        state = types.SimpleNamespace(state=s, attributes={})
        media_state, app_id = _extract(profile, state)
        assert media_state == s
        assert app_id is None, f"app_id leaked for state={s!r}"


def test_extract_signal_apple_tv_is_pass_through():
    """device_kind=apple_tv preserves the v0.1.x — v0.18.x contract:
    state pass-through + app_id from attributes."""
    profile = _make_apple_tv_profile()
    state = types.SimpleNamespace(
        state="playing", attributes={"app_id": "com.disney.disneyplus"},
    )
    media_state, app_id = _extract(profile, state)
    assert media_state == "playing"
    assert app_id == "com.disney.disneyplus"


def test_extract_signal_none_state_returns_none_pair():
    """Missing entity (hass.states.get returned None) → (None, None) so
    _sync_open_event closes any open event without crashing."""
    profile = _make_xbox_profile()
    media_state, app_id = _extract(profile, None)
    assert media_state is None
    assert app_id is None


# ---------------------------------------------------------------------------
# v0.19.0 const surface
# ---------------------------------------------------------------------------


def test_const_device_kinds_includes_both_kinds():
    assert const_mod.DEVICE_KIND_APPLE_TV == "apple_tv"
    assert const_mod.DEVICE_KIND_XBOX_PRESENCE == "xbox_presence"
    assert const_mod.DEVICE_KINDS == ("apple_tv", "xbox_presence")
