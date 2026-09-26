"""v0.19.2 — `someone_could_be_watching` gate for heads-up voices.

Live bug 2026-06-19: adult mode expired while the movies group was
exhausted, so the enforcer walked WARN→GRACE→countdown→ENFORCE and fired
voices on the dining-room speaker even though the Apple TV had been `idle`
and the Samsung TV `off` for an hour — nagging an empty room.

This pure predicate gates the warn (audit.py) + countdown (enforcer.py)
voices: fire only if EITHER the primary device is actively consuming OR the
TV is on. Fail-safe toward firing (a pyatv-stale `idle` while the TV is
genuinely on still warns); only suppress when BOTH say off.

conftest.py preloads media_attribution, so no HA stubs needed here.
"""
from __future__ import annotations

from custom_components.appletv_mgmt.media_attribution import (
    someone_could_be_watching,
)


# ---- apple_tv kind ---------------------------------------------------------

def test_apple_tv_playing_no_tv_configured_fires():
    assert someone_could_be_watching("apple_tv", "playing", None) is True


def test_apple_tv_paused_fires():
    assert someone_could_be_watching("apple_tv", "paused", None) is True


def test_apple_tv_buffering_fires():
    assert someone_could_be_watching("apple_tv", "buffering", None) is True


def test_apple_tv_idle_tv_off_suppressed():
    """Tonight's exact case: idle Apple TV + off TV → nobody watching."""
    assert someone_could_be_watching("apple_tv", "idle", "off") is False


def test_apple_tv_idle_no_tv_suppressed():
    assert someone_could_be_watching("apple_tv", "idle", None) is False


def test_apple_tv_off_tv_off_suppressed():
    assert someone_could_be_watching("apple_tv", "off", "off") is False


def test_apple_tv_idle_but_tv_on_fires_failsafe():
    """pyatv-stale `idle` while the TV is genuinely on → still warn
    (fail-safe toward firing; don't miss a legit heads-up)."""
    assert someone_could_be_watching("apple_tv", "idle", "on") is True


def test_apple_tv_off_but_tv_on_fires():
    assert someone_could_be_watching("apple_tv", "off", "on") is True


def test_apple_tv_missing_state_suppressed():
    """Entity missing (hass.states.get returned None) → treat as off."""
    assert someone_could_be_watching("apple_tv", None, None) is False


def test_apple_tv_home_screen_states_do_not_count_as_watching():
    """`on`/`standby`/`unavailable`/`unknown` are not active consumption."""
    for s in ("on", "standby", "unavailable", "unknown"):
        assert someone_could_be_watching("apple_tv", s, None) is False, s


# ---- xbox_presence kind ----------------------------------------------------

def test_xbox_home_fires():
    assert someone_could_be_watching("xbox_presence", "home", None) is True


def test_xbox_not_home_tv_off_suppressed():
    assert someone_could_be_watching("xbox_presence", "not_home", "off") is False


def test_xbox_not_home_but_tv_on_fires():
    assert someone_could_be_watching("xbox_presence", "not_home", "on") is True


def test_xbox_playing_string_does_not_count_only_home():
    """For xbox the primary entity is a device_tracker — only 'home' means
    present. A media-style 'playing' must NOT count."""
    assert someone_could_be_watching("xbox_presence", "playing", None) is False
