"""v0.18.0 — coordinator integration test for DNS-corroborated attribution.

This is the ACCEPTANCE TEST defined in the workflow synthesis:
  "Replay today's 2026-06-14 06:27-08:33 incident against the new code
  with the feature enabled in correct mode. Expected: ONE Disney+ UsageEvent
  open from 06:27 with bundle_id='com.disney.disneyplus' (preserved truth),
  and group_segments=[(06:27..~06:36, 'movies'), (~06:36..08:33, 'gaming')].
  group_totals_today: movies ~9 min, gaming ~117 min. With feature disabled
  (default): bit-identical to v0.17.4 behavior — 126.7 min of movies."

We exercise `_check_dns_corroboration` directly (the same pattern as
test_stale_session.py — bypassing DataUpdateCoordinator inheritance via a
bare coordinator-like object).
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


# --- conftest pre-loads the pure modules. coordinator.py imports HA — load it
# with full HA stubs the same way test_stale_session.py does. ---


def _load_coordinator():
    pkg = "custom_components"
    sub = "custom_components.appletv_mgmt"
    for name in (pkg, sub):
        if name not in sys.modules:
            m = types.ModuleType(name); m.__path__ = []; sys.modules[name] = m

    # Stub the HA modules the coordinator imports at top level.
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

    # Pre-load const, then everything else.
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

    # Load storage with HA stubs in place.
    if f"{sub}.storage" not in sys.modules:
        path = (
            Path(__file__).parent.parent
            / "custom_components" / "appletv_mgmt" / "storage.py"
        )
        spec = importlib.util.spec_from_file_location(f"{sub}.storage", path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        spec.loader.exec_module(m)

    # Stub audit + enforcer — we don't exercise their internals here.
    if f"{sub}.audit" not in sys.modules:
        audit_stub = types.ModuleType(f"{sub}.audit")
        audit_stub.record_admin_action = MagicMock()
        sys.modules[f"{sub}.audit"] = audit_stub
    if f"{sub}.enforcer" not in sys.modules:
        enf_stub = types.ModuleType(f"{sub}.enforcer")
        enf_stub.EnforcementController = MagicMock
        sys.modules[f"{sub}.enforcer"] = enf_stub

    # Finally load coordinator.
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
dns_classifier_mod = sys.modules["custom_components.appletv_mgmt.dns_classifier"]
media_attr_mod = sys.modules["custom_components.appletv_mgmt.media_attribution"]


T0 = datetime(2026, 6, 14, 6, 27, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(minutes=30)  # "now" = 30 min into the session


def _make_profile(mode="correct", apple_tv_ip="192.168.1.24"):
    return storage_mod.Profile(
        id="p1",
        display_name="Living Room",
        apple_tv_entity_id="media_player.living_room_apple_tv",
        adguard_client_name="AppleTV",
        daily_budget_min=60,
        grace_seconds=60,
        warn_thresholds_min=[5],
        idle_grace_minutes=5,
        stale_session_minutes=5,
        apple_tv_ip=apple_tv_ip,
        dns_corroboration_mode=mode,
    )


class _BareCoordinator:
    """Test-only coordinator-like object that bypasses DataUpdateCoordinator's
    __init__. Mirrors the pattern in test_stale_session.py.

    We bind only the methods we exercise: _check_dns_corroboration,
    _apply_attribution_decision, _audit_attribution_correction, _group_for.
    """
    _check_dns_corroboration = coord_mod.AppleTVMgmtCoordinator._check_dns_corroboration
    _apply_attribution_decision = coord_mod.AppleTVMgmtCoordinator._apply_attribution_decision
    _audit_attribution_correction = coord_mod.AppleTVMgmtCoordinator._audit_attribution_correction
    _group_for = coord_mod.AppleTVMgmtCoordinator._group_for

    def __init__(self, profile, store, enforcer, hass):
        self._profile = profile
        self._store = store
        self._enforcer = enforcer
        self.hass = hass
        self._consecutive_ambient_ticks = 0
        self._dns_cache = None
        self._dns_lock = None
        self._last_corrected_group = {}
        self._itunes_lookups_attempted = set()
        # v0.18.0 diagnostic stash — mirrors the real coordinator's
        # __init__ so _check_dns_corroboration + _apply_attribution_decision
        # can update them.
        self._last_attribution_source = None
        self._last_dns_confidence = "NONE"


def _build(mode="correct", apple_tv_ip="192.168.1.24"):
    profile = _make_profile(mode=mode, apple_tv_ip=apple_tv_ip)
    # store mock
    store = MagicMock()
    store.async_save = AsyncMock(return_value=None)
    store.app_categories = MagicMock(return_value={})
    # adguard mock — we'll set query_recent_dns per-test
    adguard = MagicMock()
    adguard.query_recent_dns = AsyncMock(return_value=[])
    enforcer = MagicMock()
    enforcer._adguard = adguard
    # hass mock
    hass = MagicMock()
    hass.states = MagicMock()
    hass.bus = MagicMock()
    hass.bus.async_fire = MagicMock()
    c = _BareCoordinator(profile, store, enforcer, hass)
    return c, store, adguard, hass


def _state(state_str: str, last_updated: datetime):
    s = MagicMock()
    s.state = state_str
    s.last_updated = last_updated
    s.attributes = {}
    return s


def _open_event(bundle="com.disney.disneyplus", started=T0, segments=None):
    return storage_mod.UsageEvent(
        id=f"{int(started.timestamp()*1000)}-p1",
        profile_id="p1",
        bundle_id=bundle,
        started_at=started,
        ended_at=None,
        group_segments=segments or [],
    )


# ============================================================================
# The live-bug replay — the test the workflow synthesis defined as the
# ACCEPTANCE gate.
# ============================================================================


def test_live_bug_replay_disney_event_with_kooapps_dns_appends_gaming_segment():
    """v0.18.0 acceptance: replay today's incident.

    Setup: open event = Disney+, opened at 06:27. Now is 06:57 (30 min in).
    pyatv entity last_updated stuck at 06:27 (stale by 30 min). Samsung on.
    AdGuard returns recent DNS dominated by KooApps + Game Center.

    Expected (correct mode):
    - Open event still has bundle_id == com.disney.disneyplus (truth preserved)
    - Open event has ONE new GroupSegment(group='gaming', started_at≈now)
    - Audit row 'app_group_corrected' was written
    """
    c, store, adguard, hass = _build(mode="correct")
    ev = _open_event()
    store.open_event_for.return_value = ev
    hass.states.get.return_value = _state("playing", T0)

    # AdGuard returns 5 KooApps queries within the last minute -- well above
    # MIN_BUNDLE_CROSS_GROUP_HITS=3.
    def _ts(seconds_ago: int) -> str:
        return (NOW - timedelta(seconds=seconds_ago)).isoformat()

    adguard.query_recent_dns.return_value = [
        ("www.kooappsservers.com", _ts(5)),
        ("usa-kooapps-dlc.s3.amazonaws.com", _ts(15)),
        ("kaserver-new-2-elb-1744555362.us-east-1.elb.amazonaws.com", _ts(25)),
        ("nakiostudio.com", _ts(35)),
        ("kooapps.com", _ts(45)),
        # Some Apple background sprinkled in
        ("gateway.fe2.apple-dns.net", _ts(8)),
        ("time.apple.com", _ts(20)),
    ]

    # Patch audit module to capture writes
    audit_records = []
    audit_mod = sys.modules.get("custom_components.appletv_mgmt.audit")
    if audit_mod and hasattr(audit_mod, "record_admin_action"):
        original = audit_mod.record_admin_action
        audit_mod.record_admin_action = lambda *a, **kw: audit_records.append(kw)
    try:
        asyncio.run(c._check_dns_corroboration(now=NOW))

        # Truth preservation: bundle_id is untouched.
        assert ev.bundle_id == "com.disney.disneyplus"

        # Correction landed: open event now has exactly ONE gaming segment.
        assert len(ev.group_segments) == 1
        seg = ev.group_segments[0]
        assert seg.group == "gaming"
        assert seg.ended_at is None  # still-open segment
        assert seg.source == "dns_classifier"

        # store.async_save was called (we mutated the event).
        store.async_save.assert_awaited()

        # An audit row was emitted with the corrected action.
        assert any(
            r.get("action") == "app_group_corrected" for r in audit_records
        ), f"expected app_group_corrected, got: {audit_records}"
    finally:
        if audit_mod and hasattr(audit_mod, "record_admin_action"):
            audit_mod.record_admin_action = original


def test_live_bug_replay_monitor_mode_records_proposal_does_not_mutate():
    """Monitor mode: same DNS evidence, but UsageEvent.group_segments
    stays empty (no mutation), and the audit row is 'proposed' variant.
    This is the safe rollout step the owner runs for 1-2 weeks before
    flipping to correct."""
    c, store, adguard, hass = _build(mode="monitor")
    ev = _open_event()
    store.open_event_for.return_value = ev
    hass.states.get.return_value = _state("playing", T0)

    def _ts(seconds_ago: int) -> str:
        return (NOW - timedelta(seconds=seconds_ago)).isoformat()

    adguard.query_recent_dns.return_value = [
        ("www.kooappsservers.com", _ts(5)),
        ("usa-kooapps-dlc.s3.amazonaws.com", _ts(15)),
        ("nakiostudio.com", _ts(25)),
        ("kaserver-new-2-elb-1744555362.us-east-1.elb.amazonaws.com", _ts(35)),
    ]

    audit_records = []
    audit_mod = sys.modules.get("custom_components.appletv_mgmt.audit")
    if audit_mod and hasattr(audit_mod, "record_admin_action"):
        original = audit_mod.record_admin_action
        audit_mod.record_admin_action = lambda *a, **kw: audit_records.append(kw)
    try:
        asyncio.run(c._check_dns_corroboration(now=NOW))

        # MUST NOT mutate the event.
        assert ev.group_segments == []
        store.async_save.assert_not_awaited()

        # Audit row uses the _proposed variant.
        assert any(
            r.get("action") == "app_group_corrected_proposed"
            for r in audit_records
        ), f"expected app_group_corrected_proposed, got: {audit_records}"
    finally:
        if audit_mod and hasattr(audit_mod, "record_admin_action"):
            audit_mod.record_admin_action = original


# ============================================================================
# Feature OFF: bit-identical to v0.17.4 behavior
# ============================================================================


def test_feature_off_default_does_not_query_adguard():
    """Default mode is 'off'. AdGuard MUST NOT be queried (no hot-path
    cost for non-opted-in users)."""
    c, store, adguard, hass = _build(mode="off")
    ev = _open_event()
    store.open_event_for.return_value = ev
    hass.states.get.return_value = _state("playing", T0)

    asyncio.run(c._check_dns_corroboration(now=NOW))

    adguard.query_recent_dns.assert_not_called()
    store.async_save.assert_not_awaited()
    assert ev.group_segments == []


def test_feature_on_but_no_ip_set_is_a_noop():
    """apple_tv_ip='' forces the feature off regardless of mode flag.
    Safety: an install that flips mode to 'correct' without filling in
    the IP must NOT spuriously act."""
    c, store, adguard, hass = _build(mode="correct", apple_tv_ip="")
    ev = _open_event()
    store.open_event_for.return_value = ev
    hass.states.get.return_value = _state("playing", T0)

    asyncio.run(c._check_dns_corroboration(now=NOW))

    adguard.query_recent_dns.assert_not_called()
    assert ev.group_segments == []


# ============================================================================
# Fail-open under AdGuard outage
# ============================================================================


def test_adguard_outage_falls_back_to_v0173_behavior():
    """AdGuard error -> empty DNS rows -> Confidence.NONE -> PRESERVE.
    No mutation, no audit row. Same outcome as v0.17.4."""
    c, store, adguard, hass = _build(mode="correct")
    ev = _open_event()
    store.open_event_for.return_value = ev
    hass.states.get.return_value = _state("playing", T0)
    adguard.query_recent_dns.return_value = []  # outage -> empty

    asyncio.run(c._check_dns_corroboration(now=NOW))

    assert ev.group_segments == []
    store.async_save.assert_not_awaited()


def test_adguard_raises_is_treated_as_no_signal():
    """If query_recent_dns somehow raises (defensive belt for caller),
    the integration must not crash the tick."""
    c, store, adguard, hass = _build(mode="correct")
    ev = _open_event()
    store.open_event_for.return_value = ev
    hass.states.get.return_value = _state("playing", T0)
    adguard.query_recent_dns.side_effect = RuntimeError("boom")

    # Must not raise
    asyncio.run(c._check_dns_corroboration(now=NOW))

    assert ev.group_segments == []


# ============================================================================
# Anti-leak: Disney+ + Game Center heartbeat must NOT be misclassified
# ============================================================================


def test_disney_plus_with_only_game_center_heartbeat_not_promoted_to_gaming():
    """The classic false-positive scenario. The curated-bundle safelist
    in decide_attribution prevents GROUP_ONLY signals from downgrading
    streaming bundles."""
    c, store, adguard, hass = _build(mode="correct")
    ev = _open_event()
    store.open_event_for.return_value = ev
    hass.states.get.return_value = _state("playing", T0)

    def _ts(s: int) -> str:
        return (NOW - timedelta(seconds=s)).isoformat()

    adguard.query_recent_dns.return_value = [
        # ONLY GC + background — no specific game CDN
        ("stats.gc.fe2.apple-dns.net", _ts(10)),
        ("profile.gc.fe2.apple-dns.net", _ts(20)),
        ("gateway.fe2.apple-dns.net", _ts(15)),
        ("time.apple.com", _ts(25)),
    ]

    asyncio.run(c._check_dns_corroboration(now=NOW))

    # CRITICAL: Disney+ must NOT have a gaming segment appended.
    assert ev.group_segments == []
    store.async_save.assert_not_awaited()


# ============================================================================
# pyatv fresh -> trust pyatv (DNS does not override)
# ============================================================================


def test_pyatv_fresh_disables_dns_correction():
    """If pyatv just pushed (last_updated < 90s ago), we trust it
    completely — even if DNS would suggest a different group."""
    c, store, adguard, hass = _build(mode="correct")
    ev = _open_event()
    store.open_event_for.return_value = ev
    # Apple TV pushed ~30s ago — pyatv is talking
    hass.states.get.return_value = _state("playing", NOW - timedelta(seconds=30))

    def _ts(s: int) -> str:
        return (NOW - timedelta(seconds=s)).isoformat()

    adguard.query_recent_dns.return_value = [
        ("www.kooappsservers.com", _ts(5)),
        ("usa-kooapps-dlc.s3.amazonaws.com", _ts(15)),
        ("kooapps.com", _ts(25)),
    ]

    asyncio.run(c._check_dns_corroboration(now=NOW))

    # pyatv-fresh rule kicks in — DNS is not consulted to override.
    assert ev.group_segments == []


# ============================================================================
# Dedup: same correction across ticks doesn't re-emit
# ============================================================================


def test_same_correction_across_ticks_only_audits_once():
    """The classifier-driven path runs every 30s. We must NOT emit a
    fresh audit row each tick for the same ongoing correction. Dedup is
    keyed by (open_event_id, new_group)."""
    c, store, adguard, hass = _build(mode="correct")
    ev = _open_event()
    store.open_event_for.return_value = ev
    hass.states.get.return_value = _state("playing", T0)

    def _ts(s: int) -> str:
        return (NOW - timedelta(seconds=s)).isoformat()

    adguard.query_recent_dns.return_value = [
        ("www.kooappsservers.com", _ts(5)),
        ("usa-kooapps-dlc.s3.amazonaws.com", _ts(10)),
        ("kooapps.com", _ts(15)),
    ]

    audit_records = []
    audit_mod = sys.modules.get("custom_components.appletv_mgmt.audit")
    if audit_mod and hasattr(audit_mod, "record_admin_action"):
        original = audit_mod.record_admin_action
        audit_mod.record_admin_action = lambda *a, **kw: audit_records.append(kw)
    try:
        asyncio.run(c._check_dns_corroboration(now=NOW))
        first_audit_count = len(audit_records)
        # Second tick a minute later — same DNS pattern, same event.
        c._dns_cache = None  # bypass TTL; simulate cache miss
        asyncio.run(c._check_dns_corroboration(now=NOW + timedelta(minutes=1)))

        # Only ONE app_group_corrected total, despite two ticks.
        corrected = [
            r for r in audit_records
            if r.get("action") == "app_group_corrected"
        ]
        assert len(corrected) == 1, (
            f"expected 1 audit, got {len(corrected)}: {corrected}"
        )
        # And the segment list still has just one entry (no churn).
        assert len(ev.group_segments) == 1
    finally:
        if audit_mod and hasattr(audit_mod, "record_admin_action"):
            audit_mod.record_admin_action = original


# ============================================================================
# End-to-end breakdown: replay the full session shape and check minutes
# ============================================================================


def test_close_proposed_deduped_across_ticks():
    """v0.18.0 follow-up — close proposals must dedup the same way group
    corrections do. Live monitor-mode caught the bug: every 30s tick
    re-emitted `app_attribution_close_proposed` for the entire AMBIENT
    window (30+ rows in 20 min on the live install)."""
    c, store, adguard, hass = _build(mode="monitor")
    # Non-curated bundle so the AMBIENT close path engages.
    ev = _open_event(bundle="com.someobscure.app")
    store.open_event_for.return_value = ev
    # pyatv stale (last_updated 30 min ago)
    hass.states.get.return_value = _state("playing", T0)

    # AdGuard returns only background noise -> AMBIENT_ONLY.
    def _ts(s: int) -> str:
        return (NOW - timedelta(seconds=s)).isoformat()
    adguard.query_recent_dns.return_value = [
        ("gateway.fe2.apple-dns.net", _ts(5)),
        ("time.apple.com", _ts(20)),
    ]

    audit_records = []
    audit_mod = sys.modules.get("custom_components.appletv_mgmt.audit")
    original = None
    if audit_mod and hasattr(audit_mod, "record_admin_action"):
        original = audit_mod.record_admin_action
        audit_mod.record_admin_action = lambda *a, **kw: audit_records.append(kw)
    try:
        # Walk the ambient streak past the close threshold (4 ticks).
        # We have to bypass the TTL cache between ticks to simulate
        # 30s having passed without using real time.
        for tick in range(8):
            c._dns_cache = None  # bust TTL
            tick_now = NOW + timedelta(seconds=30 * tick)
            asyncio.run(c._check_dns_corroboration(now=tick_now))

        # Filter to close proposals on THIS open event.
        close_rows = [
            r for r in audit_records
            if r.get("action") == "app_attribution_close_proposed"
        ]
        assert len(close_rows) <= 1, (
            f"close proposals must be deduped, got {len(close_rows)}: "
            f"{[r.get('detail','')[:80] for r in close_rows]}"
        )
    finally:
        if original:
            audit_mod.record_admin_action = original


def test_disney_plus_ambient_session_does_not_propose_close():
    """v0.18.0 follow-up — Disney+ uses long-lived TCP + heavy buffering.
    A real Disney+ session goes AMBIENT for long stretches. Curated
    streaming safelist must protect against the close. Live-bug repro
    from the monitor-mode rollout on a live install."""
    c, store, adguard, hass = _build(mode="monitor")
    ev = _open_event(bundle="com.disney.disneyplus")
    store.open_event_for.return_value = ev
    hass.states.get.return_value = _state("playing", T0)

    def _ts(s: int) -> str:
        return (NOW - timedelta(seconds=s)).isoformat()
    adguard.query_recent_dns.return_value = [
        ("gateway.fe2.apple-dns.net", _ts(5)),
        ("time.apple.com", _ts(20)),
    ]

    audit_records = []
    audit_mod = sys.modules.get("custom_components.appletv_mgmt.audit")
    original = None
    if audit_mod and hasattr(audit_mod, "record_admin_action"):
        original = audit_mod.record_admin_action
        audit_mod.record_admin_action = lambda *a, **kw: audit_records.append(kw)
    try:
        for tick in range(10):
            c._dns_cache = None
            tick_now = NOW + timedelta(seconds=30 * tick)
            asyncio.run(c._check_dns_corroboration(now=tick_now))

        # Disney+ is a curated bundle -> close NOT proposed.
        close_rows = [
            r for r in audit_records
            if r.get("action") == "app_attribution_close_proposed"
        ]
        assert close_rows == [], (
            f"Disney+ must NOT have close proposed during AMBIENT-only "
            f"window, got: {[r.get('detail','')[:80] for r in close_rows]}"
        )
    finally:
        if original:
            audit_mod.record_admin_action = original


def test_e2e_breakdown_matches_workflow_acceptance_criteria():
    """The workflow synthesis's P0 ACCEPTANCE criterion:
    'movies <= 15 min (Disney+ ~9 min preserved via group_segments),
    gaming >= 110 min, other == 0. Total time = ~126.7 min ± 1 min.'

    We construct the event + segment shape the v0.18.0 coordinator would
    produce on the live bug, then verify the breakdown helper computes
    correctly."""
    ev_start = T0  # 06:27
    ev_end = T0 + timedelta(minutes=126, seconds=44)  # 08:33:44 — full 126.7 min
    # At 06:36:18 (9m18s in) — when Disney+ DNS stopped and KooApps started —
    # decide_attribution would have fired ANNOTATE_GROUP(gaming).
    correction_at = T0 + timedelta(minutes=9, seconds=18)
    ev = storage_mod.UsageEvent(
        id="evt1", profile_id="p1",
        bundle_id="com.disney.disneyplus",
        started_at=ev_start, ended_at=ev_end,
        group_segments=[storage_mod.GroupSegment(
            started_at=correction_at, ended_at=None,
            group="gaming", source="dns_classifier", confidence="BUNDLE",
        )],
    )
    breakdown = ev.group_seconds_breakdown("movies", now=ev_end)
    total = sum(breakdown.values())
    assert ev.duration_seconds() == total  # no time lost
    movies_min = breakdown.get("movies", 0) / 60
    gaming_min = breakdown.get("gaming", 0) / 60
    other_min = breakdown.get("other", 0) / 60
    assert movies_min <= 15, f"movies should be <=15 min, got {movies_min:.1f}"
    assert gaming_min >= 110, f"gaming should be >=110 min, got {gaming_min:.1f}"
    assert other_min == 0, f"other should be 0, got {other_min:.1f}"
    total_min = total / 60
    assert 125 < total_min < 128


# ============================================================================
# v0.18.0 diagnostic sensors — verify _check_dns_corroboration updates the
# state fields the new sensors read from. The full sensor wiring is
# covered in test_diag_sensors.py; this is the integration-level check.
# ============================================================================


def test_diagnostic_state_set_after_annotate_group():
    """After an annotate_group decision, the diagnostic stash should
    surface BUNDLE confidence and an 'annotate_group:...' source string.
    This is what the diagnostic sensors expose to the user."""
    c, store, adguard, hass = _build(mode="correct")
    ev = _open_event()
    store.open_event_for.return_value = ev
    hass.states.get.return_value = _state("playing", T0)

    def _ts(s):
        return (NOW - timedelta(seconds=s)).isoformat()

    adguard.query_recent_dns.return_value = [
        ("www.kooappsservers.com", _ts(5)),
        ("usa-kooapps-dlc.s3.amazonaws.com", _ts(15)),
        ("kooapps.com", _ts(25)),
        ("nakiostudio.com", _ts(35)),
    ]
    asyncio.run(c._check_dns_corroboration(now=NOW))
    assert c._last_dns_confidence == "BUNDLE"
    assert c._last_attribution_source is not None
    assert c._last_attribution_source.startswith("annotate_group")


def test_diagnostic_state_disabled_when_feature_off():
    """Mode='off' -> sensor reflects 'disabled' / 'NONE'."""
    c, store, adguard, hass = _build(mode="off")
    asyncio.run(c._check_dns_corroboration(now=NOW))
    assert c._last_attribution_source == "disabled"
    assert c._last_dns_confidence == "NONE"


def test_diagnostic_state_no_open_event():
    """No open event -> sensor reflects 'no_open_event' / 'NONE'."""
    c, store, adguard, hass = _build(mode="correct")
    store.open_event_for.return_value = None
    asyncio.run(c._check_dns_corroboration(now=NOW))
    assert c._last_attribution_source == "no_open_event"
    assert c._last_dns_confidence == "NONE"
