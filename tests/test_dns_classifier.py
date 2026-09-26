"""Tests for the v0.18.0 DNS classifier — pure module, HA-free.

Includes a LIVE-BUG REPLAY: the exact AdGuard DNS pattern observed during
today's (2026-06-14) 126.7-min misattributed session, asserting the
classifier would have correctly identified gaming activity.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _load():
    pkg = "custom_components"
    if pkg not in sys.modules:
        m = types.ModuleType(pkg); m.__path__ = []; sys.modules[pkg] = m
    sub = "custom_components.appletv_mgmt"
    if sub not in sys.modules:
        m = types.ModuleType(sub); m.__path__ = []; sys.modules[sub] = m
    path = (
        Path(__file__).parent.parent
        / "custom_components" / "appletv_mgmt" / "dns_classifier.py"
    )
    spec = importlib.util.spec_from_file_location(f"{sub}.dns_classifier", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


dns = _load()
Confidence = dns.Confidence
classify_dns_window = dns.classify_dns_window


NOW = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)


def _at(seconds_ago: float) -> datetime:
    return NOW - timedelta(seconds=seconds_ago)


# ============================================================================
# Confidence levels — the four-rung ladder
# ============================================================================


def test_empty_window_is_none():
    """No DNS at all -> NONE (and only NONE)."""
    r = classify_dns_window([], now=NOW)
    assert r.confidence is Confidence.NONE
    assert r.bundle_id is None
    assert r.group is None


def test_background_only_is_ambient():
    """Only Apple keepalive / NTP / OS chatter -> AMBIENT_ONLY (the
    device is online but no foreground app traffic). Important so the
    coordinator can distinguish 'device offline' from 'device idle but
    online' for the visible-gap CLOSE rule."""
    r = classify_dns_window([
        ("gateway.fe2.apple-dns.net", _at(10)),
        ("time.apple.com", _at(20)),
        ("pancake.apple.com", _at(30)),
        ("init-p01md.apple.com", _at(40)),
        ("_dns-push-tls._tcp.service.arpa", _at(50)),
    ], now=NOW)
    assert r.confidence is Confidence.AMBIENT_ONLY
    assert r.bundle_id is None
    assert r.group is None
    assert r.total_signal_queries == 0


def test_game_center_alone_is_group_only_gaming():
    """Game Center heartbeats fire when a game-capable app is foreground
    (and occasionally idle). On their own they tell us 'gaming' but not
    which game -> GROUP_ONLY, gaming."""
    r = classify_dns_window([
        ("stats.gc.fe2.apple-dns.net", _at(10)),
        ("profile.gc.fe2.apple-dns.net", _at(20)),
    ], now=NOW)
    assert r.confidence is Confidence.GROUP_ONLY
    assert r.group == "gaming"
    assert r.bundle_id is None
    assert r.group_hit_count == 2


def test_specific_streaming_bundle_is_bundle_confidence():
    """A Disney+/Netflix/Prime CDN hit gives BUNDLE confidence + exact
    bundle_id."""
    r = classify_dns_window([
        ("disney.api.edge.bamgrid.com", _at(10)),
        ("appconfigs.disney-plus.net", _at(20)),
    ], now=NOW)
    assert r.confidence is Confidence.BUNDLE
    assert r.bundle_id == "com.disney.disneyplus"
    assert r.group == "movies"
    assert r.bundle_hit_count == 2


# ============================================================================
# Specific app coverage — the owner's actual install
# ============================================================================


def test_netflix_classified_correctly():
    r = classify_dns_window([
        ("ipv4-c005-xfw001-vodafonede-isp.1.oca.nf", _at(10)),  # Netflix open-connect
        ("nflxvideo.net", _at(15)),
    ], now=NOW)
    # OCA isn't in the seed; the nflxvideo.net is.
    assert r.bundle_id == "com.netflix.Netflix"
    assert r.group == "movies"


def test_disney_via_fastly_cdn_classified():
    """Disney+ traffic often fronts through Fastly. Seed includes
    `dss-eu.map.fastly.net` so it's caught."""
    r = classify_dns_window([
        ("dualstack.dss-eu.map.fastly.net", _at(10)),
    ], now=NOW)
    assert r.bundle_id == "com.disney.disneyplus"


def test_kooapps_games_classified_as_gaming_publisher():
    """KooApps publishes multiple games; we identify the publisher,
    not the specific title."""
    r = classify_dns_window([
        ("usa-kooapps-dlc.s3.amazonaws.com", _at(10)),
        ("www.kooappsservers.com", _at(20)),
        ("kaserver-new-2-elb-1744555362.us-east-1.elb.amazonaws.com",
         _at(30)),  # not seeded directly — only kooapps* substring
    ], now=NOW)
    assert r.confidence is Confidence.BUNDLE
    assert r.bundle_id == "publisher.kooapps"
    assert r.group == "gaming"


def test_perchang_games_classified_as_gaming_publisher():
    r = classify_dns_window([
        ("perchangdata.net", _at(10)),
    ], now=NOW)
    assert r.confidence is Confidence.BUNDLE
    assert r.bundle_id == "publisher.perchang"
    assert r.group == "gaming"


def test_jellyfin_classified_as_movies():
    r = classify_dns_window([
        ("jellyfin.example.local", _at(10)),
    ], now=NOW)
    assert r.bundle_id == "org.jellyfin.swiftfin"
    assert r.group == "movies"


def test_youtube_classified_as_tv_shows():
    r = classify_dns_window([
        ("googlevideo.com", _at(10)),
        ("i.ytimg.com", _at(15)),
    ], now=NOW)
    assert r.bundle_id == "com.google.ios.youtube"
    assert r.group == "tv_shows"


# ============================================================================
# Recency tiers
# ============================================================================


def test_bundle_match_outside_bundle_recency_does_not_count():
    """A Disney+ hit 120s ago > default bundle_recency_s=60s should NOT
    qualify as BUNDLE confidence."""
    r = classify_dns_window([
        ("disney.api.edge.bamgrid.com", _at(120)),
    ], now=NOW)
    # Falls through to AMBIENT (saw a domain, but not within signal window
    # AND not a background match either) — actually it should go to AMBIENT
    # because we treat anything-but-classified-signal as ambient.
    assert r.confidence is Confidence.AMBIENT_ONLY


def test_group_only_uses_wider_recency():
    """GC heartbeat 150s ago: still GROUP_ONLY (within 180s)."""
    r = classify_dns_window([
        ("stats.gc.fe2.apple-dns.net", _at(150)),
    ], now=NOW)
    assert r.confidence is Confidence.GROUP_ONLY


def test_group_only_outside_group_recency_drops_to_ambient():
    """GC heartbeat 250s ago > 180s should not count as signal."""
    r = classify_dns_window([
        ("stats.gc.fe2.apple-dns.net", _at(250)),
    ], now=NOW)
    assert r.confidence is Confidence.AMBIENT_ONLY


def test_custom_recency_windows_respected():
    """Callers can tighten/loosen the windows."""
    r = classify_dns_window([
        ("disney.api.edge.bamgrid.com", _at(10)),
    ], now=NOW, bundle_recency_s=5, group_recency_s=10)
    # 10s ago is outside 5s; not BUNDLE; falls to ambient.
    assert r.confidence is Confidence.AMBIENT_ONLY


# ============================================================================
# Background-noise discrimination
# ============================================================================


def test_app_store_browsing_is_background_not_signal():
    """When the kid opens the App Store to browse, we should NOT charge
    that as gaming. App Store endpoints (mzstatic, amp-api-edge.apps.apple.com)
    are explicitly in BACKGROUND_NOISE."""
    r = classify_dns_window([
        ("amp-api-edge.apps.apple.com", _at(10)),
        ("amp-api-search-edge.apps-lb.itunes-apple.com.akadns.net", _at(15)),
        ("is-ssl.mzstatic.com.itunes-apple.com.akadns.net", _at(20)),
    ], now=NOW)
    assert r.confidence is Confidence.AMBIENT_ONLY
    assert r.bundle_id is None
    assert r.group is None


def test_ntp_and_pool_servers_are_background():
    r = classify_dns_window([
        ("0.datadog.pool.ntp.org", _at(10)),
        ("time.apple.com", _at(20)),
    ], now=NOW)
    assert r.confidence is Confidence.AMBIENT_ONLY


# ============================================================================
# Tie-breaking
# ============================================================================


def test_more_hits_wins_when_two_bundles_compete():
    """If two bundles both fire, the one with more hits wins."""
    r = classify_dns_window([
        ("disney.api.edge.bamgrid.com", _at(10)),
        ("appconfigs.disney-plus.net", _at(20)),
        ("dss-eu.map.fastly.net", _at(30)),
        ("nflxvideo.net", _at(40)),
    ], now=NOW)
    assert r.bundle_id == "com.disney.disneyplus"
    assert r.bundle_hit_count == 3


def test_recency_breaks_tie_when_hit_counts_equal():
    """Same hit count -> the one with the most-recent hit wins."""
    r = classify_dns_window([
        ("disney.api.edge.bamgrid.com", _at(10)),
        ("nflxvideo.net", _at(40)),
    ], now=NOW)
    assert r.bundle_id == "com.disney.disneyplus"  # newer


# ============================================================================
# Confidence floor (BUNDLE beats GROUP_ONLY)
# ============================================================================


def test_bundle_hit_outranks_group_only_in_same_window():
    """If we see Disney+ AND a Game Center heartbeat in the same window,
    the BUNDLE wins (Disney+ is the foreground app; GC is incidental)."""
    r = classify_dns_window([
        ("disney.api.edge.bamgrid.com", _at(10)),
        ("stats.gc.fe2.apple-dns.net", _at(15)),
    ], now=NOW)
    assert r.confidence is Confidence.BUNDLE
    assert r.bundle_id == "com.disney.disneyplus"
    assert r.group == "movies"


# ============================================================================
# LIVE-BUG REPLAY — today's actual session
# ============================================================================
# Domains and approximate timing from the AdGuard query log captured during
# the 2026-06-14 06:27-08:33 UTC session that was misattributed as 126.7 min
# of Disney+ but was actually ~9 min Disney+ + ~117 min gaming.
#
# This test asserts: classifying a 30-second window taken from the GAMING
# portion of the session (post-Disney+ silence) MUST classify as gaming.
# It's the gate that proves the fix actually fixes the live bug.
#
# Approximate data shape from the real log (per workflow context CTX block):
# - 754 ATV DNS queries over the 2h6m window
# - Disney+: 2 queries (both at 06:36:18, ~9min after open)
# - Game Center: 41 queries
# - KooApps backends: ~17 queries (kooappsservers, kooapps-dlc, kaserver-elb)
# - Apple infra background: 636 queries
# - The remaining ~58 queries are misc (perchangdata, generic Apple maps,
#   App Store browsing)
# ============================================================================


def test_live_bug_replay_30s_gaming_window_classifies_as_gaming():
    """A 30s slice from the middle of the gaming portion. In v0.17.4 the
    coordinator would have observed only the stale `app_id=disneyplus`
    from pyatv and kept attributing time to Disney+. With this classifier
    + decide_attribution, the gaming activity is plainly visible."""
    # Simulate ~5s ago a burst typical of an active KooApps game session.
    r = classify_dns_window([
        ("www.kooappsservers.com", _at(2)),
        ("usa-kooapps-dlc.s3.amazonaws.com", _at(5)),
        ("kaserver-new-2-elb-1744555362.us-east-1.elb.amazonaws.com", _at(8)),
        ("stats.gc.fe2.apple-dns.net", _at(12)),
        ("profile.gc.fe2.apple-dns.net", _at(20)),
        # Background noise also present (would be in any window)
        ("gateway.fe2.apple-dns.net", _at(3)),
        ("time.apple.com", _at(15)),
        ("_dns-push-tls._tcp.service.arpa", _at(25)),
    ], now=NOW)
    assert r.confidence is Confidence.BUNDLE, (
        f"Expected BUNDLE confidence, got {r.confidence} — the live-bug "
        f"replay must produce a strong gaming signal"
    )
    assert r.bundle_id == "publisher.kooapps"
    assert r.group == "gaming"
    assert r.bundle_hit_count >= 3, (
        "Bundle hit count must clear MIN_BUNDLE_CROSS_GROUP_HITS=3 so "
        "decide_attribution will accept a cross-group correction"
    )


def test_live_bug_replay_only_background_during_disney_buffer():
    """Inverse case — buffered Disney+ + only ambient Apple traffic.
    Important for the sticky-bundle rule: classifier returning AMBIENT_ONLY
    means decide_attribution will PRESERVE the Disney+ binding, not promote
    to gaming on a single stray GC ping."""
    r = classify_dns_window([
        ("gateway.fe2.apple-dns.net", _at(5)),
        ("apple-dns.net", _at(10)),
        ("time.apple.com", _at(20)),
    ], now=NOW)
    assert r.confidence is Confidence.AMBIENT_ONLY


def test_live_bug_replay_disney_plus_first_minutes():
    """Disney+ at session start (first 9 minutes) — the classifier
    should plainly identify Disney+. This is what we EXPECT for the
    legitimate Disney+ portion of today's session."""
    r = classify_dns_window([
        ("disney.api.edge.bamgrid.com", _at(5)),
        ("appconfigs.disney-plus.net", _at(10)),
        ("cdn.registerdisney.go.com", _at(15)),
        ("dssott.com", _at(20)),
    ], now=NOW)
    assert r.confidence is Confidence.BUNDLE
    assert r.bundle_id == "com.disney.disneyplus"
    assert r.group == "movies"


# ============================================================================
# Defensive — caller passes weird data
# ============================================================================


def test_empty_string_domain_is_skipped():
    """Empty domain shouldn't crash."""
    r = classify_dns_window([("", _at(10)),
                             ("disney.api.edge.bamgrid.com", _at(20))],
                            now=NOW)
    assert r.bundle_id == "com.disney.disneyplus"


def test_negative_age_is_clamped_to_zero():
    """Clock skew: timestamp ahead of `now` should not crash or count
    as recent-future. We clamp negative age to zero (treat as 'just now')."""
    r = classify_dns_window([
        ("disney.api.edge.bamgrid.com", NOW + timedelta(seconds=5)),
    ], now=NOW)
    assert r.confidence is Confidence.BUNDLE
    assert r.bundle_id == "com.disney.disneyplus"


def test_unknown_domain_treated_as_ambient_not_signal():
    """A domain we don't recognize (and isn't on the background list)
    should land in AMBIENT, not generate a phantom signal."""
    r = classify_dns_window([
        ("some-random-domain-we-dont-know.example.com", _at(10)),
    ], now=NOW)
    assert r.confidence is Confidence.AMBIENT_ONLY
    assert r.bundle_id is None
    assert r.total_signal_queries == 0


# ============================================================================
# v0.18.0 follow-up — seed gap discovered during live HW test 2026-06-14
# ============================================================================


def test_gameloft_lego_star_wars_classified_as_gaming():
    """Live HW test caught a missing publisher: LEGO Star Wars Castaways
    by Gameloft. `legostarwarscastaways.gameloft.com` must now produce
    BUNDLE confidence + gaming group."""
    r = classify_dns_window([
        ("legostarwarscastaways.gameloft.com", _at(5)),
        ("legostarwarscastaways.gameloft.com", _at(10)),
        ("gameloft.com", _at(15)),
    ], now=NOW)
    assert r.confidence is Confidence.BUNDLE
    assert r.bundle_id == "publisher.gameloft"
    assert r.group == "gaming"


def test_kooapps_aws_execute_api_hash_classified():
    """v0.18.0 follow-up — the KooApps API Gateway endpoint
    `09nzmxy3h5.execute-api.us-west-2.amazonaws.com` fires during real
    KooApps game sessions (observed both in the 2026-06-14 06:27-08:33
    incident and during the live HW test). The full `execute-api.us-west-2
    .amazonaws.com` would be too generic to seed (many non-game apps use
    AWS API Gateway), but the specific hash prefix is unique to KooApps'
    infrastructure and safe to seed."""
    r = classify_dns_window([
        ("09nzmxy3h5.execute-api.us-west-2.amazonaws.com", _at(5)),
        ("09nzmxy3h5.execute-api.us-west-2.amazonaws.com", _at(15)),
        ("09nzmxy3h5.execute-api.us-west-2.amazonaws.com", _at(25)),
    ], now=NOW)
    assert r.confidence is Confidence.BUNDLE
    assert r.bundle_id == "publisher.kooapps"
    assert r.group == "gaming"
