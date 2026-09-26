"""Tests for v0.18.0 GroupSegment + UsageEvent.group_seconds_breakdown.

Covers the live-bug shape: a Disney+ event whose middle is reclassified to
gaming via DNS evidence should yield ~9 min movies + ~117 min gaming when
asked for its group breakdown, while a legacy event (no segments) maps
its entire duration to the bundle's curated group (bit-identical to today).
"""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _load_storage():
    """Load storage.py with stubbed HA dependencies — same pattern as the
    existing tests/conftest.py uses."""
    pkg = "custom_components"
    if pkg not in sys.modules:
        m = types.ModuleType(pkg); m.__path__ = []; sys.modules[pkg] = m
    sub = "custom_components.appletv_mgmt"
    if sub not in sys.modules:
        m = types.ModuleType(sub); m.__path__ = []; sys.modules[sub] = m

    # Stub the HA modules storage.py imports at top level. ONLY stub what's
    # missing — do NOT overwrite anything other tests rely on (especially
    # `homeassistant.util.dt.utcnow`, which existing tests use the real
    # `dt_util.utcnow` for time math).
    for name in ("homeassistant", "homeassistant.core",
                 "homeassistant.helpers", "homeassistant.helpers.storage",
                 "homeassistant.util", "homeassistant.util.dt"):
        if name not in sys.modules:
            stub = types.ModuleType(name)
            stub.__path__ = []
            sys.modules[name] = stub
    # Add the symbols storage.py reads — only if not already provided.
    core = sys.modules["homeassistant.core"]
    if not hasattr(core, "HomeAssistant"):
        core.HomeAssistant = type("HomeAssistant", (), {})
    storage_mod = sys.modules["homeassistant.helpers.storage"]
    if not hasattr(storage_mod, "Store"):
        storage_mod.Store = type("Store", (), {})
    # Critical: DO NOT override dt_util.utcnow here — would pollute other
    # tests. Every test in this file passes `now=` explicitly to
    # `group_seconds_breakdown` and `duration_seconds`.

    # const module
    const_path = (
        Path(__file__).parent.parent
        / "custom_components" / "appletv_mgmt" / "const.py"
    )
    if "custom_components.appletv_mgmt.const" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "custom_components.appletv_mgmt.const", const_path)
        const = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = const
        spec.loader.exec_module(const)

    path = (
        Path(__file__).parent.parent
        / "custom_components" / "appletv_mgmt" / "storage.py"
    )
    spec = importlib.util.spec_from_file_location(
        f"{sub}.storage", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


storage = _load_storage()
UsageEvent = storage.UsageEvent
GroupSegment = storage.GroupSegment


T0 = datetime(2026, 6, 14, 6, 27, 0, tzinfo=timezone.utc)


def _ev(started=T0, ended=None, bundle="com.disney.disneyplus", segs=None):
    return UsageEvent(
        id=f"{int(started.timestamp()*1000)}-p1",
        profile_id="p1",
        bundle_id=bundle,
        started_at=started,
        ended_at=ended,
        group_segments=segs or [],
    )


# ============================================================================
# Legacy compatibility — no segments == today's behavior, bit-identical
# ============================================================================


def test_legacy_event_with_no_segments_maps_to_curated_group():
    """Existing events on disk have no `group_segments` field. They must
    behave identically to pre-v0.18.0 — full duration -> curated group."""
    ended = T0 + timedelta(minutes=60)
    ev = _ev(ended=ended)
    breakdown = ev.group_seconds_breakdown("movies")
    assert breakdown == {"movies": 3600}


def test_open_event_with_no_segments_uses_now():
    """Open events count up to `now` — preserved."""
    now = T0 + timedelta(minutes=30)
    ev = _ev()  # ended_at=None
    breakdown = ev.group_seconds_breakdown("movies", now=now)
    assert breakdown == {"movies": 1800}


def test_zero_duration_event_is_empty_or_zero():
    """A defensive case: started_at == ended_at -> 0s."""
    ev = _ev(ended=T0)
    breakdown = ev.group_seconds_breakdown("movies")
    # The function returns {curated_group: 0} for the empty case — fine for
    # downstream sum() callers.
    assert breakdown == {"movies": 0}


# ============================================================================
# Single-segment corrections
# ============================================================================


def test_full_event_covered_by_one_segment_attributes_entirely_to_segment():
    """If a single segment spans the entire event, the breakdown reflects
    only the segment's group — no leakage to curated."""
    ended = T0 + timedelta(minutes=30)
    seg = GroupSegment(
        started_at=T0,
        ended_at=ended,
        group="gaming",
    )
    ev = _ev(ended=ended, segs=[seg])
    breakdown = ev.group_seconds_breakdown("movies")
    assert breakdown == {"gaming": 1800}


def test_segment_starts_after_event_opens_pre_segment_time_goes_to_curated():
    """Real shape of the live bug: event opens as Disney+, DNS classifier
    later observes gaming, appends a segment. Pre-segment time stays
    attributed to movies; segment time goes to gaming."""
    ended = T0 + timedelta(minutes=30)
    seg = GroupSegment(
        started_at=T0 + timedelta(minutes=5),
        ended_at=ended,
        group="gaming",
    )
    ev = _ev(ended=ended, segs=[seg])
    breakdown = ev.group_seconds_breakdown("movies")
    assert breakdown == {"movies": 300, "gaming": 1500}


def test_open_segment_inside_open_event_extends_to_now():
    """Both event and segment open -> both extend to `now`."""
    now = T0 + timedelta(minutes=30)
    seg = GroupSegment(
        started_at=T0 + timedelta(minutes=5),
        ended_at=None,  # open segment
        group="gaming",
    )
    ev = _ev(segs=[seg])  # open event
    breakdown = ev.group_seconds_breakdown("movies", now=now)
    assert breakdown == {"movies": 300, "gaming": 1500}


# ============================================================================
# Multi-segment corrections
# ============================================================================


def test_two_segments_with_gap_attributes_gap_to_curated():
    """Two corrections with a gap between them: gap belongs to curated."""
    ended = T0 + timedelta(minutes=60)
    segs = [
        GroupSegment(
            started_at=T0 + timedelta(minutes=10),
            ended_at=T0 + timedelta(minutes=20),
            group="gaming",
        ),
        GroupSegment(
            started_at=T0 + timedelta(minutes=40),
            ended_at=T0 + timedelta(minutes=50),
            group="gaming",
        ),
    ]
    ev = _ev(ended=ended, segs=segs)
    breakdown = ev.group_seconds_breakdown("movies")
    # 0-10 movies (10min) + 10-20 gaming (10min) + 20-40 movies (20min) +
    # 40-50 gaming (10min) + 50-60 movies (10min) = 40 min movies + 20 gaming
    assert breakdown == {"movies": 2400, "gaming": 1200}


def test_adjacent_segments_of_different_groups_no_curated_leak():
    """Segments touching exactly — no leftover, no curated allocation."""
    ended = T0 + timedelta(minutes=30)
    segs = [
        GroupSegment(
            started_at=T0,
            ended_at=T0 + timedelta(minutes=10),
            group="gaming",
        ),
        GroupSegment(
            started_at=T0 + timedelta(minutes=10),
            ended_at=ended,
            group="tv_shows",
        ),
    ]
    ev = _ev(ended=ended, segs=segs)
    breakdown = ev.group_seconds_breakdown("movies")
    assert breakdown == {"gaming": 600, "tv_shows": 1200}


# ============================================================================
# Live-bug replay — today's session shape
# ============================================================================


def test_live_bug_replay_disney_event_with_gaming_segment_breakdown():
    """The exact shape of today's misattribution, properly corrected:
    Event opens 06:27 as Disney+. At 06:36 (9 min later) DNS classifier
    observes gaming, opens a gaming segment that runs to 08:33 (event end).
    Breakdown: ~9 min movies + ~117 min gaming, NOT 126.7 min of movies.
    """
    started = T0  # 06:27
    ended = T0 + timedelta(minutes=126, seconds=44)  # 08:33:44
    # Disney+ for the first 9m12s of the session, then gaming.
    gaming_seg = GroupSegment(
        started_at=T0 + timedelta(minutes=9, seconds=12),
        ended_at=ended,
        group="gaming",
        source="dns_classifier",
        confidence="BUNDLE",
    )
    ev = _ev(ended=ended, segs=[gaming_seg])
    breakdown = ev.group_seconds_breakdown("movies")

    # Expected: ~9 min movies, ~117 min gaming (total ~126.7 min)
    total_secs = ev.duration_seconds()
    assert total_secs == 126 * 60 + 44  # full event preserved
    assert sum(breakdown.values()) == total_secs  # nothing lost

    movies_min = breakdown.get("movies", 0) / 60
    gaming_min = breakdown.get("gaming", 0) / 60
    assert 8.5 < movies_min < 10, f"movies expected ~9 min, got {movies_min:.1f}"
    assert 115 < gaming_min < 119, f"gaming expected ~117 min, got {gaming_min:.1f}"


# ============================================================================
# Serialization round-trip
# ============================================================================


def test_serialization_round_trip_preserves_segments():
    """to_dict / from_dict must round-trip without loss."""
    seg = GroupSegment(
        started_at=T0,
        ended_at=T0 + timedelta(minutes=5),
        group="gaming",
        source="dns_classifier",
        confidence="BUNDLE",
    )
    ev = _ev(ended=T0 + timedelta(minutes=10), segs=[seg])
    d = ev.to_dict()
    assert "group_segments" in d
    ev2 = UsageEvent.from_dict(d)
    assert len(ev2.group_segments) == 1
    s = ev2.group_segments[0]
    assert s.started_at == T0
    assert s.ended_at == T0 + timedelta(minutes=5)
    assert s.group == "gaming"
    assert s.source == "dns_classifier"
    assert s.confidence == "BUNDLE"


def test_serialization_omits_empty_segments_for_legacy_compat():
    """Events with no segments must NOT have `group_segments` in their
    serialized form — keeps storage bit-identical for legacy events."""
    ev = _ev(ended=T0 + timedelta(minutes=10))
    d = ev.to_dict()
    assert "group_segments" not in d


def test_from_dict_handles_missing_segments_key():
    """Loading a pre-v0.18.0 event (no `group_segments` key) must yield
    an empty segments list — clean migration."""
    legacy = {
        "id": "evt1",
        "profile_id": "p1",
        "bundle_id": "com.disney.disneyplus",
        "started_at": T0.isoformat(),
        "ended_at": (T0 + timedelta(minutes=10)).isoformat(),
    }
    ev = UsageEvent.from_dict(legacy)
    assert ev.group_segments == []
