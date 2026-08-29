"""Tests for v0.18.0 decide_attribution — the central decision function
that composes pyatv + DNS into one of PRESERVE / ANNOTATE_GROUP /
CLOSE_AT_LAST_UPDATED.

The test matrix specifically covers the five injustice traps that the
adversarial review identified must be closed (see CHANGELOG / workflow
synthesis):
  (a) gaming-as-movies (today's bug)
  (b) movies-as-gaming (Disney+ + background GC ping)
  (c) silently charging wrong budget on AMBIENT
  (d) App-Store glance over-charge
  (e) sustained AMBIENT -> CLOSE (visible gap, honest under-count)
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


def _load(name: str, path_fragments: tuple[str, ...]):
    pkg = "custom_components"
    if pkg not in sys.modules:
        m = types.ModuleType(pkg); m.__path__ = []; sys.modules[pkg] = m
    sub = "custom_components.appletv_mgmt"
    if sub not in sys.modules:
        m = types.ModuleType(sub); m.__path__ = []; sys.modules[sub] = m
    path = Path(__file__).parent.parent.joinpath(*path_fragments)
    spec = importlib.util.spec_from_file_location(f"{sub}.{name}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load both modules (dns_classifier is independent; media_attribution has no HA deps either).
dns_cls = _load(
    "dns_classifier",
    ("custom_components", "appletv_mgmt", "dns_classifier.py"),
)
ma = _load(
    "media_attribution",
    ("custom_components", "appletv_mgmt", "media_attribution.py"),
)


Confidence = dns_cls.Confidence
DnsClassification = dns_cls.DnsClassification
decide_attribution = ma.decide_attribution
AttributionAction = ma.AttributionAction
CURATED_STREAMING_BUNDLES = ma.CURATED_STREAMING_BUNDLES


def _dns(
    confidence: str,
    bundle_id: str | None = None,
    group: str | None = None,
    bundle_hit_count: int = 0,
    group_hit_count: int = 0,
):
    """Build a DnsClassification with the given confidence by name."""
    return DnsClassification(
        bundle_id=bundle_id,
        group=group,
        confidence=Confidence[confidence],
        matched_domains=(),
        bundle_hit_count=bundle_hit_count,
        group_hit_count=group_hit_count,
        total_signal_queries=bundle_hit_count + group_hit_count,
    )


# ============================================================================
# Rule 1 — no open event => PRESERVE
# ============================================================================


def test_no_open_event_preserves():
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns("BUNDLE", "publisher.kooapps", "gaming", 5),
        open_event_bundle_id=None,
        open_event_current_group="movies",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.PRESERVE
    assert d.reason == "no_open_event"


# ============================================================================
# Rule 2 — pyatv fresh => PRESERVE (trust pyatv when it's talking)
# ============================================================================


def test_pyatv_fresh_overrides_dns_correction():
    """Even with a strong DNS gaming signal, if pyatv just pushed, we
    trust pyatv. pyatv talking == ground truth."""
    d = decide_attribution(
        pyatv_age_s=10.0,  # just pushed
        dns=_dns("BUNDLE", "publisher.kooapps", "gaming", 5),
        open_event_bundle_id="com.disney.disneyplus",
        open_event_current_group="movies",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.PRESERVE
    assert "pyatv_fresh" in d.reason


def test_pyatv_just_at_freshness_threshold_still_trusted():
    """89s < 90s threshold (default)."""
    d = decide_attribution(
        pyatv_age_s=89.0,
        dns=_dns("BUNDLE", "publisher.kooapps", "gaming", 5),
        open_event_bundle_id="com.disney.disneyplus",
        open_event_current_group="movies",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.PRESERVE


def test_pyatv_just_past_threshold_dns_takes_over():
    """91s > 90s threshold — DNS now in play, and BUNDLE/cross-group
    correction fires."""
    d = decide_attribution(
        pyatv_age_s=91.0,
        dns=_dns("BUNDLE", "publisher.kooapps", "gaming", 5),
        open_event_bundle_id="com.disney.disneyplus",
        open_event_current_group="movies",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.ANNOTATE_GROUP
    assert d.new_group == "gaming"


def test_pyatv_age_none_treated_as_stale():
    """If we have no last_updated info, treat as stale (DNS takes over)."""
    d = decide_attribution(
        pyatv_age_s=None,
        dns=_dns("BUNDLE", "publisher.kooapps", "gaming", 5),
        open_event_bundle_id="com.disney.disneyplus",
        open_event_current_group="movies",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.ANNOTATE_GROUP


# ============================================================================
# Rule 3 — DNS unavailable => PRESERVE (fail-open: v0.17.3 behavior preserved)
# ============================================================================


def test_dns_none_preserves_v0173_behavior():
    """AdGuard down / not configured -> dns=None -> v0.17.3 behavior."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=None,
        open_event_bundle_id="com.disney.disneyplus",
        open_event_current_group="movies",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.PRESERVE
    assert d.reason == "dns_unavailable"


def test_dns_confidence_none_preserves():
    """Empty window -> Confidence.NONE -> PRESERVE."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns("NONE"),
        open_event_bundle_id="com.disney.disneyplus",
        open_event_current_group="movies",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.PRESERVE


# ============================================================================
# Rules 4-5 — AMBIENT_ONLY: streak < N preserves; streak >= N closes
# ============================================================================


def test_ambient_short_streak_preserves():
    """First few AMBIENT ticks don't trigger close — could be transient
    DNS lull during legitimate playback."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns("AMBIENT_ONLY"),
        open_event_bundle_id="com.disney.disneyplus",
        open_event_current_group="movies",
        consecutive_ambient_ticks=2,
    )
    assert d.action is AttributionAction.PRESERVE


def test_ambient_sustained_streak_triggers_visible_gap_close():
    """4+ consecutive AMBIENT ticks (~2 min at 30s tick) -> CLOSE.
    Honest under-count: the kid walked away with Samsung on.

    v0.18.0 follow-up: a non-curated bundle is used because the curated
    streaming safelist (Disney+/Netflix/etc.) intentionally exempts those
    from sustained-AMBIENT close — see test_ambient_close_blocked_for_
    curated_streaming_bundle for that behavior. The visible-gap close
    is meant for "kid walked away" on idle/unknown/non-streaming work."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns("AMBIENT_ONLY"),
        open_event_bundle_id="com.someobscure.app",
        open_event_current_group="other",
        consecutive_ambient_ticks=4,
    )
    assert d.action is AttributionAction.CLOSE_AT_LAST_UPDATED
    assert "sustained_ambient" in d.reason


# ============================================================================
# Rules 6-7 — BUNDLE match / same-group => PRESERVE
# ============================================================================


def test_dns_bundle_matches_open_event_preserves():
    """DNS confirms what pyatv last said — nothing to do."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns("BUNDLE", "com.disney.disneyplus", "movies", 5),
        open_event_bundle_id="com.disney.disneyplus",
        open_event_current_group="movies",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.PRESERVE


def test_dns_bundle_different_same_group_preserves():
    """Disney+ -> Netflix is still movies — budget unaffected, no need
    to act."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns("BUNDLE", "com.netflix.Netflix", "movies", 5),
        open_event_bundle_id="com.disney.disneyplus",
        open_event_current_group="movies",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.PRESERVE


# ============================================================================
# Rule 8 — cross-group BUNDLE correction (the live-bug fix)
# ============================================================================


def test_today_live_bug_cross_group_bundle_correction():
    """The exact case that motivated this whole effort:
    Open event = Disney+/movies (pyatv said so 2h ago).
    DNS now shows KooApps with >= 3 hits.
    -> ANNOTATE_GROUP to gaming."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns(
            "BUNDLE",
            bundle_id="publisher.kooapps",
            group="gaming",
            bundle_hit_count=5,
        ),
        open_event_bundle_id="com.disney.disneyplus",
        open_event_current_group="movies",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.ANNOTATE_GROUP
    assert d.new_group == "gaming"
    assert "publisher.kooapps" in d.reason
    assert "movies_to_gaming" in d.reason


def test_cross_group_blocked_when_hit_count_below_threshold():
    """2 hits < 3-hit threshold — don't flip the group yet."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns(
            "BUNDLE",
            bundle_id="publisher.kooapps",
            group="gaming",
            bundle_hit_count=2,
        ),
        open_event_bundle_id="com.disney.disneyplus",
        open_event_current_group="movies",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.PRESERVE
    assert "insufficient_cross_group_hits" in d.reason


def test_cross_group_correction_threshold_can_be_tuned_via_arg():
    """Caller can lower the threshold for testing or tighter installs."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns(
            "BUNDLE",
            bundle_id="publisher.kooapps",
            group="gaming",
            bundle_hit_count=2,
        ),
        open_event_bundle_id="com.disney.disneyplus",
        open_event_current_group="movies",
        consecutive_ambient_ticks=0,
        min_bundle_cross_group_hits=2,  # tightened
    )
    assert d.action is AttributionAction.ANNOTATE_GROUP


# ============================================================================
# Rule 10 — CURATED_STREAMING_BUNDLES safelist (movies-as-gaming protection)
# ============================================================================


def test_disney_plus_protected_from_group_only_gaming_promotion():
    """The 'real Disney+ session + background Game Center ping' false
    positive. GROUP_ONLY signal alone must NOT promote Disney+ to gaming.

    This was injustice trap (b) — movies-as-gaming false positive."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns(
            "GROUP_ONLY",
            group="gaming",
            group_hit_count=2,
        ),
        open_event_bundle_id="com.disney.disneyplus",
        open_event_current_group="movies",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.PRESERVE
    assert "curated_safelist" in d.reason


def test_netflix_protected_from_group_only_gaming_promotion():
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns("GROUP_ONLY", group="gaming", group_hit_count=5),
        open_event_bundle_id="com.netflix.Netflix",
        open_event_current_group="movies",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.PRESERVE


def test_apple_music_protected_from_gaming_promotion():
    """AirPlay-audio sessions (HomePod, etc.) must not be misread as
    gaming because Game Center pings periodically."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns("GROUP_ONLY", group="gaming", group_hit_count=5),
        open_event_bundle_id="com.apple.TVMusic",
        open_event_current_group="other",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.PRESERVE


def test_tvairplay_protected_from_gaming_promotion():
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns("GROUP_ONLY", group="gaming", group_hit_count=5),
        open_event_bundle_id="com.apple.TVAirPlay",
        open_event_current_group="other",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.PRESERVE


def test_curated_safelist_does_not_block_bundle_strength_correction():
    """A BUNDLE signal (different streaming bundle, e.g. Netflix while open
    event is Disney+) is NOT a downgrade attempt — same group; this rule
    fires before the safelist check and PRESERVES correctly."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns("BUNDLE", "com.netflix.Netflix", "movies", 5),
        open_event_bundle_id="com.disney.disneyplus",
        open_event_current_group="movies",
        consecutive_ambient_ticks=0,
    )
    # Same-group rule applies, PRESERVE.
    assert d.action is AttributionAction.PRESERVE


def test_curated_safelist_does_not_block_cross_group_bundle_correction():
    """If DNS shows BUNDLE-strength evidence of a game (not just GC ping),
    that BEATS the safelist — Disney+ would still be flipped if the game's
    own CDN fires. Tests that the safelist applies to GROUP_ONLY only."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns(
            "BUNDLE",
            bundle_id="publisher.kooapps",
            group="gaming",
            bundle_hit_count=5,
        ),
        open_event_bundle_id="com.disney.disneyplus",
        open_event_current_group="movies",
        consecutive_ambient_ticks=0,
    )
    # Cross-group BUNDLE correction wins; safelist doesn't gate this path.
    assert d.action is AttributionAction.ANNOTATE_GROUP
    assert d.new_group == "gaming"


# ============================================================================
# Rules 11-13 — GROUP_ONLY for unknown bundles
# ============================================================================


def test_group_only_attributes_unknown_bundle():
    """Bundle is unknown -> GROUP_ONLY is the best signal -> attribute to
    that group. This is how games we DON'T have a curated bundle for get
    correctly billed to gaming."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns("GROUP_ONLY", group="gaming", group_hit_count=3),
        open_event_bundle_id="unknown",
        open_event_current_group="other",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.ANNOTATE_GROUP
    assert d.new_group == "gaming"


def test_group_only_preserves_when_group_already_matches():
    """If we're already attributing to gaming, a GC ping shouldn't churn
    the segment list."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns("GROUP_ONLY", group="gaming", group_hit_count=3),
        open_event_bundle_id="unknown",
        open_event_current_group="gaming",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.PRESERVE
    assert d.reason == "group_match"


def test_group_only_too_weak_to_flip_non_curated_bundle():
    """If pyatv identified a non-curated bundle (e.g. some obscure app) and
    DNS shows only group-only gaming evidence, we're conservative — don't
    flip the group. The app's own CDN will eventually fire and we'll catch
    it at BUNDLE strength."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns("GROUP_ONLY", group="gaming", group_hit_count=3),
        open_event_bundle_id="com.someobscure.app",
        open_event_current_group="other",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.PRESERVE
    assert "too_weak_to_flip" in d.reason


# ============================================================================
# Adversarial — the 5 injustice traps from the workflow synthesis
# ============================================================================


def test_injustice_a_gaming_as_movies_caught():
    """Today's bug. pyatv stale on Disney+, kid playing KooApps. With DNS
    showing kooapps BUNDLE >= 3 hits, the gate correctly corrects to
    gaming via ANNOTATE_GROUP (which the coordinator turns into a
    GroupSegment append, not a close-reopen)."""
    d = decide_attribution(
        pyatv_age_s=7200.0,  # 2h stale
        dns=_dns("BUNDLE", "publisher.kooapps", "gaming", 8),
        open_event_bundle_id="com.disney.disneyplus",
        open_event_current_group="movies",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.ANNOTATE_GROUP
    assert d.new_group == "gaming"


def test_injustice_b_movies_as_gaming_blocked():
    """Real Disney+ session + occasional Game Center heartbeat. Without
    this protection, Disney+ would be misclassified as gaming. With
    curated safelist, PRESERVED."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns("GROUP_ONLY", group="gaming", group_hit_count=3),
        open_event_bundle_id="com.disney.disneyplus",
        open_event_current_group="movies",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.PRESERVE


def test_injustice_c_sustained_ambient_visible_gap_close():
    """Kid walked away with Samsung on. v0.17.3 would keep accruing
    indefinitely. v0.18.0: 4 ticks of AMBIENT -> visible close.

    v0.18.0 follow-up: this test uses a non-curated bundle because the
    AMBIENT close was intentionally restricted to non-streaming bundles
    (real Disney+/Netflix sessions are AMBIENT-prone due to buffering).
    For curated bundles, see test_ambient_close_blocked_for_curated_
    streaming_bundle. For the "kid walked away" case, the typical state
    after the kid leaves is the home screen on an unknown/games bundle."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns("AMBIENT_ONLY"),
        open_event_bundle_id="unknown",
        open_event_current_group="other",
        consecutive_ambient_ticks=4,
    )
    assert d.action is AttributionAction.CLOSE_AT_LAST_UPDATED


def test_injustice_d_app_store_glance_not_overcharged():
    """Kid opens App Store briefly to browse. AMBIENT (mzstatic + amp-api
    are in BACKGROUND_NOISE). Short streak -> PRESERVE -> doesn't burn
    a different bucket than what was open."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns("AMBIENT_ONLY"),
        open_event_bundle_id="com.disney.disneyplus",
        open_event_current_group="movies",
        consecutive_ambient_ticks=1,
    )
    assert d.action is AttributionAction.PRESERVE


# ============================================================================
# Pre-existing behavior preservation (v0.17.3 + v0.17.4)
# ============================================================================


def test_ambient_close_blocked_for_curated_streaming_bundle():
    """v0.18.0 follow-up — Disney+ uses long-lived TCP + heavy buffering.
    AMBIENT (no foreground DNS) is the EXPECTED steady-state of a real
    Disney+ session, NOT 'kid walked away'. Caught live during monitor-
    mode rollout 2026-06-14: 30+ spurious close proposals fired during a
    real Disney+ session. Curated-bundle safelist must protect against
    this — Disney+/Netflix/etc. must NOT be closed by sustained AMBIENT."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns("AMBIENT_ONLY"),
        open_event_bundle_id="com.disney.disneyplus",
        open_event_current_group="movies",
        consecutive_ambient_ticks=10,  # well past threshold
    )
    assert d.action is AttributionAction.PRESERVE
    assert "curated_safelist" in d.reason


def test_ambient_close_blocked_for_netflix_too():
    """Same protection for Netflix."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns("AMBIENT_ONLY"),
        open_event_bundle_id="com.netflix.Netflix",
        open_event_current_group="movies",
        consecutive_ambient_ticks=8,
    )
    assert d.action is AttributionAction.PRESERVE


def test_ambient_close_still_fires_for_non_curated_bundles():
    """Regression guard: the safelist protection must NOT block the
    intended use case (sustained AMBIENT on a non-streaming bundle ->
    visible-gap CLOSE for "kid walked away" detection)."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns("AMBIENT_ONLY"),
        open_event_bundle_id="com.someobscure.app",
        open_event_current_group="other",
        consecutive_ambient_ticks=4,
    )
    assert d.action is AttributionAction.CLOSE_AT_LAST_UPDATED


def test_ambient_close_still_fires_for_unknown_bundle():
    """Unknown bundle + sustained AMBIENT -> CLOSE. Critical for the
    "kid started something, walked away" scenario."""
    d = decide_attribution(
        pyatv_age_s=600.0,
        dns=_dns("AMBIENT_ONLY"),
        open_event_bundle_id="unknown",
        open_event_current_group="other",
        consecutive_ambient_ticks=4,
    )
    assert d.action is AttributionAction.CLOSE_AT_LAST_UPDATED


def test_v0173_behavior_exactly_when_dns_feature_off():
    """When the coordinator doesn't pass dns (feature disabled or AdGuard
    down), behavior is EXACTLY v0.17.3 — no corrections, no segments."""
    d = decide_attribution(
        pyatv_age_s=600.0,  # very stale, Samsung-on KEEP_OPEN path
        dns=None,
        open_event_bundle_id="com.disney.disneyplus",
        open_event_current_group="movies",
        consecutive_ambient_ticks=0,
    )
    assert d.action is AttributionAction.PRESERVE
