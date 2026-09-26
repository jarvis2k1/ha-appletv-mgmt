"""v0.18.0 — coordinator integration test for proactive pyatv reload.

Exercises `_check_pyatv_reload` directly via a bare coordinator-like
object — same scaffolding pattern as `test_dns_corroboration_integration`.

Asserts the wiring:
- All gates pass on tick 1 -> `hass.config_entries.async_reload` is called
  exactly once with the correct entry id, `_last_pyatv_reload_at` is set,
  and an audit row `pyatv_reload_triggered` is recorded.
- Tick 2 inside the 10-min rate limit -> async_reload NOT called again.
- Tick 3 past the rate limit window -> async_reload called once more.
- Feature off (apple_tv_entry_id="") -> no-op, AdGuard not queried.
- Samsung off -> no reload (fail-CLOSED).
- AdGuard returns 0 hits -> no reload (online corroborator failed).
- AdGuard raises -> treated as 0 hits -> no reload (defensive belt).
- async_reload itself raises -> tick does not crash; rate-limit still set.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _load_coordinator():
    pkg = "custom_components"
    sub = "custom_components.appletv_mgmt"
    for name in (pkg, sub):
        if name not in sys.modules:
            m = types.ModuleType(name); m.__path__ = []; sys.modules[name] = m

    ha_stubs = {
        "homeassistant": {},
        "homeassistant.core": {
            "Event": type("Event", (), {}),
            "EventStateChangedData": type("EventStateChangedData", (), {}),
            "HomeAssistant": type("HomeAssistant", (), {}),
            "callback": (lambda f: f),
        },
        "homeassistant.helpers": {},
        "homeassistant.helpers.event": {
            "async_track_state_change_event": (lambda *a, **k: lambda: None),
            "async_track_time_interval": (lambda *a, **k: lambda: None),
        },
        "homeassistant.helpers.update_coordinator": {
            "DataUpdateCoordinator": type(
                "DataUpdateCoordinator",
                (),
                {
                    "__init_subclass__": classmethod(lambda cls, **_: None),
                    "__class_getitem__": classmethod(lambda cls, key: cls),
                    "__init__": (lambda self, *a, **k: None),
                },
            ),
        },
        "homeassistant.helpers.aiohttp_client": {
            "async_get_clientsession": (lambda *a, **k: None),
        },
        "homeassistant.helpers.storage": {"Store": type("Store", (), {})},
        "homeassistant.util": {},
        "homeassistant.util.dt": {
            "utcnow": (lambda: datetime.now(timezone.utc)),
            "as_local": (lambda dt: dt),
        },
    }
    for name, attrs in ha_stubs.items():
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
            sys.modules[name].__path__ = []
        for k, v in attrs.items():
            if not hasattr(sys.modules[name], k):
                setattr(sys.modules[name], k, v)

    for modname in (
        "const", "state", "adguard", "quiet", "categorize",
        "schedule", "media_attribution", "dns_classifier",
    ):
        if f"{sub}.{modname}" not in sys.modules:
            path = (
                Path(__file__).parent.parent
                / "custom_components" / "appletv_mgmt" / f"{modname}.py"
            )
            spec = importlib.util.spec_from_file_location(
                f"{sub}.{modname}", path)
            m = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = m
            spec.loader.exec_module(m)

    if f"{sub}.storage" not in sys.modules:
        path = (
            Path(__file__).parent.parent
            / "custom_components" / "appletv_mgmt" / "storage.py"
        )
        spec = importlib.util.spec_from_file_location(f"{sub}.storage", path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        spec.loader.exec_module(m)

    if f"{sub}.audit" not in sys.modules:
        audit_stub = types.ModuleType(f"{sub}.audit")
        audit_stub.record_admin_action = MagicMock()
        sys.modules[f"{sub}.audit"] = audit_stub
    if f"{sub}.enforcer" not in sys.modules:
        enf_stub = types.ModuleType(f"{sub}.enforcer")
        enf_stub.EnforcementController = MagicMock
        sys.modules[f"{sub}.enforcer"] = enf_stub

    path = (
        Path(__file__).parent.parent
        / "custom_components" / "appletv_mgmt" / "coordinator.py"
    )
    spec = importlib.util.spec_from_file_location(f"{sub}.coordinator", path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


coord_mod = _load_coordinator()
storage_mod = sys.modules["custom_components.appletv_mgmt.storage"]
media_attr_mod = sys.modules["custom_components.appletv_mgmt.media_attribution"]


# Constants from the pure module — referenced here so threshold changes
# stay in sync with both unit + integration tests.
PYATV_RELOAD_MIN_STUCK_S = media_attr_mod.PYATV_RELOAD_MIN_STUCK_S
PYATV_RELOAD_RATE_LIMIT_S = media_attr_mod.PYATV_RELOAD_RATE_LIMIT_S
DNS_RECENT_WINDOW_S = media_attr_mod.DNS_RECENT_WINDOW_S


# Reference timestamps — "now" is when each tick fires; `last_updated`
# on the apple_tv entity is fixed at T0 (way past the 10-min threshold).
T0 = datetime(2026, 6, 14, 14, 0, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(seconds=PYATV_RELOAD_MIN_STUCK_S + 60)  # 11 min later


_APPLE_TV_ENTRY_ID = "atv_core_abc123def456"


def _make_profile(
    *,
    apple_tv_entry_id=_APPLE_TV_ENTRY_ID,
    apple_tv_ip="192.168.1.24",
    tv_entity_id="media_player.samsung_tv",
):
    return storage_mod.Profile(
        id="p1",
        display_name="Living Room",
        apple_tv_entity_id="media_player.heimkinoaaa",
        adguard_client_name="AppleTV",
        daily_budget_min=60,
        grace_seconds=60,
        warn_thresholds_min=[5],
        idle_grace_minutes=5,
        stale_session_minutes=5,
        tv_entity_id=tv_entity_id,
        apple_tv_ip=apple_tv_ip,
        apple_tv_entry_id=apple_tv_entry_id,
    )


class _BareCoordinator:
    """Bypasses DataUpdateCoordinator.__init__; same pattern as the DNS
    integration test."""
    _check_pyatv_reload = coord_mod.AppleTVMgmtCoordinator._check_pyatv_reload

    def __init__(self, profile, store, enforcer, hass):
        self._profile = profile
        self._store = store
        self._enforcer = enforcer
        self.hass = hass
        self._last_pyatv_reload_at = None


def _state(state_str, last_updated):
    s = MagicMock()
    s.state = state_str
    s.last_updated = last_updated
    s.attributes = {}
    return s


def _build(
    *,
    apple_tv_state="idle",
    apple_tv_last_updated=T0,
    samsung_state="on",
    dns_rows=None,
    dns_raises=False,
    reload_raises=False,
    profile_kwargs=None,
):
    """Construct a coordinator harness pre-loaded with the requested state.

    Returns (coordinator, async_reload_mock, audit_records_list).
    """
    profile = _make_profile(**(profile_kwargs or {}))
    store = MagicMock()
    store.async_save = AsyncMock(return_value=None)

    adguard = MagicMock()
    if dns_raises:
        adguard.query_recent_dns = AsyncMock(side_effect=RuntimeError("adguard down"))
    else:
        rows = dns_rows if dns_rows is not None else [
            # 3 recent DNS hits (well above DNS_RECENT_MIN_HITS=1)
            ("a.example.com", (NOW - timedelta(seconds=5)).isoformat()),
            ("b.example.com", (NOW - timedelta(seconds=15)).isoformat()),
            ("c.example.com", (NOW - timedelta(seconds=30)).isoformat()),
        ]
        adguard.query_recent_dns = AsyncMock(return_value=rows)

    enforcer = MagicMock()
    enforcer._adguard = adguard

    hass = MagicMock()
    hass.states = MagicMock()
    # Two states: the Apple TV entity (apple_tv_state/last_updated) and
    # the Samsung TV (samsung_state). states.get dispatches by entity id.
    apple_tv_st = _state(apple_tv_state, apple_tv_last_updated)
    samsung_st = _state(samsung_state, T0) if samsung_state is not None else None

    def _states_get(eid):
        if eid == profile.apple_tv_entity_id:
            return apple_tv_st
        if eid == profile.tv_entity_id:
            return samsung_st
        return None

    hass.states.get.side_effect = _states_get

    # config_entries.async_reload — the call this whole feature exists to make.
    hass.config_entries = MagicMock()
    if reload_raises:
        hass.config_entries.async_reload = AsyncMock(
            side_effect=RuntimeError("reload failed")
        )
    else:
        hass.config_entries.async_reload = AsyncMock(return_value=True)

    c = _BareCoordinator(profile, store, enforcer, hass)

    # NOTE: audit-module patching is the responsibility of the
    # `audit_records` pytest fixture below (autouse), which guarantees
    # the original `record_admin_action` symbol is restored at the end
    # of each test. Patching here would leak across to other test files
    # that expect a MagicMock (e.g. test_stale_session.py:830 calls
    # `audit_mod.record_admin_action.reset_mock()`).
    return c, hass.config_entries.async_reload, _AUDIT_RECORDS, adguard


# Module-level list reset per-test by the `audit_records` autouse fixture.
_AUDIT_RECORDS: list[dict] = []


@pytest.fixture(autouse=True)
def audit_records_fixture():
    """Per-test patch of `audit.record_admin_action` -> appends to
    `_AUDIT_RECORDS`. Restored after the test so neighboring test files
    that expect the original symbol (typically a MagicMock from their own
    setup) are not contaminated.
    """
    audit_mod = sys.modules.get("custom_components.appletv_mgmt.audit")
    original = None
    if audit_mod is not None and hasattr(audit_mod, "record_admin_action"):
        original = audit_mod.record_admin_action
        audit_mod.record_admin_action = (
            lambda *a, **kw: _AUDIT_RECORDS.append(kw)
        )
    _AUDIT_RECORDS.clear()
    try:
        yield _AUDIT_RECORDS
    finally:
        if audit_mod is not None and original is not None:
            audit_mod.record_admin_action = original


# ============================================================================
# Happy path: reload fires exactly once, audit row written, rate-limit set
# ============================================================================


def test_reload_fires_when_all_gates_satisfied():
    """Live-bug shape: apple_tv stuck at `idle` for 11 min, Samsung on,
    DNS firing -> `hass.config_entries.async_reload(_APPLE_TV_ENTRY_ID)`
    is called exactly once."""
    c, reload_mock, audits, _ = _build()

    asyncio.run(c._check_pyatv_reload(now=NOW))

    reload_mock.assert_awaited_once_with(_APPLE_TV_ENTRY_ID)
    # Rate-limit timestamp set so the NEXT tick within 10 min won't fire.
    assert c._last_pyatv_reload_at == NOW
    # Audit row recorded with kind=pyatv_reload_triggered.
    assert any(
        r.get("action") == "pyatv_reload_triggered" for r in audits
    ), f"expected pyatv_reload_triggered audit, got: {audits}"


def test_reload_rate_limits_second_tick_inside_window():
    """Tick 1: all gates pass -> fire. Tick 2 (5 min later): all gates
    STILL pass but rate-limit holds -> NO fire. Tick 3 (15 min after
    tick 1): rate-limit expired -> fire again."""
    c, reload_mock, audits, _ = _build()

    # Tick 1: fire.
    asyncio.run(c._check_pyatv_reload(now=NOW))
    assert reload_mock.await_count == 1

    # Tick 2: 5 min later, well inside the 10-min rate limit window.
    tick2 = NOW + timedelta(minutes=5)
    asyncio.run(c._check_pyatv_reload(now=tick2))
    assert reload_mock.await_count == 1, (
        "reload must not fire again inside the rate-limit window"
    )

    # Tick 3: 11 min after tick 1 -> rate limit expired.
    tick3 = NOW + timedelta(minutes=11)
    asyncio.run(c._check_pyatv_reload(now=tick3))
    assert reload_mock.await_count == 2
    assert c._last_pyatv_reload_at == tick3


# ============================================================================
# Feature OFF: no apple_tv_entry_id, no AdGuard query, no reload
# ============================================================================


def test_no_op_when_apple_tv_entry_id_empty():
    """Feature disabled by empty string. Must NOT even query AdGuard
    (avoid wasted hot-path cost for non-opted-in installs)."""
    c, reload_mock, audits, adguard = _build(
        profile_kwargs={"apple_tv_entry_id": ""}
    )

    asyncio.run(c._check_pyatv_reload(now=NOW))

    reload_mock.assert_not_awaited()
    adguard.query_recent_dns.assert_not_awaited()
    assert c._last_pyatv_reload_at is None
    assert audits == []


# ============================================================================
# Samsung-off gate: fail-closed
# ============================================================================


def test_no_reload_when_samsung_off():
    """Strict 'on' gate. Samsung off -> kid isn't watching -> skip reload."""
    c, reload_mock, audits, _ = _build(samsung_state="off")

    asyncio.run(c._check_pyatv_reload(now=NOW))

    reload_mock.assert_not_awaited()
    assert c._last_pyatv_reload_at is None


def test_no_reload_when_samsung_unknown():
    """Strict gate — unknown is rejected (unlike stale-session which fails
    open on unknown). A wrong reload is more disruptive than a missed
    one."""
    c, reload_mock, _, _ = _build(samsung_state="unknown")

    asyncio.run(c._check_pyatv_reload(now=NOW))

    reload_mock.assert_not_awaited()


def test_no_reload_when_samsung_not_configured():
    """No tv_entity_id configured -> Samsung gate cannot evaluate -> skip
    reload. YAML self-heal covers this fallback path."""
    c, reload_mock, _, _ = _build(
        samsung_state=None,
        profile_kwargs={"tv_entity_id": None},
    )

    asyncio.run(c._check_pyatv_reload(now=NOW))

    reload_mock.assert_not_awaited()


# ============================================================================
# Apple TV state gate
# ============================================================================


def test_no_reload_when_apple_tv_playing():
    """Stale `playing` is owned by the stale-session path, not this one."""
    c, reload_mock, _, _ = _build(apple_tv_state="playing")

    asyncio.run(c._check_pyatv_reload(now=NOW))

    reload_mock.assert_not_awaited()


def test_no_reload_when_apple_tv_off():
    """Device truly off — no point reloading pyatv."""
    c, reload_mock, _, _ = _build(apple_tv_state="off")

    asyncio.run(c._check_pyatv_reload(now=NOW))

    reload_mock.assert_not_awaited()


# ============================================================================
# pyatv-quiet-s gate
# ============================================================================


def test_no_reload_when_apple_tv_recently_updated():
    """last_updated 1 min ago -> entity is healthy -> no reload."""
    c, reload_mock, _, _ = _build(
        apple_tv_last_updated=NOW - timedelta(seconds=60)
    )

    asyncio.run(c._check_pyatv_reload(now=NOW))

    reload_mock.assert_not_awaited()


# ============================================================================
# DNS-recency corroborator
# ============================================================================


def test_no_reload_when_dns_empty():
    """0 DNS hits -> device might be sleeping -> fail-closed (no reload)."""
    c, reload_mock, _, _ = _build(dns_rows=[])

    asyncio.run(c._check_pyatv_reload(now=NOW))

    reload_mock.assert_not_awaited()


def test_no_reload_when_adguard_raises():
    """AdGuard query raises -> treated as 0 hits (fail-CLOSED). Note this
    is OPPOSITE the DNS-corroboration feature's fail-open posture, because
    the consequence of a wrong reload is worse than a missed reload."""
    c, reload_mock, _, _ = _build(dns_raises=True)

    asyncio.run(c._check_pyatv_reload(now=NOW))

    reload_mock.assert_not_awaited()


def test_no_reload_when_adguard_client_not_wired():
    """If enforcer._adguard is None (e.g. integration without AdGuard
    configured), DNS hits stay at 0 -> fail-closed."""
    c, reload_mock, _, _ = _build()
    c._enforcer._adguard = None

    asyncio.run(c._check_pyatv_reload(now=NOW))

    reload_mock.assert_not_awaited()


# ============================================================================
# async_reload failure does not crash the tick
# ============================================================================


def test_reload_failure_is_caught_and_rate_limit_still_set():
    """If async_reload raises (broken integration), the coordinator tick
    must NOT crash. The rate-limit timestamp IS still set so we don't
    retry-storm a broken reload path every 30s tick."""
    c, reload_mock, audits, _ = _build(reload_raises=True)

    # Must not raise.
    asyncio.run(c._check_pyatv_reload(now=NOW))

    reload_mock.assert_awaited_once_with(_APPLE_TV_ENTRY_ID)
    # Critically: timestamp set BEFORE the await, so a failed reload
    # still rate-limits the next attempt.
    assert c._last_pyatv_reload_at == NOW
    # Audit row was written before the reload was attempted.
    assert any(
        r.get("action") == "pyatv_reload_triggered" for r in audits
    )
