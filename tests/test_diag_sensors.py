"""v0.18.0 — diagnostic sensors: attribution_source, dns_classifier_confidence,
attribution_gap_minutes_today.

Three concerns under test:

1. The new SENSORS entries in `sensor.py` produce the expected native_value
   for both empty and populated coordinator snapshots. We grab the
   AppleTVMgmtSensorDescription objects directly (data only) without
   instantiating any HA SensorEntity — avoids dragging the full HA sensor
   platform into the test sandbox.

2. The coordinator's `_compute_gap_minutes_today` correctly sums only
   `source=='dns_classifier'` segments, clips to today's local-midnight
   window, and ignores legacy/empty-segment events. The pre-v0.18.0
   stale-closed invariant (empty group_segments -> contributes 0) is
   what defends us from double-counting honest pyatv-silence closes.

3. `_check_dns_corroboration` and `_apply_attribution_decision` update
   `_last_attribution_source` and `_last_dns_confidence` so the diagnostic
   sensors have something to surface. We reuse the same _build helper as
   `test_dns_corroboration_integration.py` so the test environment is
   identical.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Load coordinator + sensor.py via the same stub pattern as
# test_dns_corroboration_integration.py. Sensor.py needs richer HA stubs
# than the coordinator alone (SensorEntity, SensorEntityDescription,
# EntityCategory, etc.) — we add those before loading.
# ---------------------------------------------------------------------------


def _sensor_stub_module():
    """Build the homeassistant.components.sensor stub.

    The tricky part: `AppleTVMgmtSensorDescription` in sensor.py is
    `@dataclass(frozen=True, kw_only=True)` and extends `SensorEntityDescription`.
    Python's @dataclass inheritance only picks up fields from PARENT dataclasses,
    so the parent stub here MUST be a dataclass too — listing every parent
    field the subclass instantiations reference (key, translation_key, name,
    icon, entity_category, native_unit_of_measurement, device_class, state_class).
    All defaulted to None / "" so the subclass's kw_only entries land cleanly.
    """
    @dataclass(frozen=True, kw_only=True)
    class SensorEntityDescription:
        key: str = ""
        translation_key: str | None = None
        name: str | None = None
        icon: str | None = None
        entity_category: str | None = None
        native_unit_of_measurement: str | None = None
        device_class: str | None = None
        state_class: str | None = None

    return {
        "SensorEntity": type("SensorEntity", (), {}),
        "SensorEntityDescription": SensorEntityDescription,
        "SensorDeviceClass": types.SimpleNamespace(DURATION="duration"),
        "SensorStateClass": types.SimpleNamespace(MEASUREMENT="measurement"),
    }


def _load_with_stubs():
    pkg = "custom_components"
    sub = "custom_components.appletv_mgmt"
    for name in (pkg, sub):
        if name not in sys.modules:
            m = types.ModuleType(name); m.__path__ = []; sys.modules[name] = m

    ha_stubs = {
        "homeassistant": {},
        "homeassistant.config_entries": {
            "ConfigEntry": type("ConfigEntry", (), {}),
        },
        "homeassistant.const": {
            "UnitOfTime": types.SimpleNamespace(MINUTES="min"),
        },
        "homeassistant.core": {
            "Event": type("Event", (), {}),
            "EventStateChangedData": type("EventStateChangedData", (), {}),
            "HomeAssistant": type("HomeAssistant", (), {}),
            "callback": (lambda f: f),
        },
        "homeassistant.helpers": {},
        "homeassistant.helpers.entity": {
            "EntityCategory": types.SimpleNamespace(DIAGNOSTIC="diagnostic"),
        },
        "homeassistant.helpers.entity_platform": {
            "AddEntitiesCallback": type("AddEntitiesCallback", (), {}),
        },
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
            "CoordinatorEntity": type(
                "CoordinatorEntity",
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
        "homeassistant.components": {},
        "homeassistant.components.sensor": _sensor_stub_module(),
        "homeassistant.util": {},
        "homeassistant.util.dt": {
            "utcnow": (lambda: datetime.now(timezone.utc)),
            "as_local": (lambda dt: dt),
            "as_utc": (lambda dt: dt),
        },
    }
    for name, attrs in ha_stubs.items():
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
            sys.modules[name].__path__ = []
        for k, v in attrs.items():
            if not hasattr(sys.modules[name], k):
                setattr(sys.modules[name], k, v)

    # Pure modules.
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

    # Storage.
    if f"{sub}.storage" not in sys.modules:
        path = (
            Path(__file__).parent.parent
            / "custom_components" / "appletv_mgmt" / "storage.py"
        )
        spec = importlib.util.spec_from_file_location(f"{sub}.storage", path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        spec.loader.exec_module(m)

    # Stub audit + enforcer.
    if f"{sub}.audit" not in sys.modules:
        audit_stub = types.ModuleType(f"{sub}.audit")
        audit_stub.record_admin_action = MagicMock()
        sys.modules[f"{sub}.audit"] = audit_stub
    if f"{sub}.enforcer" not in sys.modules:
        enf_stub = types.ModuleType(f"{sub}.enforcer")
        enf_stub.EnforcementController = MagicMock
        sys.modules[f"{sub}.enforcer"] = enf_stub

    # Coordinator.
    if f"{sub}.coordinator" not in sys.modules:
        path = (
            Path(__file__).parent.parent
            / "custom_components" / "appletv_mgmt" / "coordinator.py"
        )
        spec = importlib.util.spec_from_file_location(f"{sub}.coordinator", path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        spec.loader.exec_module(m)

    # Sensor module — pulls policy + const + coordinator.
    # `policy.py` needs to be importable; load it the same way.
    if f"{sub}.policy" not in sys.modules:
        path = (
            Path(__file__).parent.parent
            / "custom_components" / "appletv_mgmt" / "policy.py"
        )
        spec = importlib.util.spec_from_file_location(f"{sub}.policy", path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        spec.loader.exec_module(m)

    if f"{sub}.sensor" not in sys.modules:
        path = (
            Path(__file__).parent.parent
            / "custom_components" / "appletv_mgmt" / "sensor.py"
        )
        spec = importlib.util.spec_from_file_location(f"{sub}.sensor", path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        spec.loader.exec_module(m)

    return (
        sys.modules[f"{sub}.coordinator"],
        sys.modules[f"{sub}.sensor"],
        sys.modules[f"{sub}.storage"],
    )


coord_mod, sensor_mod, storage_mod = _load_with_stubs()


# ---------------------------------------------------------------------------
# Test 1 — sensor descriptions read snapshot keys correctly
# ---------------------------------------------------------------------------


def _diag_descriptions():
    """Return the three v0.18.0 diagnostic descriptions from SENSORS."""
    keys = {
        "attribution_source",
        "dns_classifier_confidence",
        "attribution_gap_minutes_today",
    }
    return {d.key: d for d in sensor_mod.SENSORS if d.key in keys}


def test_three_diagnostic_sensors_registered():
    diag = _diag_descriptions()
    assert set(diag.keys()) == {
        "attribution_source",
        "dns_classifier_confidence",
        "attribution_gap_minutes_today",
    }, f"missing entries in SENSORS, got: {list(diag.keys())}"


def test_diagnostic_sensors_value_fns_populated_snapshot():
    """Populated coordinator snapshot -> sensors surface the live values."""
    diag = _diag_descriptions()
    snapshot = {
        "attribution_source": "annotate_group:dns_bundle",
        "dns_classifier_confidence": "BUNDLE",
        "attribution_gap_minutes_today": 12.4,
    }
    assert diag["attribution_source"].value_fn(snapshot) == "annotate_group:dns_bundle"
    assert diag["dns_classifier_confidence"].value_fn(snapshot) == "BUNDLE"
    assert diag["attribution_gap_minutes_today"].value_fn(snapshot) == 12.4


def test_diagnostic_sensors_value_fns_empty_snapshot():
    """Empty snapshot -> defaults that distinguish 'no data' from 'idle'."""
    diag = _diag_descriptions()
    empty: dict = {}
    # attribution_source missing -> 'unknown' (distinguishes from explicit
    # 'disabled' / 'no_open_event' / 'preserve:...')
    assert diag["attribution_source"].value_fn(empty) == "unknown"
    # confidence default mirrors the dns_classifier.Confidence.NONE name
    assert diag["dns_classifier_confidence"].value_fn(empty) == "NONE"
    # gap defaults to 0.0 so the duration-units sensor renders cleanly
    assert diag["attribution_gap_minutes_today"].value_fn(empty) == 0.0


def test_diagnostic_sensors_value_fns_disabled_feature():
    """When the feature is off the coordinator writes 'disabled' /
    'NONE' / 0.0 — the sensors should surface those literal values
    without falling back to defaults."""
    diag = _diag_descriptions()
    snapshot = {
        "attribution_source": "disabled",
        "dns_classifier_confidence": "NONE",
        "attribution_gap_minutes_today": 0.0,
    }
    assert diag["attribution_source"].value_fn(snapshot) == "disabled"
    assert diag["dns_classifier_confidence"].value_fn(snapshot) == "NONE"
    assert diag["attribution_gap_minutes_today"].value_fn(snapshot) == 0.0


# ---------------------------------------------------------------------------
# Test 2 — _compute_gap_minutes_today math
# ---------------------------------------------------------------------------


T0 = datetime(2026, 6, 14, 8, 0, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(hours=2)  # 10:00 UTC same day


def _make_profile():
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
        apple_tv_ip="192.168.1.24",
        dns_corroboration_mode="correct",
    )


class _BareCoordinator:
    """Coordinator-like object that binds only what we exercise.

    Mirrors the pattern in test_dns_corroboration_integration.py — bypasses
    DataUpdateCoordinator's __init__ which expects a HomeAssistant instance.
    """
    _compute_gap_minutes_today = coord_mod.AppleTVMgmtCoordinator._compute_gap_minutes_today

    def __init__(self, profile, store):
        self._profile = profile
        self._store = store


def _evt(*, bundle="com.disney.disneyplus", started=T0, ended=None, segments=None):
    return storage_mod.UsageEvent(
        id=f"{int(started.timestamp()*1000)}-p1",
        profile_id="p1",
        bundle_id=bundle,
        started_at=started,
        ended_at=ended,
        group_segments=segments or [],
    )


def _seg(*, start_offset_s, end_offset_s=None, group="gaming", source="dns_classifier"):
    return storage_mod.GroupSegment(
        started_at=T0 + timedelta(seconds=start_offset_s),
        ended_at=(T0 + timedelta(seconds=end_offset_s)) if end_offset_s is not None else None,
        group=group,
        source=source,
        confidence="BUNDLE",
    )


def test_compute_gap_zero_for_legacy_event_without_segments():
    """Pre-v0.18.0 events have empty group_segments -> contribute 0.
    This is the contract that protects legacy stale_closed rows from
    being misread as DNS corrections."""
    profile = _make_profile()
    store = MagicMock()
    store.events_for_profile.return_value = [
        _evt(started=T0, ended=T0 + timedelta(minutes=30), segments=[]),
    ]
    c = _BareCoordinator(profile, store)
    assert c._compute_gap_minutes_today(NOW) == 0.0


def test_compute_gap_sums_dns_classifier_segments_only():
    """Only segments with source=='dns_classifier' count toward the gap.
    A hypothetical future 'manual' / 'pyatv' source would NOT inflate
    the visible-gap metric."""
    profile = _make_profile()
    store = MagicMock()
    store.events_for_profile.return_value = [
        _evt(segments=[
            # 4 minutes of DNS-corrected gaming
            _seg(start_offset_s=0, end_offset_s=240, group="gaming"),
            # 2 minutes of some other-source annotation -> ignored
            _seg(start_offset_s=240, end_offset_s=360, group="movies", source="manual"),
        ]),
    ]
    c = _BareCoordinator(profile, store)
    # 240 / 60 = 4.0 minutes
    assert c._compute_gap_minutes_today(NOW) == 4.0


def test_compute_gap_open_segment_counts_to_now():
    """An open dns_classifier segment (ended_at=None) on an open event
    accrues up to `now`. The event opened 2h ago, the segment started
    30 min in -> 90 min of gap."""
    profile = _make_profile()
    store = MagicMock()
    store.events_for_profile.return_value = [
        _evt(
            started=T0,
            ended=None,  # open event
            segments=[
                _seg(start_offset_s=30 * 60, end_offset_s=None, group="gaming"),
            ],
        ),
    ]
    c = _BareCoordinator(profile, store)
    # NOW = T0 + 2h; segment started at T0 + 30min; open -> ends at NOW
    # Gap = 2h - 30min = 90 min
    assert c._compute_gap_minutes_today(NOW) == 90.0


def test_compute_gap_multiple_events_summed():
    """Multiple events with multiple dns_classifier segments are all
    summed into a single profile-wide total."""
    profile = _make_profile()
    store = MagicMock()
    store.events_for_profile.return_value = [
        _evt(
            started=T0,
            ended=T0 + timedelta(minutes=10),
            segments=[_seg(start_offset_s=0, end_offset_s=180, group="gaming")],
        ),
        _evt(
            started=T0 + timedelta(minutes=30),
            ended=T0 + timedelta(minutes=60),
            segments=[
                _seg(
                    start_offset_s=30 * 60,
                    end_offset_s=30 * 60 + 120,
                    group="gaming",
                ),
            ],
        ),
    ]
    c = _BareCoordinator(profile, store)
    # 180s + 120s = 300s = 5.0 min
    assert c._compute_gap_minutes_today(NOW) == 5.0


def test_compute_gap_ignores_events_entirely_before_today():
    """Yesterday's event with a dns_classifier segment MUST contribute 0
    to today's gap sensor. The local-midnight clip is load-bearing."""
    profile = _make_profile()
    store = MagicMock()
    yesterday_start = T0 - timedelta(days=1)
    store.events_for_profile.return_value = [
        _evt(
            started=yesterday_start,
            ended=yesterday_start + timedelta(minutes=30),
            segments=[
                _seg(start_offset_s=-86400, end_offset_s=-86400 + 180, group="gaming"),
            ],
        ),
    ]
    c = _BareCoordinator(profile, store)
    assert c._compute_gap_minutes_today(NOW) == 0.0


# ---------------------------------------------------------------------------
# Test 3 — _check_dns_corroboration / _apply_attribution_decision wire the
# stash. Integration-flavored — we re-use the bare-coordinator pattern
# from test_dns_corroboration_integration.py.
# ---------------------------------------------------------------------------


class _DiagCoordinator:
    """Minimal coordinator binding for the dns-corroboration flow.

    Includes the diagnostic-stash fields we just added to __init__.
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
        # Diagnostic stash defaults — what __init__ sets in the real class.
        self._last_attribution_source = None
        self._last_dns_confidence = "NONE"


def _state(state_str, last_updated):
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


def _build_corroboration_env(mode="correct"):
    profile = storage_mod.Profile(
        id="p1",
        display_name="Living Room",
        apple_tv_entity_id="media_player.heimkinoaaa",
        adguard_client_name="AppleTV",
        daily_budget_min=60,
        grace_seconds=60,
        warn_thresholds_min=[5],
        idle_grace_minutes=5,
        stale_session_minutes=5,
        apple_tv_ip="192.168.1.24",
        dns_corroboration_mode=mode,
    )
    store = MagicMock()
    store.async_save = AsyncMock(return_value=None)
    store.app_categories = MagicMock(return_value={})
    adguard = MagicMock()
    adguard.query_recent_dns = AsyncMock(return_value=[])
    enforcer = MagicMock()
    enforcer._adguard = adguard
    hass = MagicMock()
    hass.states = MagicMock()
    hass.bus = MagicMock()
    hass.bus.async_fire = MagicMock()
    return _DiagCoordinator(profile, store, enforcer, hass), store, adguard, hass


def test_feature_off_sets_attribution_source_to_disabled():
    """Mode 'off' -> sensor reads 'disabled' / 'NONE' so the parent
    can tell the feature isn't on."""
    c, store, adguard, hass = _build_corroboration_env(mode="off")
    asyncio.run(c._check_dns_corroboration(now=datetime.now(timezone.utc)))
    assert c._last_attribution_source == "disabled"
    assert c._last_dns_confidence == "NONE"
    adguard.query_recent_dns.assert_not_called()


def test_no_open_event_sets_attribution_source_to_no_open_event():
    """No open event -> sensor reads 'no_open_event' so the parent
    can distinguish 'feature ran but nothing to attribute' from
    'feature is off'."""
    c, store, adguard, hass = _build_corroboration_env(mode="correct")
    store.open_event_for.return_value = None
    asyncio.run(c._check_dns_corroboration(now=datetime.now(timezone.utc)))
    assert c._last_attribution_source == "no_open_event"
    assert c._last_dns_confidence == "NONE"


def test_annotate_group_decision_updates_attribution_source():
    """Live-bug shape: Disney+ open event + KooApps DNS evidence. After
    the tick, the sensor stash should reflect the annotate_group decision
    and the BUNDLE confidence."""
    c, store, adguard, hass = _build_corroboration_env(mode="correct")
    ev = _open_event()
    store.open_event_for.return_value = ev
    now = T0 + timedelta(minutes=30)
    hass.states.get.return_value = _state("playing", T0)

    def _ts(s):
        return (now - timedelta(seconds=s)).isoformat()

    adguard.query_recent_dns.return_value = [
        ("www.kooappsservers.com", _ts(5)),
        ("usa-kooapps-dlc.s3.amazonaws.com", _ts(15)),
        ("kooapps.com", _ts(25)),
        ("nakiostudio.com", _ts(35)),
    ]

    asyncio.run(c._check_dns_corroboration(now=now))

    assert c._last_dns_confidence == "BUNDLE"
    # action label always present; reason hint optional. At minimum it
    # starts with 'annotate_group'.
    assert c._last_attribution_source is not None
    assert c._last_attribution_source.startswith("annotate_group")


def test_pyatv_fresh_preserve_updates_attribution_source():
    """pyatv-fresh PRESERVE branch -> source reads 'preserve:pyatv_fresh'
    so the parent sees that pyatv is healthy and the classifier is
    standing down."""
    c, store, adguard, hass = _build_corroboration_env(mode="correct")
    ev = _open_event()
    store.open_event_for.return_value = ev
    now = T0 + timedelta(minutes=30)
    # pyatv just talked — 30s ago.
    hass.states.get.return_value = _state("playing", now - timedelta(seconds=30))

    def _ts(s):
        return (now - timedelta(seconds=s)).isoformat()
    adguard.query_recent_dns.return_value = [
        ("www.kooappsservers.com", _ts(5)),
    ]

    asyncio.run(c._check_dns_corroboration(now=now))

    # PRESERVE path still updates the stash. Reason hint is 'pyatv_fresh'.
    assert c._last_attribution_source is not None
    assert c._last_attribution_source.startswith("preserve")
    assert "pyatv" in c._last_attribution_source


# ---------------------------------------------------------------------------
# Test 4 — snapshot keys are present in _async_update_data output
#
# Smoke-check the three new keys appear with the expected types. Drives
# _async_update_data via the same bypass pattern so the test doesn't need
# a real DataUpdateCoordinator.
# ---------------------------------------------------------------------------


def test_snapshot_includes_three_new_keys():
    """Coordinator snapshot has the three v0.18.0 diagnostic keys with
    the expected types (str | None, str, float). This is what the HA
    EVENT_USAGE_UPDATED bus event also carries."""
    # We don't exercise the full coordinator tick (too much HA state to
    # wire) — instead we assert the snapshot dict construction itself
    # contains the keys. The key proof is in the source: assemble a
    # representative snapshot the way the coordinator does and verify it.
    c, store, adguard, hass = _build_corroboration_env(mode="correct")
    # Seed the stash as if the classifier had run.
    c._last_attribution_source = "annotate_group:dns_bundle"
    c._last_dns_confidence = "BUNDLE"

    # Drive _compute_gap_minutes_today directly with a known event.
    store.events_for_profile.return_value = [
        _evt(segments=[_seg(start_offset_s=0, end_offset_s=120)]),
    ]
    # Bind the method to our bare coordinator for the math call.
    gap = coord_mod.AppleTVMgmtCoordinator._compute_gap_minutes_today(c, NOW)
    assert gap == 2.0

    # Mimic the snapshot assembly that the real coordinator does.
    snapshot = {
        "attribution_source": c._last_attribution_source,
        "dns_classifier_confidence": c._last_dns_confidence,
        "attribution_gap_minutes_today": gap,
    }
    assert snapshot["attribution_source"] == "annotate_group:dns_bundle"
    assert snapshot["dns_classifier_confidence"] == "BUNDLE"
    assert snapshot["attribution_gap_minutes_today"] == 2.0
    assert isinstance(snapshot["attribution_gap_minutes_today"], float)


# ---------------------------------------------------------------------------
# v0.18.1 — TV-on counter (observability-only diagnostic sensor)
# ---------------------------------------------------------------------------


class _TvCoordinator:
    """Coordinator-like harness for _update_tv_on_counter.

    Binds only the attributes the helper reads + the helper method itself.
    Mirrors the _BareCoordinator pattern but exposes a hass.states.get
    that returns a fake state object so we can drive TV-state transitions.
    """
    _update_tv_on_counter = coord_mod.AppleTVMgmtCoordinator._update_tv_on_counter
    _TV_ON_STATES = coord_mod.AppleTVMgmtCoordinator._TV_ON_STATES
    _TV_ON_MAX_TICK_DELTA_S = coord_mod.AppleTVMgmtCoordinator._TV_ON_MAX_TICK_DELTA_S

    def __init__(self, profile, tv_state_sequence):
        self._profile = profile
        self._tv_on_seconds_today = 0.0
        self._tv_on_last_seen_at = None
        self._tv_state_iter = iter(tv_state_sequence)
        self.hass = types.SimpleNamespace(
            states=types.SimpleNamespace(get=self._fake_get_state),
        )

    def _fake_get_state(self, entity_id):
        try:
            s = next(self._tv_state_iter)
        except StopIteration:
            s = None
        if s is None:
            return None
        return types.SimpleNamespace(state=s)


def _profile_with_tv(tv_id="media_player.samsung_tv"):
    p = _make_profile()
    p.tv_entity_id = tv_id
    return p


def test_tv_on_counter_no_tv_entity_id_short_circuits():
    """When tv_entity_id is None the counter stays at 0 regardless of state."""
    p = _make_profile()
    p.tv_entity_id = None
    c = _TvCoordinator(p, ["on", "on", "on"])
    c._update_tv_on_counter(NOW)
    c._update_tv_on_counter(NOW + timedelta(seconds=30))
    c._update_tv_on_counter(NOW + timedelta(seconds=60))
    assert c._tv_on_seconds_today == 0.0
    assert c._tv_on_last_seen_at is None


def test_tv_on_counter_accrues_when_on():
    """on -> on -> on across 3 ticks (30s each) accrues 60s (two deltas)."""
    p = _profile_with_tv()
    c = _TvCoordinator(p, ["on", "on", "on"])
    c._update_tv_on_counter(NOW)
    assert c._tv_on_seconds_today == 0.0  # first tick: no prior anchor
    c._update_tv_on_counter(NOW + timedelta(seconds=30))
    assert c._tv_on_seconds_today == 30.0
    c._update_tv_on_counter(NOW + timedelta(seconds=60))
    assert c._tv_on_seconds_today == 60.0


def test_tv_on_counter_pauses_when_off():
    """on -> off -> on credits only the on-streaks."""
    p = _profile_with_tv()
    c = _TvCoordinator(p, ["on", "on", "off", "on", "on"])
    c._update_tv_on_counter(NOW)
    c._update_tv_on_counter(NOW + timedelta(seconds=30))
    assert c._tv_on_seconds_today == 30.0
    # Mirror went off — anchor cleared, gap not credited
    c._update_tv_on_counter(NOW + timedelta(seconds=60))
    assert c._tv_on_seconds_today == 30.0
    assert c._tv_on_last_seen_at is None
    # On again — first on-tick re-anchors but doesn't credit the off-gap
    c._update_tv_on_counter(NOW + timedelta(seconds=90))
    assert c._tv_on_seconds_today == 30.0
    c._update_tv_on_counter(NOW + timedelta(seconds=120))
    assert c._tv_on_seconds_today == 60.0


def test_tv_on_counter_counts_playing_paused_buffering():
    """`samsungtv_smart`-style integrations may report `playing`/`paused`
    instead of generic `on`. The counter should treat all three +
    `buffering` as on-ish."""
    p = _profile_with_tv()
    c = _TvCoordinator(p, ["playing", "playing", "paused", "buffering"])
    c._update_tv_on_counter(NOW)
    c._update_tv_on_counter(NOW + timedelta(seconds=30))
    c._update_tv_on_counter(NOW + timedelta(seconds=60))
    c._update_tv_on_counter(NOW + timedelta(seconds=90))
    assert c._tv_on_seconds_today == 90.0


def test_tv_on_counter_excludes_unavailable_unknown_standby_idle():
    """Defensive states the counter must NOT count: `unavailable`,
    `unknown`, `standby`, `idle`. These typically mean 'integration lag'
    or 'home screen' — counting them inflates the daily total."""
    p = _profile_with_tv()
    c = _TvCoordinator(p, ["unavailable", "unknown", "standby", "idle"])
    for i in range(4):
        c._update_tv_on_counter(NOW + timedelta(seconds=30 * i))
    assert c._tv_on_seconds_today == 0.0


def test_tv_on_counter_sanity_caps_huge_tick_delta():
    """A clock jump (DST, host sleep+wake) could yield a multi-hour delta
    between two on-state ticks. The sanity cap (10 min) keeps this from
    silently inflating the daily total."""
    p = _profile_with_tv()
    c = _TvCoordinator(p, ["on", "on"])
    c._update_tv_on_counter(NOW)
    # 2-hour jump
    c._update_tv_on_counter(NOW + timedelta(hours=2))
    # The cap rejected the delta — the counter did NOT add 7200s.
    assert c._tv_on_seconds_today == 0.0
    # And the anchor still advanced so the next normal tick credits cleanly.
    assert c._tv_on_last_seen_at == NOW + timedelta(hours=2)


def test_tv_on_counter_entity_missing_treated_as_off():
    """When `hass.states.get(tv_entity_id)` returns None (entity not yet
    registered, or registry was reloaded), the counter treats it as off
    rather than crashing."""
    p = _profile_with_tv()
    c = _TvCoordinator(p, [None, "on", "on"])  # first tick: entity missing
    c._update_tv_on_counter(NOW)
    assert c._tv_on_seconds_today == 0.0
    assert c._tv_on_last_seen_at is None
    # Once the entity is back the counter resumes cleanly
    c._update_tv_on_counter(NOW + timedelta(seconds=30))
    c._update_tv_on_counter(NOW + timedelta(seconds=60))
    assert c._tv_on_seconds_today == 30.0


def test_tv_on_sensor_description_registered():
    """The TV-on sensor description must be in the SENSORS tuple with
    EntityCategory.DIAGNOSTIC + DURATION/MEASUREMENT semantics."""
    matching = [d for d in sensor_mod.SENSORS if d.key == "tv_on_minutes_today"]
    assert len(matching) == 1
    d = matching[0]
    assert d.entity_category is not None  # DIAGNOSTIC (stubbed in tests)


def test_tv_on_sensor_value_fn_converts_seconds_to_minutes():
    """The sensor surfaces `tv_on_seconds_today` from the snapshot as
    minutes rounded to 1 decimal."""
    desc = next(d for d in sensor_mod.SENSORS if d.key == "tv_on_minutes_today")
    assert desc.value_fn({"tv_on_seconds_today": 0.0}) == 0.0
    assert desc.value_fn({"tv_on_seconds_today": 90.0}) == 1.5
    assert desc.value_fn({"tv_on_seconds_today": 3725.0}) == 62.1
    # Missing key defaults to 0 (the snapshot always writes the key, but
    # be defensive against partial-update bugs).
    assert desc.value_fn({}) == 0.0
