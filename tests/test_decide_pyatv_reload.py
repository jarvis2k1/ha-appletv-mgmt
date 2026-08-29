"""Pure-function decision-table tests for `decide_pyatv_reload`.

The function is HA-free by design (operates on primitives via a frozen
dataclass), so these tests import `media_attribution` directly without
any HA stubbing — same loader scaffolding as `test_decide_stale_action`.

Decision rule — two trigger shapes (v0.19.3), all gates in a shape must hold:
  A) idle/on (push-quiet symptom):
       pyatv_quiet_s >= MIN_STUCK_S (10 min) AND samsung 'on' AND
       dns_recent_hits >= MIN_HITS (1 hit / 60s) AND rate-limit OK.
  B) playing/paused/buffering (froze mid-playback):
       pyatv_quiet_s >= PLAYING_STUCK_S (20 min) AND samsung 'on' AND
       rate-limit OK. DNS is NOT required — steady streaming is long-lived
       TCP that makes ~no DNS, so the 60s gate is blind to it; Samsung-on
       is the liveness proof.

Any unmet gate -> False. Documented as fail-CLOSED because a spurious
reload disrupts pyatv mid-session, whereas a missed reload just means we
wait for the next coordinator tick.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


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
decide_pyatv_reload = ma.decide_pyatv_reload
PyatvReloadInputs = ma.PyatvReloadInputs
PYATV_RELOAD_MIN_STUCK_S = ma.PYATV_RELOAD_MIN_STUCK_S
PYATV_RELOAD_PLAYING_STUCK_S = ma.PYATV_RELOAD_PLAYING_STUCK_S
PYATV_RELOAD_RATE_LIMIT_S = ma.PYATV_RELOAD_RATE_LIMIT_S
DNS_RECENT_MIN_HITS = ma.DNS_RECENT_MIN_HITS


# ---- baseline: a "ready to reload" scenario; each test below mutates
# exactly one field to isolate the rule under test. ----
def _baseline(**overrides):
    base = dict(
        apple_tv_state="idle",
        pyatv_quiet_s=float(PYATV_RELOAD_MIN_STUCK_S + 100),  # > 10 min
        samsung_state="on",
        dns_recent_hits=3,
        last_reload_age_s=None,  # never reloaded before
    )
    base.update(overrides)
    return PyatvReloadInputs(**base)


# ============================================================================
# Happy path
# ============================================================================


def test_reload_when_baseline_satisfied():
    """All gates satisfied -> reload. The 'idle' + 10 min push-silent +
    Samsung on + DNS recent + never-reloaded shape from the live
    pyatv-stuck symptom."""
    assert decide_pyatv_reload(_baseline()) is True


def test_reload_when_apple_tv_on_also_triggers():
    """The other push-quiet symptom state is `on` (home screen). Same
    treatment as `idle`."""
    assert decide_pyatv_reload(_baseline(apple_tv_state="on")) is True


# ============================================================================
# Rule 1: apple_tv_state gate — only {idle, on} trigger reload
# ============================================================================


def test_no_reload_when_playing_below_playing_threshold():
    """v0.19.3 — `playing` IS now a trigger, but at a HIGHER threshold
    (20 min) than idle/on (10 min). The baseline ~11.6 min is past the
    idle threshold but under the playing one, so no reload yet — normal
    steady-playback push gaps must not churn a reload."""
    assert decide_pyatv_reload(_baseline(apple_tv_state="playing")) is False


def test_no_reload_when_paused_below_playing_threshold():
    assert decide_pyatv_reload(_baseline(apple_tv_state="paused")) is False


def test_no_reload_when_buffering_below_playing_threshold():
    assert decide_pyatv_reload(_baseline(apple_tv_state="buffering")) is False


def test_no_reload_when_apple_tv_off():
    """Device truly off — nothing to heal; no point reloading."""
    assert decide_pyatv_reload(_baseline(apple_tv_state="off")) is False


def test_no_reload_when_apple_tv_unavailable():
    """Entity dropped — wrong layer to fix. Don't poke pyatv."""
    assert (
        decide_pyatv_reload(_baseline(apple_tv_state="unavailable")) is False
    )


def test_no_reload_when_apple_tv_unknown():
    """Defensive — `unknown` isn't a trigger state. Fail-closed."""
    assert decide_pyatv_reload(_baseline(apple_tv_state="unknown")) is False


def test_no_reload_when_apple_tv_none():
    """Entity not registered yet — None apple_tv_state."""
    assert decide_pyatv_reload(_baseline(apple_tv_state=None)) is False


# ============================================================================
# Rule 2: pyatv_quiet_s threshold gate
# ============================================================================


def test_no_reload_when_pyatv_quiet_unknown():
    """No last_updated -> can't evaluate staleness -> fail-closed."""
    assert decide_pyatv_reload(_baseline(pyatv_quiet_s=None)) is False


def test_no_reload_when_pyatv_quiet_below_threshold():
    """1 second under the threshold -> not stale enough yet."""
    assert (
        decide_pyatv_reload(
            _baseline(pyatv_quiet_s=float(PYATV_RELOAD_MIN_STUCK_S - 1))
        )
        is False
    )


def test_reload_at_exact_threshold_boundary():
    """Threshold uses closed-interval `>=`. Documented inclusive."""
    assert (
        decide_pyatv_reload(
            _baseline(pyatv_quiet_s=float(PYATV_RELOAD_MIN_STUCK_S))
        )
        is True
    )


def test_no_reload_when_pyatv_quiet_is_zero():
    """Fresh entity (0s since last update). Doesn't pass threshold."""
    assert decide_pyatv_reload(_baseline(pyatv_quiet_s=0.0)) is False


# ============================================================================
# Rule 3: Samsung TV liveness gate — strict 'on'
# ============================================================================


def test_no_reload_when_samsung_off():
    """Kid isn't watching anything; reloading is pointless + disruptive."""
    assert decide_pyatv_reload(_baseline(samsung_state="off")) is False


def test_no_reload_when_samsung_standby():
    """Same as off — `standby` is some Samsung firmwares' off state."""
    assert decide_pyatv_reload(_baseline(samsung_state="standby")) is False


def test_no_reload_when_samsung_unknown():
    """STRICT here (unlike decide_stale_action which fails open on unknown)
    — a wrong reload is worse than a missed one."""
    assert decide_pyatv_reload(_baseline(samsung_state="unknown")) is False


def test_no_reload_when_samsung_unavailable():
    """Same as unknown — strict gate."""
    assert (
        decide_pyatv_reload(_baseline(samsung_state="unavailable")) is False
    )


def test_no_reload_when_samsung_none():
    """tv_entity_id not configured -> None state -> no reload.
    Falls back to YAML self-heal (which can use different gates) when
    Samsung isn't wired up."""
    assert decide_pyatv_reload(_baseline(samsung_state=None)) is False


def test_no_reload_when_samsung_idle():
    """Strict 'on' — even Samsung 'idle' (home menu visible, TV on but
    no input) is rejected by this gate. We want positive `on` evidence
    that the kid is actively using the device."""
    assert decide_pyatv_reload(_baseline(samsung_state="idle")) is False


# ============================================================================
# Rule 4: DNS-recency corroborator
# ============================================================================


def test_no_reload_when_dns_empty():
    """0 DNS hits in last 60s — device might be sleeping or offline.
    Skip the reload (fail-closed)."""
    assert decide_pyatv_reload(_baseline(dns_recent_hits=0)) is False


def test_reload_at_exact_dns_threshold():
    """Threshold is `>= 1` — one hit is enough."""
    assert (
        decide_pyatv_reload(_baseline(dns_recent_hits=DNS_RECENT_MIN_HITS))
        is True
    )


def test_reload_when_many_dns_hits():
    """Plenty of recent DNS -> device is clearly online."""
    assert decide_pyatv_reload(_baseline(dns_recent_hits=50)) is True


# ============================================================================
# Rule 5: rate-limit gate
# ============================================================================


def test_reload_when_never_reloaded_before():
    """None last_reload_age_s == fresh start, no rate-limit yet."""
    assert decide_pyatv_reload(_baseline(last_reload_age_s=None)) is True


def test_no_reload_when_rate_limited_just_after():
    """Reloaded 1 second ago — definitely rate-limited."""
    assert decide_pyatv_reload(_baseline(last_reload_age_s=1.0)) is False


def test_no_reload_when_rate_limit_just_below():
    """Just under the 10-min cap — still rate-limited."""
    assert (
        decide_pyatv_reload(
            _baseline(last_reload_age_s=float(PYATV_RELOAD_RATE_LIMIT_S - 1))
        )
        is False
    )


def test_reload_at_exact_rate_limit_boundary():
    """Rate-limit uses closed-interval `>=`. Documented inclusive so the
    second reload fires exactly at +RATE_LIMIT_S."""
    assert (
        decide_pyatv_reload(
            _baseline(last_reload_age_s=float(PYATV_RELOAD_RATE_LIMIT_S))
        )
        is True
    )


def test_reload_well_past_rate_limit():
    """Hours since last reload -> definitely allowed."""
    assert decide_pyatv_reload(_baseline(last_reload_age_s=3600.0)) is True


# ============================================================================
# Multi-gate composition — ensure the AND semantics hold
# ============================================================================


def test_no_reload_when_all_gates_marginally_failed_together():
    """Stress: every gate fails simultaneously. Must return False (not
    accidentally OR somewhere)."""
    inputs = PyatvReloadInputs(
        apple_tv_state="playing",
        pyatv_quiet_s=0.0,
        samsung_state="off",
        dns_recent_hits=0,
        last_reload_age_s=0.0,
    )
    assert decide_pyatv_reload(inputs) is False


def test_reload_with_minimal_passing_values_at_every_boundary():
    """Every gate at its minimum-passing value simultaneously -> True."""
    inputs = PyatvReloadInputs(
        apple_tv_state="idle",
        pyatv_quiet_s=float(PYATV_RELOAD_MIN_STUCK_S),
        samsung_state="on",
        dns_recent_hits=DNS_RECENT_MIN_HITS,
        last_reload_age_s=float(PYATV_RELOAD_RATE_LIMIT_S),
    )
    assert decide_pyatv_reload(inputs) is True


# ============================================================================
# v0.19.3 — stuck-PLAYING reload branch (pyatv froze mid-playback)
# ============================================================================


def _playing(**overrides):
    """Baseline for the stuck-playing branch: playing, past the 20-min
    playing threshold, Samsung on, NO DNS (streams make sparse DNS),
    never reloaded."""
    base = dict(
        apple_tv_state="playing",
        pyatv_quiet_s=float(PYATV_RELOAD_PLAYING_STUCK_S + 60),
        samsung_state="on",
        dns_recent_hits=0,          # steady stream = long-lived TCP, ~no DNS
        last_reload_age_s=None,
    )
    base.update(overrides)
    return PyatvReloadInputs(**base)


def test_reload_when_playing_stuck_past_threshold_without_dns():
    """The live 2026-06-20 bug: stuck `playing` 20+ min, Samsung on, and
    NO recent DNS (steady stream). Must reload — Samsung-on is the liveness
    proof; the DNS gate is intentionally NOT required for playing."""
    assert decide_pyatv_reload(_playing()) is True


def test_reload_when_paused_stuck_past_threshold():
    assert decide_pyatv_reload(_playing(apple_tv_state="paused")) is True


def test_reload_when_buffering_stuck_past_threshold():
    assert decide_pyatv_reload(_playing(apple_tv_state="buffering")) is True


def test_no_reload_playing_just_under_playing_threshold():
    """1 s under the 20-min playing threshold -> not stale enough."""
    assert (
        decide_pyatv_reload(
            _playing(pyatv_quiet_s=float(PYATV_RELOAD_PLAYING_STUCK_S - 1))
        )
        is False
    )


def test_reload_playing_at_exact_playing_threshold():
    assert (
        decide_pyatv_reload(
            _playing(pyatv_quiet_s=float(PYATV_RELOAD_PLAYING_STUCK_S))
        )
        is True
    )


def test_idle_threshold_does_not_apply_to_playing():
    """A playing freeze at the IDLE threshold (10 min) must NOT reload —
    it needs the full 20-min playing threshold. Guards against the higher
    bar being accidentally lowered for playing."""
    assert (
        decide_pyatv_reload(_playing(pyatv_quiet_s=float(PYATV_RELOAD_MIN_STUCK_S)))
        is False
    )


def test_no_reload_playing_when_samsung_off():
    """Samsung off = nobody watching -> don't reload (no liveness proof)."""
    assert decide_pyatv_reload(_playing(samsung_state="off")) is False


def test_no_reload_playing_when_samsung_standby():
    assert decide_pyatv_reload(_playing(samsung_state="standby")) is False


def test_no_reload_playing_when_rate_limited():
    """Just reloaded -> rate-limit blocks a second reload even when stuck."""
    assert (
        decide_pyatv_reload(_playing(last_reload_age_s=60.0)) is False
    )


def test_reload_playing_after_rate_limit_elapsed():
    assert (
        decide_pyatv_reload(
            _playing(last_reload_age_s=float(PYATV_RELOAD_RATE_LIMIT_S))
        )
        is True
    )


def test_playing_does_not_require_dns_but_idle_still_does():
    """Contrast: playing fires with 0 DNS hits; idle with 0 DNS does NOT
    (idle still requires the online corroborator)."""
    assert decide_pyatv_reload(_playing(dns_recent_hits=0)) is True
    assert decide_pyatv_reload(_baseline(dns_recent_hits=0)) is False


def test_playing_dns_is_a_noop_in_both_directions():
    """DNS is genuinely IGNORED for the playing branch — present or absent
    yields the same True (Samsung-on is the sole liveness proof)."""
    assert decide_pyatv_reload(_playing(dns_recent_hits=0)) is True
    assert decide_pyatv_reload(_playing(dns_recent_hits=5)) is True
