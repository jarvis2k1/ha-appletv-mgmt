"""v0.18.0 — DNS-based app/group classifier (pure module, HA-free).

Background
----------
pyatv on tvOS 26 + Apple TV 4K goes push-silent during steady playback. The
HA `media_player.living_room_apple_tv` entity stops emitting state-change events for
minutes-to-hours during real use, leaving the integration's `current_bundle_id`
frozen at whatever pyatv last reported. v0.17.3 fixed time-counting via the
Samsung-on liveness gate (the clock keeps ticking through pyatv silence), but
it did NOT fix bundle attribution: if the kid switches Disney+ -> a game while
pyatv is silent, the entire continuing session is still booked as Disney+.

Live evidence (2026-06-14): a 126.7-min session attributed to Disney+ was, per
the AdGuard DNS log, ~9 min Disney+ + ~117 min KooApps/Perchang games + Apple
Game Center. The wrong budget got burned (movies); the gaming budget showed
as still available. This module exists to corroborate pyatv with the network
behavior of the Apple TV's IP.

Design
------
- Pure: takes a list of `(domain, timestamp)` tuples and a `now` reference;
  returns a `DnsClassification(bundle_id, group, confidence, matched_domains)`.
- HA-free: no `homeassistant.*` imports. Unit-testable in a vanilla venv.
- Strict allow-lists for "background noise" domains (Apple keepalive, NTP,
  Game Center heartbeat that fires on standby) so AMBIENT vs NONE is
  distinguishable. NONE = no DNS at all; AMBIENT_ONLY = device is online but
  no app-foreground traffic visible.
- Confidence hierarchy: BUNDLE > GROUP_ONLY > AMBIENT_ONLY > NONE. Callers
  use confidence + count to decide whether to act.
- Recency tiers: BUNDLE rules require recent (>= 1 hit within
  `bundle_recency_s`, default 60s — streaming apps stay chatty); GROUP_ONLY
  rules use the wider `group_recency_s` (default 180s — game backends ping
  less often).
- Bundle correction across-group requires `MIN_BUNDLE_CROSS_GROUP_HITS = 3`
  hits to defuse single-noisy-query false positives.

This module ONLY classifies the signal. The decision to actually correct an
open event is in `media_attribution.decide_attribution` (which composes this
with pyatv freshness and other context).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


class Confidence(Enum):
    """How sure we are the device is doing app-foreground work.

    Ordering matters: callers MAY compare `>= Confidence.GROUP_ONLY`.
    """
    NONE = 0           # zero DNS in the window
    AMBIENT_ONLY = 1   # only background keepalive / OS chatter
    GROUP_ONLY = 2     # group-level rule hit (e.g. Game Center, generic game SDK)
    BUNDLE = 3         # a specific app's CDN was hit (Disney+/Netflix/...)


@dataclass(frozen=True)
class DnsClassification:
    """Result of `classify_dns_window`.

    - `bundle_id`: a specific app bundle id when confidence == BUNDLE.
      `None` otherwise.
    - `group`: one of `movies` / `tv_shows` / `gaming` / `other` when we
      have at least GROUP_ONLY confidence. `None` for NONE/AMBIENT_ONLY.
    - `confidence`: see `Confidence`.
    - `matched_domains`: the domains that contributed to the decision,
      most-recent first (for audit / sensor display).
    - `bundle_hit_count`: how many distinct hits to the WINNING bundle's
      domain set (used by `decide_attribution` to gate cross-group
      corrections behind `MIN_BUNDLE_CROSS_GROUP_HITS`).
    - `group_hit_count`: same for the winning group.
    - `total_signal_queries`: domains that matched ANY non-background rule
      (used by `decide_attribution` to distinguish ambient from quiet).
    """
    bundle_id: str | None
    group: str | None
    confidence: Confidence
    matched_domains: tuple[str, ...] = field(default_factory=tuple)
    bundle_hit_count: int = 0
    group_hit_count: int = 0
    total_signal_queries: int = 0


# ---------------------------------------------------------------------------
# Domain rules — seed list
# ---------------------------------------------------------------------------
#
# Each rule is a tuple `(substrings, attribution)` where:
# - `substrings`: any of these found in the lowercased domain triggers the rule.
# - `attribution`: a `(bundle_id, group)` pair, OR `(None, group)` for
#   group-only rules (e.g. Apple Game Center heartbeat — tells us "something
#   gaming is happening on this device" but not WHICH app).
#
# Order doesn't matter; specificity is determined by attribution type
# (bundle > group_only). Substring matches are case-insensitive and use a
# simple `in` check — fine because all entries are already specific enough
# that false matches are highly unlikely.
#
# Seeded from real observed traffic on the owner's install (last 14 days of
# usage events + today's DNS log) plus the curated mapping in categorize.py.

# Group constants — kept aligned with categorize.py.
GROUP_MOVIES = "movies"
GROUP_TV_SHOWS = "tv_shows"
GROUP_GAMING = "gaming"
GROUP_OTHER = "other"


# BUNDLE rules: domain substrings -> (bundle_id, group)
BUNDLE_RULES: tuple[tuple[tuple[str, ...], tuple[str, str]], ...] = (
    # ----- Streaming: movies -----
    (
        ("bamgrid.com", "dssott.com", "disney-plus.net", "disneyplus.com",
         "cdn.registerdisney.go.com", "dss-eu.map.fastly.net"),
        ("com.disney.disneyplus", GROUP_MOVIES),
    ),
    (
        ("nflxvideo.net", "nflximg.net", "nflxext.com", "nflxso.net",
         "netflix.com", "ftl.netflix.com"),
        ("com.netflix.Netflix", GROUP_MOVIES),
    ),
    (
        ("aiv-cdn.net", "atv-ps.amazon.com", "atv-ext.amazon.com",
         "pv-cdn.net", "amazonvideo.com", "primevideo.com"),
        ("com.amazon.aiv.AIVApp", GROUP_MOVIES),
    ),
    (
        ("plex.tv", "plex.direct"),
        ("tv.plex.player", GROUP_MOVIES),
    ),
    # ----- Streaming: TV / other live -----
    (
        ("googlevideo.com", "ytimg.com", "youtube.com",
         "youtubei.googleapis.com"),
        ("com.google.ios.youtube", GROUP_TV_SHOWS),
    ),
    (
        ("ttvnw.net", "twitch.tv", "twitchcdn.net"),
        ("tv.twitch", GROUP_TV_SHOWS),
    ),
    # ----- Self-hosted -----
    (
        ("jellyfin",),
        ("org.jellyfin.swiftfin", GROUP_MOVIES),
    ),
    # ----- German Mediatheken -----
    (
        ("zdf.de", "zdf-cdn", "zdf.tv"),
        ("de.zdf.zdfmediathek.tvos", GROUP_TV_SHOWS),
    ),
    (
        ("ardmediathek.de", "ardmediathek-cdn"),
        ("de.ard.mediathek.tvos", GROUP_TV_SHOWS),
    ),
    (
        ("rtl.de", "rtl-tv.de"),
        ("de.rtl.now", GROUP_TV_SHOWS),
    ),
    (
        ("joyn.de", "joyn-cdn"),
        ("tv.joyn.app", GROUP_TV_SHOWS),
    ),
    (
        ("7tv.de", "p7s1-tv"),
        ("de.prosiebensat1.app7tv", GROUP_TV_SHOWS),
    ),
    (
        ("sky.de", "skyq", "skygo"),
        ("de.skygo.skygo", GROUP_MOVIES),
    ),
    (
        ("dazn.com", "dazn.de", "dazn-cdn"),
        ("de.dazn.dazn", GROUP_MOVIES),
    ),
    # ----- Games: known publishers -----
    (
        # KooApps publishes many games; we can identify the publisher but
        # not the specific title from DNS alone. `kaserver` is a distinctive
        # KooApps backend prefix that appears in dynamic ELB hostnames
        # without the `kooapps` substring (observed: kaserver-new-2-elb-
        # 1744555362.us-east-1.elb.amazonaws.com). `nakiostudio` is the
        # backend domain used by their UHF cable-TV game
        # (com.nakiostudio.uhf.ios, played 51 min in the prior 14d).
        # `09nzmxy3h5.execute-api.us-west-2.amazonaws.com` is a specific
        # API Gateway identifier observed in the owner's AdGuard log
        # during both the 2026-06-14 06:27-08:33 incident AND a live HW
        # test re-launch of the same game — too generic to seed via
        # `execute-api` alone (would match many non-games), but the
        # exact hash prefix is safe to seed.
        ("kooappsservers.com", "kooapps-dlc", "kooapps.com",
         "kaserver-", "nakiostudio",
         "09nzmxy3h5.execute-api"),
        ("publisher.kooapps", GROUP_GAMING),
    ),
    (
        ("perchangdata.net", "perchang.com"),
        ("publisher.perchang", GROUP_GAMING),
    ),
    (
        # Gameloft — major tvOS/iOS game publisher (Asphalt, LEGO Star
        # Wars Castaways, Disney Speedstorm, Modern Combat, etc.).
        # Discovered during live HW test on 2026-06-14 when the owner
        # launched LEGO Star Wars Castaways: `legostarwarscastaways.
        # gameloft.com` fired but missed the seed list.
        ("gameloft.com",),
        ("publisher.gameloft", GROUP_GAMING),
    ),
)


# GROUP_ONLY rules: domain substrings -> group
# These fire when we can tell the user is in a category but not which app.
# CRITICAL: decide_attribution will refuse to correct CURATED streaming
# bundles based on these alone (e.g. Disney+ + Game Center heartbeat is
# common and must not flip to gaming).
GROUP_ONLY_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    # Apple Game Center — fires whenever any GC-enabled app is foreground
    # (and occasionally on standby). Strongest "this is gaming" signal that
    # isn't tied to a specific game.
    (
        ("gc.fe2.apple-dns.net", "gc.fe.apple-dns.net",
         "stats.gc.fe", "profile.gc.fe", "challenge.gc.fe"),
        GROUP_GAMING,
    ),
    # Game-engine + ad/analytics SDKs that virtually only appear in games.
    (
        ("unity3d.com", "unityads.unity3d.com", "config.unity3d.com",
         "playfab.com", "playfabapi.com",
         "chartboost.com", "applovin.com", "ironsrc.mobi",
         "vungle.com", "adcolony.com",
         "supercell.com", "scgs.io", "supercellgames.com",
         "miniclippt.com", "miniclip.com",
         "rovio.com", "rovioaccount.com"),
        GROUP_GAMING,
    ),
)


# Background-noise prefixes/substrings. Domains matching these are NOT
# counted as signal at all (they fire on standby AND during use, so are
# useless for foreground app detection). Crucially: this is the bar
# between Confidence.NONE and Confidence.AMBIENT_ONLY.
BACKGROUND_NOISE: tuple[str, ...] = (
    # Apple keepalive / push / iCloud
    "apple-dns.net",        # subset; GC subdomains override above
    "icloud.com",
    "push.apple.com",
    "courier.push.apple",
    "time.apple.com",
    "time-ios.apple.com",
    "captive.apple.com",
    "mesu.apple.com",
    "init-p01md",
    "init.itunes.apple",
    "gsp",
    "ocsp",
    "doh.dns.apple",
    "pancake.apple.com",
    "guzzoni",                          # Siri DNS
    "xp.itunes-apple",                  # iTunes metrics
    "xp.apple.com",
    # App Store browsing — distinguish from app use
    "mzstatic.com",                     # App Store artwork CDN
    "amp-api-edge.apps.apple.com",      # App Store API
    "amp-api-search-edge",              # App Store search
    "is-ssl.mzstatic",
    "buy.itunes.apple",
    "p.itunes.apple",
    # OS / network plumbing
    "_dns-push-tls._tcp",
    "_matter._tcp",
    "_companion-link._tcp",
    "_airplay._tcp",
    "arpa",
    "_homekit._tcp",
    # NTP / analytics that aren't app-foreground
    "pool.ntp.org",
    "ntp.org",
    "datadog",
    "onetrust",
)


# Tunables — single source of truth, easy to reference from tests.
DEFAULT_BUNDLE_RECENCY_S = 60      # streaming/game DNS within this window = BUNDLE confidence
DEFAULT_GROUP_RECENCY_S = 180      # GROUP_ONLY rules use this wider window
MIN_BUNDLE_CROSS_GROUP_HITS = 3    # how many hits required to CROSS group via BUNDLE correction


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_background(domain: str) -> bool:
    """True iff the domain matches any background-noise pattern.

    Order matters: GC subdomains (`gc.fe2.apple-dns.net`) are checked by
    `GROUP_ONLY_RULES` BEFORE this; this function is only called for the
    background-vs-signal distinction at the bottom level.
    """
    d = domain.lower()
    return any(b in d for b in BACKGROUND_NOISE)


def _bundle_hit(domain: str) -> tuple[str, str] | None:
    """Return `(bundle_id, group)` if any BUNDLE rule matches, else None."""
    d = domain.lower()
    for substrings, attribution in BUNDLE_RULES:
        if any(s in d for s in substrings):
            return attribution
    return None


def _group_hit(domain: str) -> str | None:
    """Return group string if any GROUP_ONLY rule matches, else None."""
    d = domain.lower()
    for substrings, group in GROUP_ONLY_RULES:
        if any(s in d for s in substrings):
            return group
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_dns_window(
    domains_with_times: Iterable[tuple[str, datetime]],
    *,
    now: datetime,
    bundle_recency_s: int = DEFAULT_BUNDLE_RECENCY_S,
    group_recency_s: int = DEFAULT_GROUP_RECENCY_S,
) -> DnsClassification:
    """Classify a window of `(domain, timestamp)` tuples into a single
    decision about what's currently happening on the device.

    Algorithm
    ---------
    1. For each (domain, ts), check BUNDLE rules (within `bundle_recency_s`)
       and GROUP_ONLY rules (within `group_recency_s`). Background-noise
       domains are skipped at the signal-counting stage.
    2. The WINNING bundle is the one with the most hits within
       `bundle_recency_s`. Ties broken by recency of most-recent hit.
    3. The WINNING group is computed across BOTH BUNDLE-rule hits (which
       imply a group) and GROUP_ONLY-rule hits. Most hits wins.
    4. Confidence:
       - BUNDLE if any BUNDLE rule fired within recency
       - GROUP_ONLY if no BUNDLE but at least one GROUP_ONLY rule within recency
       - AMBIENT_ONLY if neither, but at least one background-noise domain saw
       - NONE if no domains at all (the window is empty)

    Inputs are non-negative ages (clamped by the coordinator before calling
    here). This function does NOT decide whether to ACT — it just describes
    what it sees. See `media_attribution.decide_attribution` for the action.
    """
    bundle_recency = timedelta(seconds=bundle_recency_s)
    group_recency = timedelta(seconds=group_recency_s)

    # Tally per-bundle and per-group within their respective windows.
    bundle_hits: dict[str, list[tuple[str, datetime]]] = {}
    bundle_group: dict[str, str] = {}   # bundle_id -> group (consistent per BUNDLE_RULES)
    group_hits: dict[str, list[tuple[str, datetime]]] = {}
    total_signal = 0
    saw_any_background = False
    saw_any_domain = False

    # Pre-sort newest-first so matched_domains comes out in a useful order.
    items = sorted(
        ((d.strip().lower(), t) for d, t in domains_with_times if d),
        key=lambda x: x[1],
        reverse=True,
    )

    for domain, ts in items:
        saw_any_domain = True
        age = now - ts
        # negative-age guard (caller should clamp; defensive here too)
        if age.total_seconds() < 0:
            age = timedelta(0)

        # Check BUNDLE rule first (most specific).
        bundle_attr = _bundle_hit(domain)
        if bundle_attr is not None and age <= bundle_recency:
            bundle_id, group = bundle_attr
            bundle_hits.setdefault(bundle_id, []).append((domain, ts))
            bundle_group[bundle_id] = group
            # A BUNDLE hit also counts toward its group.
            group_hits.setdefault(group, []).append((domain, ts))
            total_signal += 1
            continue

        # GROUP_ONLY rule.
        group = _group_hit(domain)
        if group is not None and age <= group_recency:
            group_hits.setdefault(group, []).append((domain, ts))
            total_signal += 1
            continue

        # Neither: background noise (still tells us the device exists)
        # or unknown signal (treated as ambient — we'd rather miss it
        # than over-attribute).
        if _is_background(domain):
            saw_any_background = True

    if not saw_any_domain:
        return DnsClassification(
            bundle_id=None,
            group=None,
            confidence=Confidence.NONE,
        )

    if bundle_hits:
        # Pick winning bundle: most hits, tiebreak by recency.
        def _bundle_score(item: tuple[str, list[tuple[str, datetime]]]):
            bid, hits = item
            return (len(hits), max(t for _, t in hits))

        winning_bundle, winning_hits = max(bundle_hits.items(), key=_bundle_score)
        winning_group = bundle_group[winning_bundle]
        # group_hit_count combines BUNDLE-implied + GROUP_ONLY hits for that group
        gcount = len(group_hits.get(winning_group, []))
        matched = tuple(d for d, _ in winning_hits)
        return DnsClassification(
            bundle_id=winning_bundle,
            group=winning_group,
            confidence=Confidence.BUNDLE,
            matched_domains=matched,
            bundle_hit_count=len(winning_hits),
            group_hit_count=gcount,
            total_signal_queries=total_signal,
        )

    if group_hits:
        # Pick winning group: most hits, tiebreak by recency.
        def _group_score(item: tuple[str, list[tuple[str, datetime]]]):
            g, hits = item
            return (len(hits), max(t for _, t in hits))

        winning_group, winning_hits = max(group_hits.items(), key=_group_score)
        matched = tuple(d for d, _ in winning_hits)
        return DnsClassification(
            bundle_id=None,
            group=winning_group,
            confidence=Confidence.GROUP_ONLY,
            matched_domains=matched,
            bundle_hit_count=0,
            group_hit_count=len(winning_hits),
            total_signal_queries=total_signal,
        )

    # No signal rules fired. Did we see anything at all?
    if saw_any_background:
        return DnsClassification(
            bundle_id=None,
            group=None,
            confidence=Confidence.AMBIENT_ONLY,
            total_signal_queries=0,
        )

    # We saw domains but none classified — treat as AMBIENT (less surprising
    # than NONE; the device is online and doing SOMETHING).
    return DnsClassification(
        bundle_id=None,
        group=None,
        confidence=Confidence.AMBIENT_ONLY,
        total_signal_queries=0,
    )
