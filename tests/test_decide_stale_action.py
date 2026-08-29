"""Pure-function decision-table tests for `decide_stale_action`.

The function is HA-free by design (operates on primitives), so these
tests import `media_attribution` directly without any HA stubbing.

Decision rules under test (first match wins):
  1. stale_threshold_s <= 0                           -> NOOP
  2. has_open_event False                              -> NOOP
  3. apple_tv_state not in ACTIVE_MEDIA_STATES        -> NOOP
  4. last_updated age None or < threshold             -> NOOP
  5. open_event_age_s >= STALE_RUNAWAY_CEILING_S      -> CLOSE_RUNAWAY
  6. tv_entity_configured False                        -> CLOSE_AT_LAST_UPDATED
  7. tv_state in {'off','standby'}                    -> CLOSE_AT_LAST_UPDATED
  8. otherwise                                          -> KEEP_OPEN

These tests were written from the empirical evidence captured live on
2026-06-09 (see CHANGELOG + commit message): of 3 zero-duration sessions
observed, 2 were genuine under-counts (Samsung `on` throughout) and 1 was
a legitimate close (Samsung went `off`). A correct decision function must
classify all three cases correctly, which the decision table above does.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


# Load media_attribution as a top-level-like module (it has no HA imports,
# but we go through the package stub pattern for symmetry with the rest of
# the test suite). The pure function lives in this module.
def _load_media_attribution():
    pkg = "custom_components"
    if pkg not in sys.modules:
        stub = types.ModuleType(pkg)
        stub.__path__ = []
        sys.modules[pkg] = stub
    subpkg = "custom_components.appletv_mgmt"
    if subpkg not in sys.modules:
        stub = types.ModuleType(subpkg)
        stub.__path__ = []
        sys.modules[subpkg] = stub
    path = (
        Path(__file__).parent.parent
        / "custom_components"
        / "appletv_mgmt"
        / "media_attribution.py"
    )
    spec = importlib.util.spec_from_file_location(
        f"{subpkg}.media_attribution", path
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ma = _load_media_attribution()
StaleAction = ma.StaleAction
decide_stale_action = ma.decide_stale_action
STALE_RUNAWAY_CEILING_S = ma.STALE_RUNAWAY_CEILING_S


# ---- minimal fixture: a "stale-in-ACTIVE" baseline that subsequent
# tests perturb one field at a time. Every rule beyond #4 reaches its
# answer from this baseline by changing only the relevant input. ----
def _baseline(**overrides):
    base = dict(
        apple_tv_state="playing",
        apple_tv_last_updated_age_s=400.0,   # > 300s threshold
        stale_threshold_s=300,
        has_open_event=True,
        open_event_age_s=900.0,              # < 9000s runaway ceiling
        tv_entity_configured=True,
        tv_state="on",
    )
    base.update(overrides)
    return base


# ============================================================================
# NOOP — early-exit rules (1-4)
# ============================================================================


def test_noop_when_threshold_disabled():
    """Rule 1: stale_threshold_s=0 opts out entirely."""
    assert decide_stale_action(**_baseline(stale_threshold_s=0)) is StaleAction.NOOP


def test_noop_when_threshold_negative():
    """Rule 1: negative threshold is also opt-out (defensive)."""
    assert (
        decide_stale_action(**_baseline(stale_threshold_s=-1)) is StaleAction.NOOP
    )


def test_noop_when_no_open_event():
    """Rule 2: nothing open => nothing to close."""
    assert (
        decide_stale_action(**_baseline(has_open_event=False)) is StaleAction.NOOP
    )


def test_noop_when_apple_tv_state_inactive_off():
    """Rule 3: off state -> NOOP. _sync_open_event handles the transition."""
    assert (
        decide_stale_action(**_baseline(apple_tv_state="off")) is StaleAction.NOOP
    )


def test_noop_when_apple_tv_state_inactive_unavailable():
    """Rule 3: unavailable too. _sync_open_event handles it."""
    assert (
        decide_stale_action(**_baseline(apple_tv_state="unavailable"))
        is StaleAction.NOOP
    )


def test_noop_when_apple_tv_state_none():
    """Rule 3: None state (entity not registered) -> NOOP."""
    assert (
        decide_stale_action(**_baseline(apple_tv_state=None)) is StaleAction.NOOP
    )


def test_noop_when_last_updated_age_unknown():
    """Rule 4: no last_updated info -> NOOP (can't evaluate staleness)."""
    assert (
        decide_stale_action(**_baseline(apple_tv_last_updated_age_s=None))
        is StaleAction.NOOP
    )


def test_noop_when_age_below_threshold():
    """Rule 4: 299s < 300s -> still healthy, NOOP."""
    assert (
        decide_stale_action(
            **_baseline(apple_tv_last_updated_age_s=299.0)
        )
        is StaleAction.NOOP
    )


def test_threshold_boundary_exactly_at_threshold_closes():
    """Boundary: age == threshold counts as stale. Documents the closed
    interval `age >= threshold` so a future contributor doesn't flip it."""
    # TV definitively off (deterministic close) — but with exactly threshold
    # age. Tests the rule-4 boundary reaches the gate, not rule-7.
    assert (
        decide_stale_action(
            **_baseline(
                apple_tv_last_updated_age_s=300.0,
                tv_entity_configured=True,
                tv_state="off",
            )
        )
        is StaleAction.CLOSE_AT_LAST_UPDATED
    )


# ============================================================================
# Active states — all should pass rule 3 and reach the stale gate.
# ============================================================================


def test_active_state_paused_reaches_gate():
    """`paused` is ACTIVE — a stale-paused entity also runs the gate."""
    # paused + TV on -> KEEP_OPEN (under-count protection extends to paused)
    assert (
        decide_stale_action(**_baseline(apple_tv_state="paused"))
        is StaleAction.KEEP_OPEN
    )


def test_active_state_buffering_reaches_gate():
    """`buffering` is ACTIVE — also gated."""
    assert (
        decide_stale_action(**_baseline(apple_tv_state="buffering"))
        is StaleAction.KEEP_OPEN
    )


def test_active_state_idle_reaches_gate():
    """`idle` is ACTIVE — gates, doesn't NOOP at rule 3."""
    assert (
        decide_stale_action(**_baseline(apple_tv_state="idle"))
        is StaleAction.KEEP_OPEN
    )


# ============================================================================
# CLOSE_AT_LAST_UPDATED — preserves v0.17.1 contract
# ============================================================================


def test_close_at_last_updated_when_tv_off():
    """Rule 7: Samsung definitively off => overnight-Disney path."""
    assert (
        decide_stale_action(**_baseline(tv_state="off"))
        is StaleAction.CLOSE_AT_LAST_UPDATED
    )


def test_close_at_last_updated_when_tv_standby():
    """Rule 7: 'standby' is also definitively off (some Samsung firmwares)."""
    assert (
        decide_stale_action(**_baseline(tv_state="standby"))
        is StaleAction.CLOSE_AT_LAST_UPDATED
    )


def test_keep_open_when_no_tv_configured():
    """Rule 6 (v0.20.1): no liveness signal => KEEP_OPEN.

    The common "just an Apple TV" install (no TV entity) must NOT close on
    every tvOS-26 push-quiet gap — that silently under-counted real playback.
    We now accrue through the gap and rely on the runaway ceiling (rule 5) to
    bound a genuine stuck mirror. Below the ceiling + no TV => KEEP_OPEN.
    """
    assert (
        decide_stale_action(
            **_baseline(tv_entity_configured=False, tv_state=None)
        )
        is StaleAction.KEEP_OPEN
    )


def test_no_tv_still_closes_via_runaway_ceiling():
    """Rule 5 still fires for a no-TV install: a genuinely stuck mirror is
    bounded — past the runaway ceiling it CLOSE_RUNAWAYs regardless."""
    assert (
        decide_stale_action(
            **_baseline(
                tv_entity_configured=False,
                tv_state=None,
                open_event_age_s=STALE_RUNAWAY_CEILING_S + 1,
            )
        )
        is StaleAction.CLOSE_RUNAWAY
    )


# ============================================================================
# KEEP_OPEN — the under-count fix
# ============================================================================


def test_keep_open_when_tv_on():
    """Rule 8: Samsung on => device is being used; KEEP COUNTING.

    This is the fix. Empirical evidence (2026-06-09 live): owner watching
    Netflix continuously while pyatv was push-quiet; Samsung stayed `on`
    throughout; sessions recorded 0.0 min before the fix.
    """
    assert (
        decide_stale_action(**_baseline(tv_state="on"))
        is StaleAction.KEEP_OPEN
    )


def test_keep_open_when_tv_unknown_fail_open():
    """Rule 8: Samsung integration reporting `unknown` => fail-open.

    Under-count is the worse failure for an enforcement tool — when the
    liveness signal is unreadable we default to KEEP_OPEN. Same posture
    as the deployed self-heal automation's Samsung gate.
    """
    assert (
        decide_stale_action(**_baseline(tv_state="unknown"))
        is StaleAction.KEEP_OPEN
    )


def test_keep_open_when_tv_unavailable_fail_open():
    """Rule 8: Samsung `unavailable` (integration dropped) => fail-open.

    Bounded by the runaway-ceiling, so a Samsung outage can't silently
    grant unlimited time — see test_close_runaway_overrides_keep_open.
    """
    assert (
        decide_stale_action(**_baseline(tv_state="unavailable"))
        is StaleAction.KEEP_OPEN
    )


def test_keep_open_when_tv_state_none():
    """Rule 8: configured but entity returned None (not yet registered).
    Bias to keep-counting; treat like 'unknown'."""
    assert (
        decide_stale_action(**_baseline(tv_state=None))
        is StaleAction.KEEP_OPEN
    )


def test_keep_open_when_tv_idle():
    """Rule 8: Samsung 'idle' (home screen / nothing playing on TV) is
    NOT in the definitively-off allow-list — TV is still on, kid could
    legitimately be using ATV. KEEP_OPEN."""
    assert (
        decide_stale_action(**_baseline(tv_state="idle"))
        is StaleAction.KEEP_OPEN
    )


# ============================================================================
# CLOSE_RUNAWAY — Samsung-stuck-on / no-Samsung safety net
# ============================================================================


def test_close_runaway_overrides_keep_open():
    """Rule 5: even with Samsung 'on', force-close once the open event
    exceeds the runaway ceiling.

    Scenario: Samsung firmware bug freezes the entity at `on`, kid
    actually went to bed; the open event would accrue indefinitely
    without this cap. Caps the worst-case over-count at ~2.5h.
    """
    assert (
        decide_stale_action(
            **_baseline(
                open_event_age_s=float(STALE_RUNAWAY_CEILING_S),
                tv_state="on",
            )
        )
        is StaleAction.CLOSE_RUNAWAY
    )


def test_close_runaway_overrides_tv_unknown():
    """Rule 5 over rule 8 also when Samsung is `unknown`/unreadable."""
    assert (
        decide_stale_action(
            **_baseline(
                open_event_age_s=float(STALE_RUNAWAY_CEILING_S) + 100,
                tv_state="unknown",
            )
        )
        is StaleAction.CLOSE_RUNAWAY
    )


def test_close_runaway_just_below_ceiling_still_keep_open():
    """Boundary: open_event_age == ceiling - 1 does NOT trigger runaway."""
    assert (
        decide_stale_action(
            **_baseline(
                open_event_age_s=float(STALE_RUNAWAY_CEILING_S) - 1,
                tv_state="on",
            )
        )
        is StaleAction.KEEP_OPEN
    )


def test_close_runaway_does_not_override_tv_off():
    """Rule 5 BEFORE rule 7: a stuck-on event with TV off goes to RUNAWAY
    (not CLOSE_AT_LAST_UPDATED). This keeps the audit reason accurate —
    the parent sees runaway-ceiling, not a regular stale-close, when the
    cap is the reason."""
    # Both rules would close — runaway just gets there first / wins.
    assert (
        decide_stale_action(
            **_baseline(
                open_event_age_s=float(STALE_RUNAWAY_CEILING_S) + 1,
                tv_state="off",
            )
        )
        is StaleAction.CLOSE_RUNAWAY
    )


def test_close_runaway_open_age_none_is_safe():
    """Rule 5 guards against `open_event_age_s is None` (defensive — the
    coordinator always passes a value, but the pure function must not
    crash on a missing input)."""
    assert (
        decide_stale_action(
            **_baseline(open_event_age_s=None, tv_state="on")
        )
        is StaleAction.KEEP_OPEN
    )


# ============================================================================
# Empirical-evidence reproduction — the three live sessions today
# ============================================================================


def test_live_2026_06_09_session_at_13_00_under_count_caught():
    """Live session 13:00:24→13:05:54 (Samsung 'on' throughout, recorded
    0.0 min before fix). Under the new gate -> KEEP_OPEN (not closed)."""
    # apple_tv 'playing', last_updated stuck since open (age ~329s),
    # Samsung 'on' at both ends. Should be KEEP_OPEN.
    assert (
        decide_stale_action(
            **_baseline(apple_tv_last_updated_age_s=329.0, tv_state="on")
        )
        is StaleAction.KEEP_OPEN
    )


def test_live_2026_06_09_session_at_13_34_legitimate_close():
    """Live session 13:34:33→13:39:54 (Samsung 'on' at open, 'off' at
    close — kid actually stopped watching). New gate must still close."""
    # By the time the check fires (Samsung off at the moment of evaluation),
    # the gate correctly returns CLOSE_AT_LAST_UPDATED.
    assert (
        decide_stale_action(
            **_baseline(apple_tv_last_updated_age_s=320.0, tv_state="off")
        )
        is StaleAction.CLOSE_AT_LAST_UPDATED
    )


def test_live_2026_06_09_session_at_15_12_under_count_caught():
    """Live session 15:12:12→15:17:24 (Samsung 'on' throughout, recorded
    0.0 min). Same shape as 13:00 — KEEP_OPEN."""
    assert (
        decide_stale_action(
            **_baseline(apple_tv_last_updated_age_s=311.0, tv_state="on")
        )
        is StaleAction.KEEP_OPEN
    )
