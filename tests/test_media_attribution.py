"""Unit tests for the pure attribution helper (v0.12.1).

Extracted from the coordinator so the rule can be exercised without
HA stubs. The 05-23 bug — `state=idle` + `app_id=None` opening a 19-hour
phantom event — is the smoke test below (`test_idle_without_app_does_not_attribute`).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from custom_components.appletv_mgmt.media_attribution import (  # noqa: E402
    UNKNOWN_APP_BUNDLE_ID,
    resolve_effective_bundle_id,
)


NOW = datetime(2026, 5, 23, 19, 0, 0, tzinfo=timezone.utc)


def _call(**overrides):
    """Default kwargs that pass through unchanged unless overridden."""
    kwargs = dict(
        media_state=None,
        app_id=None,
        now=NOW,
        last_known_bundle_id=None,
        last_known_seen_at=None,
        idle_grace_minutes=5,
    )
    kwargs.update(overrides)
    return resolve_effective_bundle_id(**kwargs)


# ---------- inactive states ----------


@pytest.mark.parametrize("state", ["off", "standby", "unavailable", "unknown", None])
def test_inactive_states_attribute_nothing_and_clear_last_known(state):
    r = _call(
        media_state=state,
        app_id="com.disney.disneyplus",
        last_known_bundle_id="com.disney.disneyplus",
        last_known_seen_at=NOW - timedelta(minutes=1),
    )
    assert r.bundle_id is None
    assert r.last_known_bundle_id is None
    assert r.last_known_seen_at is None


# ---------- active + app_id reported ----------


@pytest.mark.parametrize("state", ["playing", "paused", "buffering", "on", "idle"])
def test_active_with_app_id_returns_it_and_refreshes_last_known(state):
    r = _call(media_state=state, app_id="com.netflix.Netflix")
    assert r.bundle_id == "com.netflix.Netflix"
    assert r.last_known_bundle_id == "com.netflix.Netflix"
    assert r.last_known_seen_at == NOW


# ---------- active + no app_id, last-known within grace ----------


def test_active_no_app_within_grace_uses_last_known():
    """Brief pyatv reconnect or pause inside the same app — keep counting."""
    r = _call(
        media_state="playing",
        app_id=None,
        last_known_bundle_id="com.netflix.Netflix",
        last_known_seen_at=NOW - timedelta(minutes=2),
        idle_grace_minutes=5,
    )
    assert r.bundle_id == "com.netflix.Netflix"
    assert r.last_known_bundle_id == "com.netflix.Netflix"
    # Last-known timestamp should NOT advance — it's a memory, not a refresh.
    assert r.last_known_seen_at == NOW - timedelta(minutes=2)


def test_idle_no_app_within_grace_keeps_last_known():
    """A 30-second 'idle' beat inside a movie shouldn't drop attribution."""
    r = _call(
        media_state="idle",
        app_id=None,
        last_known_bundle_id="com.disney.disneyplus",
        last_known_seen_at=NOW - timedelta(seconds=30),
        idle_grace_minutes=5,
    )
    assert r.bundle_id == "com.disney.disneyplus"


# ---------- 05-23 phantom-idle regression ----------


def test_idle_without_app_or_recent_known_does_not_attribute():
    """THE 05-23 BUG: state=idle + app_id=None + no recent last_known →
    must NOT return 'unknown' (which would open a phantom event that
    runs until the next state change)."""
    r = _call(media_state="idle", app_id=None)
    assert r.bundle_id is None
    assert r.last_known_bundle_id is None
    assert r.last_known_seen_at is None


def test_on_without_app_or_recent_known_does_not_attribute():
    r = _call(media_state="on", app_id=None)
    assert r.bundle_id is None


def test_idle_with_stale_last_known_does_not_attribute():
    """Last-known is older than the grace window — treat as cold idle."""
    r = _call(
        media_state="idle",
        app_id=None,
        last_known_bundle_id="com.netflix.Netflix",
        last_known_seen_at=NOW - timedelta(minutes=60),
        idle_grace_minutes=5,
    )
    assert r.bundle_id is None
    # Stale last-known is NOT cleared here (that's the caller's concern);
    # we just refuse to use it.


# ---------- playing without app_id ----------


@pytest.mark.parametrize("state", ["playing", "paused", "buffering"])
def test_playing_states_without_app_id_attribute_unknown(state):
    """The game case: state IS playing audio/video, but pyatv can't read
    the app_id (most Apple TV games). Still count the time, as 'unknown'."""
    r = _call(media_state=state, app_id=None)
    assert r.bundle_id == UNKNOWN_APP_BUNDLE_ID
    # No last-known to persist for this path.
    assert r.last_known_bundle_id is None


def test_playing_with_stale_last_known_falls_back_to_unknown():
    r = _call(
        media_state="playing",
        app_id=None,
        last_known_bundle_id="com.netflix.Netflix",
        last_known_seen_at=NOW - timedelta(minutes=60),
        idle_grace_minutes=5,
    )
    assert r.bundle_id == UNKNOWN_APP_BUNDLE_ID


# ---------- idle_grace_minutes edge cases ----------


def test_zero_grace_means_immediate_loss_of_last_known():
    r = _call(
        media_state="idle",
        app_id=None,
        last_known_bundle_id="com.disney.disneyplus",
        last_known_seen_at=NOW - timedelta(seconds=1),
        idle_grace_minutes=0,
    )
    # Even 1 second is "after" the grace of 0 minutes → don't attribute.
    assert r.bundle_id is None


def test_negative_grace_treated_as_zero():
    r = _call(
        media_state="idle",
        app_id=None,
        last_known_bundle_id="com.disney.disneyplus",
        last_known_seen_at=NOW - timedelta(seconds=10),
        idle_grace_minutes=-1,
    )
    assert r.bundle_id is None
