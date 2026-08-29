"""App categorization — map a bundle_id to a group name.

Pure module (no Home Assistant imports). Two sources of truth, in order:

  1. CURATED — a static dict of `bundle_id → group` for the top Apple TV apps.
     Authoritative; ships in the code.
  2. iTunes Search API — for any bundle_id not in CURATED, the integration can
     call `lookup_itunes(session, bundle_id)` to fetch the App Store's
     `primaryGenreName` and map it through `ITUNES_GENRE_TO_GROUP`.

The integration is expected to cache iTunes results per `bundle_id` (in
`AppleTVMgmtStore.app_categories`) so we only hit Apple's API once per
unknown app.

Group names are simple lowercase strings, matching the keys used in
`Profile.groups`:

  - "movies"     — feature films, on-demand video that's primarily films
  - "tv_shows"   — episodic content, live TV, YouTube, Twitch
  - "gaming"     — anything you play
  - "other"      — music, utilities, education, and everything else
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)


GROUP_MOVIES = "movies"
GROUP_TV_SHOWS = "tv_shows"
GROUP_GAMING = "gaming"
GROUP_OTHER = "other"
# v0.21.0 — native TV watching ("Live TV"): the TV is ON but the input is
# NOT one of the tracked devices (Apple TV on HDMI1, Xbox on HDMI2/DVI) —
# i.e. tuner/broadcast, SCART, or a smart-TV app. Booked to this group under
# the same room budget. Opt-in (Profile.track_native_tv, default False).
GROUP_LINEAR_TV = "linear_tv"

ALL_GROUPS: tuple[str, ...] = (
    GROUP_MOVIES,
    GROUP_TV_SHOWS,
    GROUP_GAMING,
    GROUP_OTHER,
    GROUP_LINEAR_TV,
)


# v0.19.0 — synthetic bundle_id used by Xbox profiles to represent "the kid
# is on the Xbox right now". The Xbox MVP has no per-game signal (no Xbox
# Live integration assumed); every minute on the console is attributed
# against this single bundle_id. Routed to GROUP_GAMING via CURATED below.
XBOX_CONSOLE_BUNDLE_ID = "xbox.console"

# v0.21.0 — synthetic bundle_id for native TV watching (precedent:
# XBOX_CONSOLE_BUNDLE_ID). Emitted by the coordinator's room resolver when
# the TV is on with a non-tracked source. Routed to GROUP_LINEAR_TV via
# CURATED below so every generic per-group machinery (budgets, sensors,
# extensions) picks it up automatically.
NATIVE_TV_BUNDLE_ID = "tv.native"


# Sourced from the Apple TV app catalog. Edit by hand; takes precedence over
# the iTunes lookup. Lowercase comparison via `_norm()`.
CURATED: dict[str, str] = {
    # ----- Movies (primarily films) -----
    "com.amazon.aiv.AIVApp":                   GROUP_MOVIES,    # Prime Video
    "com.apple.TVMovies":                      GROUP_MOVIES,
    "com.apple.TVWatchList":                   GROUP_MOVIES,    # Apple TV app
    "com.disney.disneyplus":                   GROUP_MOVIES,
    "com.netflix.Netflix":                     GROUP_MOVIES,
    "tv.plex.player":                          GROUP_MOVIES,
    "com.jellyfin.jellyfin":                   GROUP_MOVIES,
    "org.jellyfin.expo-mobile":                GROUP_MOVIES,
    "com.paramountplus.ott":                   GROUP_MOVIES,
    "com.hulu.plus":                           GROUP_MOVIES,
    "com.skygo.SkyTicket":                     GROUP_MOVIES,
    "de.skygo.skygo":                          GROUP_MOVIES,
    "de.dazn.dazn":                            GROUP_MOVIES,    # arguably tv_shows for live sports
    # ----- TV shows / episodic / live -----
    "com.google.ios.youtube":                  GROUP_TV_SHOWS,
    "com.apple.TVShows":                       GROUP_TV_SHOWS,
    "tv.twitch":                               GROUP_TV_SHOWS,
    "de.zdf.zdfmediathek.tvos":                GROUP_TV_SHOWS,
    "de.ard.mediathek.tvos":                   GROUP_TV_SHOWS,
    "de.rtl.now":                              GROUP_TV_SHOWS,
    "de.prosiebensat1.app7tv":                 GROUP_TV_SHOWS,  # 7TV
    "tv.joyn.app":                             GROUP_TV_SHOWS,
    "com.cbsinteractive.cbsnews":              GROUP_TV_SHOWS,
    # ----- Gaming -----
    "com.apple.Arcade":                        GROUP_GAMING,
    "com.steam.link":                          GROUP_GAMING,
    "com.geforcenow":                          GROUP_GAMING,    # GeForce Now (if released for tvOS)
    # Most Apple Arcade titles will land here from iTunes auto-categorize.
    # v0.19.0 — Xbox MVP. The Xbox profile kind has no per-game tracking
    # (no Xbox Live integration is assumed); every minute on the Xbox is
    # bucketed against this synthetic bundle_id. Maps to gaming so per-
    # group budgets cover Xbox + Apple TV gaming together.
    XBOX_CONSOLE_BUNDLE_ID:                    GROUP_GAMING,
    # v0.21.0 — native TV watching (tuner / SCART / smart-TV app). Every
    # minute the TV is on with a non-tracked source is bucketed against this
    # synthetic bundle_id → the linear_tv group.
    NATIVE_TV_BUNDLE_ID:                       GROUP_LINEAR_TV,
    # ----- Music / other -----
    "com.apple.TVMusic":                       GROUP_OTHER,
    "com.spotify.client":                      GROUP_OTHER,
    "com.apple.podcasts":                      GROUP_OTHER,
    "com.apple.Fitness":                       GROUP_OTHER,
}


# iTunes Search API's `primaryGenreName` -> our group. Used when an app
# isn't in CURATED. Fallback is "other".
ITUNES_GENRE_TO_GROUP: dict[str, str] = {
    # Apple's iTunes/App Store taxonomy. Streaming apps mostly come back
    # as "Entertainment" which we route to tv_shows (a safe default — a
    # user can override per app via Profile.app_overrides in the future).
    "Games":            GROUP_GAMING,
    "Action":           GROUP_GAMING,
    "Adventure":        GROUP_GAMING,
    "Arcade":           GROUP_GAMING,
    "Board":            GROUP_GAMING,
    "Card":             GROUP_GAMING,
    "Casino":           GROUP_GAMING,
    "Casual":           GROUP_GAMING,
    "Family":           GROUP_GAMING,
    "Music":            GROUP_OTHER,
    "Photo & Video":    GROUP_TV_SHOWS,
    "Entertainment":    GROUP_TV_SHOWS,
    "Sports":           GROUP_TV_SHOWS,
    "News":             GROUP_TV_SHOWS,
    "Education":        GROUP_OTHER,
    "Kids":             GROUP_OTHER,
    "Lifestyle":        GROUP_OTHER,
    "Health & Fitness": GROUP_OTHER,
    "Reference":        GROUP_OTHER,
    "Utilities":        GROUP_OTHER,
    "Productivity":     GROUP_OTHER,
}


def _norm(bundle_id: str | None) -> str | None:
    """Bundle ids are case-sensitive per Apple — but iTunes is forgiving.
    We keep CURATED case-sensitive but trim whitespace."""
    return bundle_id.strip() if bundle_id else None


def group_from_curated(bundle_id: str | None) -> str | None:
    """Returns the curated group for `bundle_id`, or None if unknown."""
    bid = _norm(bundle_id)
    if not bid:
        return None
    return CURATED.get(bid)


def group_from_itunes_genre(genre: str | None) -> str:
    """Map an iTunes `primaryGenreName` to one of our groups. Falls back to 'other'."""
    if not genre:
        return GROUP_OTHER
    return ITUNES_GENRE_TO_GROUP.get(genre, GROUP_OTHER)


async def lookup_itunes(
    session: aiohttp.ClientSession,
    bundle_id: str,
    *,
    country: str = "US",
    timeout_s: float = 4.0,
) -> str | None:
    """Query Apple's iTunes Search API for the app's primary genre and map it
    to one of our groups.

    Returns the group name (e.g. "movies", "gaming"), or `None` on lookup
    failure / unknown bundle id. Callers should cache the result.

    https://performance-partners.apple.com/search-api
    """
    url = "https://itunes.apple.com/lookup"
    params = {"bundleId": bundle_id, "country": country, "media": "software"}
    try:
        async with session.get(url, params=params, timeout=timeout_s) as resp:
            if resp.status != 200:
                _LOGGER.debug("iTunes returned %s for %s", resp.status, bundle_id)
                return None
            payload: dict[str, Any] = await resp.json(content_type=None)
    except aiohttp.ClientError as err:
        _LOGGER.debug("iTunes lookup failed for %s: %s", bundle_id, err)
        return None

    results = payload.get("results") or []
    if not results:
        _LOGGER.debug("iTunes had no result for %s", bundle_id)
        return None

    genre = results[0].get("primaryGenreName")
    group = group_from_itunes_genre(genre)
    _LOGGER.info("iTunes categorized %s as %r (genre=%r)", bundle_id, group, genre)
    return group


def categorize(
    bundle_id: str | None,
    *,
    cache: dict[str, str] | None = None,
) -> str | None:
    """Synchronous categorize from CURATED + cached iTunes results.

    Returns the group name or `None` if completely unknown (caller should
    trigger an async iTunes lookup and feed the result back via `cache`).

    For known sentinels like `"unknown"` (device on, app unclear) and
    `None`, returns `"other"` and `None` respectively so the bucketing
    is well-defined.
    """
    bid = _norm(bundle_id)
    if bid is None:
        return None
    if bid == "unknown":
        return GROUP_OTHER
    curated = CURATED.get(bid)
    if curated:
        return curated
    if cache and bid in cache:
        return cache[bid]
    return None
