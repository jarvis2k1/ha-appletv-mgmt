"""v0.19.1 — request-approval extensions are group-tagged too.

The panel "+min" button and the grant_extension service tag a manual
extension to the binding/active group so it lifts that GROUP budget (see
test_group_extension.py). The two REQUEST-APPROVAL paths in notify.py
(`_handle_decision` for the Companion notification action, and
`handle_external_decision` for the REST approve) must do the same —
otherwise a kid requests "more movie time", the parent approves, and the
grant lands daily-only while the movies GROUP cap keeps nagging (the exact
v0.19.1 bug via a different entry point).

These tests pin `_resolve_request_group` (the resolver) + the end-to-end
`handle_external_decision` grant call carrying `group=`.
"""
from __future__ import annotations

import asyncio
import importlib.util
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
        "homeassistant.core",
        "homeassistant.helpers",
        "homeassistant.helpers.storage",
        "homeassistant.util",
        "homeassistant.util.dt",
    ):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)
    core = sys.modules["homeassistant.core"]
    if not hasattr(core, "HomeAssistant"):
        core.HomeAssistant = type("HomeAssistant", (), {})
    if not hasattr(core, "Event"):
        core.Event = object
    if not hasattr(core, "callback"):
        core.callback = lambda f: f
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


def _load(name: str):
    full = f"custom_components.appletv_mgmt.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, PKG / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[full] = m
    spec.loader.exec_module(m)
    return m


# audit.py imports `format_message, should_speak, speak` from voice_notifier at
# top level. An earlier enforcer test may have already installed a PARTIAL
# voice_notifier stub (only should_speak + speak), so set each attribute
# idempotently on whatever module is live — never assume a fresh one.
_vn = "custom_components.appletv_mgmt.voice_notifier"
if _vn not in sys.modules:
    sys.modules[_vn] = types.ModuleType(_vn)
_vn_mod = sys.modules[_vn]
if not hasattr(_vn_mod, "format_message"):
    _vn_mod.format_message = lambda *a, **k: ""
if not hasattr(_vn_mod, "should_speak"):
    _vn_mod.should_speak = lambda profile, template: bool(template)
if not hasattr(_vn_mod, "speak"):
    _vn_mod.speak = AsyncMock(return_value={"status": "spoken", "message": "stub"})

for _name in ("const", "categorize", "storage"):
    _load(_name)

# sys.modules is shared across the whole suite. An earlier-collected enforcer
# test (e.g. test_binding_constraint) installs a PARTIAL `audit` stub lacking
# `resolve_extension_target_group`, and notify._resolve_request_group lazily
# does `from .audit import resolve_extension_target_group` — which ImportErrors
# against that stub. Graft the REAL function (exec'd fresh under a throwaway
# name) onto whichever audit module is live, so this test is order-independent
# WITHOUT clobbering the other stubbed attributes those tests rely on.
_audit_live = sys.modules.get("custom_components.appletv_mgmt.audit")
if _audit_live is None or not hasattr(_audit_live, "resolve_extension_target_group"):
    _spec = importlib.util.spec_from_file_location(
        "custom_components.appletv_mgmt._audit_real_for_test", PKG / "audit.py"
    )
    _real_audit = importlib.util.module_from_spec(_spec)
    _real_audit.__package__ = "custom_components.appletv_mgmt"
    _spec.loader.exec_module(_real_audit)
    if _audit_live is None:
        sys.modules["custom_components.appletv_mgmt.audit"] = _real_audit
    else:
        _audit_live.resolve_extension_target_group = (
            _real_audit.resolve_extension_target_group
        )

# Force a FRESH real notify module bound only to this test. The shared
# `custom_components.appletv_mgmt.notify` slot may hold a stub installed by an
# earlier-collected test (test_api_validators stubs handle_external_decision as
# a non-coroutine no-op). We exec our own real copy under a throwaway name and
# never touch the shared slot, so other tests keep their stub. Its lazy
# `from .audit import resolve_extension_target_group` resolves to the shared
# (grafted) audit via __package__.
_notify_spec = importlib.util.spec_from_file_location(
    "custom_components.appletv_mgmt._notify_real_for_test", PKG / "notify.py"
)
notify_mod = importlib.util.module_from_spec(_notify_spec)
notify_mod.__package__ = "custom_components.appletv_mgmt"
_notify_spec.loader.exec_module(notify_mod)

storage_mod = sys.modules["custom_components.appletv_mgmt.storage"]
DOMAIN = sys.modules["custom_components.appletv_mgmt.const"].DOMAIN
# The live (grafted) audit module — the shim above may leave `_audit_live` None
# while installing the real module into sys.modules, so read it back here.
audit_mod = sys.modules["custom_components.appletv_mgmt.audit"]

NOW = datetime(2026, 6, 19, 19, 25, 45, tzinfo=timezone.utc)


def _make_request(*, bundle_id="com.netflix.Netflix", minutes=30, status="pending"):
    return storage_mod.ExtensionRequest(
        id="req1",
        profile_id="p1",
        requested_minutes=minutes,
        reason="more please",
        requested_at=NOW,
        auto_expires_at=NOW + timedelta(minutes=30),
        status=status,
        bundle_id=bundle_id,
    )


def _make_coord(*, enforce_reason=None, current_group=None, group_for="gaming",
                group_totals=None, group_budgets=None):
    return types.SimpleNamespace(
        _enforcer=types.SimpleNamespace(enforce_reason=enforce_reason),
        data={
            "current_group": current_group,
            "group_totals_seconds": group_totals or {},
            "group_budgets_minutes": group_budgets or {},
        },
        _group_for=lambda bundle_id: group_for,
        async_request_refresh=AsyncMock(),
    )


def _make_hass(bundle):
    hass = MagicMock()
    hass.data = {DOMAIN: {"p1": bundle}}
    hass.bus = MagicMock()
    hass.bus.async_fire = MagicMock()
    return hass


# ---------------------------------------------------------------------------
# _resolve_request_group
# ---------------------------------------------------------------------------


def test_resolve_request_group_binding_group_wins():
    """When the kid is currently being nagged on group:movies, an approval
    tags to movies regardless of the request's own bundle."""
    coord = _make_coord(enforce_reason="group:movies", group_for="gaming")
    bundle = {"coordinator": coord, "store": MagicMock()}
    hass = _make_hass(bundle)
    req = _make_request(bundle_id="com.kooapps.game")  # would map to gaming
    assert notify_mod._resolve_request_group(hass, bundle, req) == "movies"


def test_resolve_request_group_active_group_fallback():
    """No binding constraint, but an app is on screen → tag the active group."""
    coord = _make_coord(enforce_reason=None, current_group="tv_shows")
    bundle = {"coordinator": coord, "store": MagicMock()}
    hass = _make_hass(bundle)
    req = _make_request()
    assert notify_mod._resolve_request_group(hass, bundle, req) == "tv_shows"


def test_resolve_request_group_falls_back_to_request_bundle():
    """Approval decided after the kid stopped watching (no binding, no active)
    → fall back to the group of the app they requested time for."""
    coord = _make_coord(enforce_reason=None, current_group=None, group_for="movies")
    bundle = {"coordinator": coord, "store": MagicMock()}
    hass = _make_hass(bundle)
    req = _make_request(bundle_id="com.netflix.Netflix")
    assert notify_mod._resolve_request_group(hass, bundle, req) == "movies"


def test_resolve_request_group_none_when_nothing_resolvable():
    """No binding, no active, request carries no bundle → daily-only (None)."""
    coord = _make_coord(enforce_reason=None, current_group=None, group_for=None)
    bundle = {"coordinator": coord, "store": MagicMock()}
    hass = _make_hass(bundle)
    req = _make_request(bundle_id=None)
    assert notify_mod._resolve_request_group(hass, bundle, req) is None


def test_resolve_request_group_group_for_raises_is_daily_only():
    """If _group_for blows up, the resolver degrades to daily-only, never
    crashing the approval."""
    def _boom(_):
        raise RuntimeError("categorize exploded")

    coord = _make_coord(enforce_reason=None, current_group=None)
    coord._group_for = _boom
    bundle = {"coordinator": coord, "store": MagicMock()}
    hass = _make_hass(bundle)
    req = _make_request(bundle_id="com.netflix.Netflix")
    assert notify_mod._resolve_request_group(hass, bundle, req) is None


# ---------------------------------------------------------------------------
# v0.20.4 — resolve_extension_target_group most-used fallback (direct grants)
# Repro: parent's "+X" during a tvOS-26 freeze gap (nothing playing) landed
# daily-only, so the binding movies cap kept nagging. fallback_most_used tags
# the grant to today's most-used capped group instead.
# ---------------------------------------------------------------------------


def test_direct_grant_falls_back_to_most_used_capped_group():
    coord = _make_coord(
        enforce_reason=None, current_group=None,
        group_totals={"movies": 5880, "gaming": 420},   # movies most-used
        group_budgets={"movies": 30, "gaming": 30},
    )
    hass = _make_hass({"coordinator": coord, "store": MagicMock()})
    assert audit_mod.resolve_extension_target_group(
        hass, "p1", fallback_most_used=True) == "movies"


def test_direct_grant_default_no_fallback_is_daily_only():
    # request-approval path (fallback_most_used defaults False) → None when idle,
    # so notify._resolve_request_group can defer to the *requested* app's group.
    coord = _make_coord(
        enforce_reason=None, current_group=None,
        group_totals={"movies": 5880}, group_budgets={"movies": 30},
    )
    hass = _make_hass({"coordinator": coord, "store": MagicMock()})
    assert audit_mod.resolve_extension_target_group(hass, "p1") is None


def test_most_used_fallback_skips_unlimited_zero_budget_group():
    # 'other' has the most usage but budget 0 (unlimited) → ineligible; movies wins
    coord = _make_coord(
        enforce_reason=None, current_group=None,
        group_totals={"other": 9000, "movies": 1200},
        group_budgets={"other": 0, "movies": 30},
    )
    hass = _make_hass({"coordinator": coord, "store": MagicMock()})
    assert audit_mod.resolve_extension_target_group(
        hass, "p1", fallback_most_used=True) == "movies"


def test_most_used_fallback_none_when_no_capped_usage():
    coord = _make_coord(
        enforce_reason=None, current_group=None,
        group_totals={}, group_budgets={"movies": 30},
    )
    hass = _make_hass({"coordinator": coord, "store": MagicMock()})
    assert audit_mod.resolve_extension_target_group(
        hass, "p1", fallback_most_used=True) is None


def test_binding_group_still_wins_over_most_used_fallback():
    # an active binding constraint takes priority over the most-used heuristic
    coord = _make_coord(
        enforce_reason="group:tv_shows", current_group=None,
        group_totals={"movies": 9000},
        group_budgets={"movies": 30, "tv_shows": 30},
    )
    hass = _make_hass({"coordinator": coord, "store": MagicMock()})
    assert audit_mod.resolve_extension_target_group(
        hass, "p1", fallback_most_used=True) == "tv_shows"


def test_most_used_fallback_eligible_for_linear_tv():
    # v0.21.0 — native TV (linear_tv) is a first-class capped group, so a direct
    # "+X" while it's today's most-used capped group relieves linear_tv (not
    # daily-only) exactly like movies/gaming would.
    coord = _make_coord(
        enforce_reason=None, current_group=None,
        group_totals={"linear_tv": 3600, "movies": 600},   # linear_tv most-used
        group_budgets={"linear_tv": 30, "movies": 60},
    )
    hass = _make_hass({"coordinator": coord, "store": MagicMock()})
    assert audit_mod.resolve_extension_target_group(
        hass, "p1", fallback_most_used=True) == "linear_tv"


# ---------------------------------------------------------------------------
# handle_external_decision — the REST approve path
# ---------------------------------------------------------------------------


def _make_store(request):
    store = MagicMock()
    store.get_request = MagicMock(return_value=request)
    store.update_request = MagicMock()
    store.add_extension_minutes = MagicMock(return_value=request.requested_minutes)
    store.async_save = AsyncMock()
    return store


def test_external_approval_grants_with_resolved_group():
    """Approving a request while movies is the binding constraint must call
    add_extension_minutes with group='movies' — not daily-only."""
    req = _make_request(minutes=30)
    store = _make_store(req)
    coord = _make_coord(enforce_reason="group:movies")
    bundle = {"store": store, "coordinator": coord}
    hass = _make_hass(bundle)

    asyncio.run(
        notify_mod.handle_external_decision(
            hass, "req1", approve=True, minutes=None
        )
    )

    store.add_extension_minutes.assert_called_once()
    args, kwargs = store.add_extension_minutes.call_args
    assert args[0] == "p1"
    assert args[1] == 30
    assert kwargs.get("group") == "movies", (
        f"approval landed daily-only (group={kwargs.get('group')!r}) — the "
        f"v0.19.1 group-tag is missing on the notify approval path."
    )


def test_external_approval_uses_request_bundle_when_idle():
    """No binding/active group at approval time → tag the requested app's
    group (Netflix → movies)."""
    req = _make_request(bundle_id="com.netflix.Netflix", minutes=20)
    store = _make_store(req)
    coord = _make_coord(enforce_reason=None, current_group=None, group_for="movies")
    bundle = {"store": store, "coordinator": coord}
    hass = _make_hass(bundle)

    asyncio.run(
        notify_mod.handle_external_decision(
            hass, "req1", approve=True, minutes=None
        )
    )
    _, kwargs = store.add_extension_minutes.call_args
    assert kwargs.get("group") == "movies"


def test_external_denial_does_not_grant():
    """Denied requests grant nothing — add_extension_minutes never called."""
    req = _make_request()
    store = _make_store(req)
    coord = _make_coord()
    bundle = {"store": store, "coordinator": coord}
    hass = _make_hass(bundle)

    asyncio.run(
        notify_mod.handle_external_decision(
            hass, "req1", approve=False, minutes=None
        )
    )
    store.add_extension_minutes.assert_not_called()
