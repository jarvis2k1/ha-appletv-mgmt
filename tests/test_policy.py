"""Unit tests for `policy.should_act` + `policy.voice_allowed`.

Per spec §6 — 13 cases exhaustively cover the 3 modes × 3 adult-mode
timings (None / future / past) plus 1 boundary + 1 unknown mode + 1
TZ-naive defense + 1 far-future. Plus extra cases for voice_allowed.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "appletv_mgmt"


def _load_policy():
    if "custom_components.appletv_mgmt.policy" in sys.modules:
        return sys.modules["custom_components.appletv_mgmt.policy"]
    spec = importlib.util.spec_from_file_location(
        "custom_components.appletv_mgmt.policy", PKG / "policy.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


policy = _load_policy()
NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


# ---------- should_act — 13 spec cases --------------------------------------


def test_case1_enforced_no_adult_mode_acts():
    d = policy.should_act(mode="enforced", adult_mode_until=None, now=NOW)
    assert d.kind == "ACT"
    assert d.reason is None


def test_case2_enforced_future_adult_bypasses():
    until = NOW + timedelta(hours=1)
    d = policy.should_act(mode="enforced", adult_mode_until=until, now=NOW)
    assert d.kind == "BYPASS"
    assert d.reason == "adult_mode"


def test_case3_enforced_past_adult_acts():
    until = NOW - timedelta(hours=1)
    d = policy.should_act(mode="enforced", adult_mode_until=until, now=NOW)
    assert d.kind == "ACT"


def test_case4_enforced_adult_exactly_now_acts():
    """Boundary: `until > now` is False at the exact moment of expiry."""
    d = policy.should_act(mode="enforced", adult_mode_until=NOW, now=NOW)
    assert d.kind == "ACT"


def test_case5_monitor_no_adult_observes():
    d = policy.should_act(mode="monitor_only", adult_mode_until=None, now=NOW)
    assert d.kind == "OBSERVE"
    assert d.reason == "monitor_only"


def test_case6_monitor_future_adult_bypasses_with_adult():
    until = NOW + timedelta(hours=1)
    d = policy.should_act(mode="monitor_only", adult_mode_until=until, now=NOW)
    assert d.kind == "BYPASS"
    assert d.reason == "adult_mode"


def test_case7_monitor_past_adult_observes():
    until = NOW - timedelta(hours=1)
    d = policy.should_act(mode="monitor_only", adult_mode_until=until, now=NOW)
    assert d.kind == "OBSERVE"
    assert d.reason == "monitor_only"


def test_case8_paused_no_adult_bypasses_paused():
    d = policy.should_act(mode="paused", adult_mode_until=None, now=NOW)
    assert d.kind == "BYPASS"
    assert d.reason == "paused"


def test_case9_paused_future_adult_bypasses_adult_wins_over_paused():
    """Adult mode is the highest-priority signal — wins even when paused."""
    until = NOW + timedelta(hours=1)
    d = policy.should_act(mode="paused", adult_mode_until=until, now=NOW)
    assert d.kind == "BYPASS"
    assert d.reason == "adult_mode"


def test_case10_paused_past_adult_bypasses_paused():
    until = NOW - timedelta(hours=1)
    d = policy.should_act(mode="paused", adult_mode_until=until, now=NOW)
    assert d.kind == "BYPASS"
    assert d.reason == "paused"


def test_case11_unknown_mode_defaults_to_act():
    """Unknown mode strings → defensive ACT default (don't disable enforcement)."""
    d = policy.should_act(mode="strict", adult_mode_until=None, now=NOW)
    assert d.kind == "ACT"
    assert d.reason is None


def test_case12_tz_naive_adult_until_raises():
    """Per spec §3.4: TZ-naive caller is a bug we want to surface, not paper over."""
    naive = datetime(2026, 5, 25, 13, 0, 0)  # no tzinfo
    with pytest.raises(TypeError):
        policy.should_act(mode="enforced", adult_mode_until=naive, now=NOW)


def test_case13_far_future_adult_until_bypasses():
    """+10 years in the future — no overflow on the comparison."""
    far = NOW + timedelta(days=365 * 10)
    d = policy.should_act(mode="enforced", adult_mode_until=far, now=NOW)
    assert d.kind == "BYPASS"
    assert d.reason == "adult_mode"


# ---------- voice_allowed gating --------------------------------------------


def test_voice_extension_always_speaks_under_bypass():
    """Per PO D8: extension grants always speak regardless of mode."""
    d = policy.ActDecision("BYPASS", "adult_mode")
    assert policy.voice_allowed(d, "extension_granted") is True
    d2 = policy.ActDecision("BYPASS", "paused")
    assert policy.voice_allowed(d2, "extension_granted") is True


def test_voice_extension_always_speaks_under_observe():
    d = policy.ActDecision("OBSERVE", "monitor_only")
    assert policy.voice_allowed(d, "extension_granted") is True


def test_voice_warning_silent_under_bypass():
    d = policy.ActDecision("BYPASS", "adult_mode")
    assert policy.voice_allowed(d, "warning") is False


def test_voice_warning_allowed_under_observe():
    """Caller still gates on profile.warn_in_monitor_mode."""
    d = policy.ActDecision("OBSERVE", "monitor_only")
    assert policy.voice_allowed(d, "warning") is True


def test_voice_enforcing_silent_under_observe():
    d = policy.ActDecision("OBSERVE", "monitor_only")
    assert policy.voice_allowed(d, "enforcing_verified") is False


def test_voice_all_allowed_under_act():
    d = policy.ActDecision("ACT", None)
    assert policy.voice_allowed(d, "warning") is True
    assert policy.voice_allowed(d, "enforcing_verified") is True
    assert policy.voice_allowed(d, "mode_changed") is True
    assert policy.voice_allowed(d, "adult_mode_on") is True
    assert policy.voice_allowed(d, "extension_granted") is True
