"""Unit tests for the categorize module.

Covers:
- CURATED takes precedence over the iTunes cache.
- iTunes genre -> group mapping handles known + unknown genres.
- The async `lookup_itunes()` happy path + 404 + non-JSON failure modes.
- `categorize()` returns None for completely unknown bundles so the
  coordinator knows to trigger a lookup.
"""
import aiohttp
import pytest
from aioresponses import aioresponses

from custom_components.appletv_mgmt.categorize import (
    ALL_GROUPS,
    CURATED,
    GROUP_GAMING,
    GROUP_LINEAR_TV,
    GROUP_MOVIES,
    GROUP_OTHER,
    GROUP_TV_SHOWS,
    NATIVE_TV_BUNDLE_ID,
    categorize,
    group_from_curated,
    group_from_itunes_genre,
    lookup_itunes,
)


def test_all_groups_constant_is_complete_and_unique():
    # v0.21.0 — linear_tv (native TV / "Live TV") joins the group roster.
    assert set(ALL_GROUPS) == {
        GROUP_MOVIES,
        GROUP_TV_SHOWS,
        GROUP_GAMING,
        GROUP_OTHER,
        GROUP_LINEAR_TV,
    }
    # No dupes.
    assert len(ALL_GROUPS) == len(set(ALL_GROUPS))


def test_native_tv_bundle_maps_to_linear_tv():
    # v0.21.0 — the synthetic native-TV bundle is curated to the linear_tv group
    # so all generic per-group machinery (budgets, sensors, extensions) picks
    # it up without special-casing.
    assert NATIVE_TV_BUNDLE_ID == "tv.native"
    assert CURATED[NATIVE_TV_BUNDLE_ID] == GROUP_LINEAR_TV
    assert group_from_curated(NATIVE_TV_BUNDLE_ID) == GROUP_LINEAR_TV
    assert categorize(NATIVE_TV_BUNDLE_ID) == GROUP_LINEAR_TV


def test_curated_disney_is_movies():
    assert group_from_curated("com.disney.disneyplus") == GROUP_MOVIES


def test_curated_youtube_is_tv_shows():
    assert group_from_curated("com.google.ios.youtube") == GROUP_TV_SHOWS


def test_curated_returns_none_for_unknown():
    assert group_from_curated("com.example.unknown") is None


def test_curated_returns_none_for_blank_or_none():
    assert group_from_curated(None) is None
    assert group_from_curated("") is None


@pytest.mark.parametrize(
    "genre, expected",
    [
        ("Games", GROUP_GAMING),
        ("Action", GROUP_GAMING),
        ("Music", GROUP_OTHER),
        ("Entertainment", GROUP_TV_SHOWS),
        ("Sports", GROUP_TV_SHOWS),
        ("Education", GROUP_OTHER),
        ("Productivity", GROUP_OTHER),
        ("Something Apple Hasn't Invented Yet", GROUP_OTHER),
        ("", GROUP_OTHER),
        (None, GROUP_OTHER),
    ],
)
def test_itunes_genre_mapping(genre, expected):
    assert group_from_itunes_genre(genre) == expected


def test_categorize_curated_wins_over_cache():
    cache = {"com.disney.disneyplus": GROUP_OTHER}  # rogue cache entry
    assert categorize("com.disney.disneyplus", cache=cache) == GROUP_MOVIES


def test_categorize_falls_back_to_cache_when_uncurated():
    cache = {"com.example.app": GROUP_GAMING}
    assert categorize("com.example.app", cache=cache) == GROUP_GAMING


def test_categorize_returns_none_for_unknown():
    assert categorize("com.example.unknown") is None


def test_categorize_unknown_sentinel_is_other():
    # The coordinator emits "unknown" when pyatv lost the app id; we want
    # that to land in a real bucket so it still gets counted toward something.
    assert categorize("unknown") == GROUP_OTHER


# ---------- async iTunes lookup --------------------------------------------


@pytest.mark.asyncio
async def test_lookup_itunes_happy_path():
    async with aiohttp.ClientSession() as session:
        with aioresponses() as mock:
            mock.get(
                "https://itunes.apple.com/lookup?bundleId=com.example.someapp&country=US&media=software",
                payload={"resultCount": 1, "results": [{"primaryGenreName": "Games"}]},
            )
            group = await lookup_itunes(session, "com.example.someapp")
            assert group == GROUP_GAMING


@pytest.mark.asyncio
async def test_lookup_itunes_empty_results_returns_none():
    async with aiohttp.ClientSession() as session:
        with aioresponses() as mock:
            mock.get(
                "https://itunes.apple.com/lookup?bundleId=com.example.unknown&country=US&media=software",
                payload={"resultCount": 0, "results": []},
            )
            group = await lookup_itunes(session, "com.example.unknown")
            assert group is None


@pytest.mark.asyncio
async def test_lookup_itunes_404_returns_none():
    async with aiohttp.ClientSession() as session:
        with aioresponses() as mock:
            mock.get(
                "https://itunes.apple.com/lookup?bundleId=com.example.app&country=US&media=software",
                status=404,
            )
            group = await lookup_itunes(session, "com.example.app")
            assert group is None


@pytest.mark.asyncio
async def test_lookup_itunes_connection_error_returns_none():
    async with aiohttp.ClientSession() as session:
        with aioresponses() as mock:
            mock.get(
                "https://itunes.apple.com/lookup?bundleId=com.example.app&country=US&media=software",
                exception=aiohttp.ClientError("dns fail"),
            )
            group = await lookup_itunes(session, "com.example.app")
            assert group is None


@pytest.mark.asyncio
async def test_lookup_itunes_handles_text_content_type():
    """iTunes sometimes returns application/javascript instead of JSON."""
    async with aiohttp.ClientSession() as session:
        with aioresponses() as mock:
            mock.get(
                "https://itunes.apple.com/lookup?bundleId=com.example.app&country=US&media=software",
                body='{"resultCount":1,"results":[{"primaryGenreName":"Entertainment"}]}',
                content_type="application/javascript",
            )
            group = await lookup_itunes(session, "com.example.app")
            assert group == GROUP_TV_SHOWS


# ---------- sanity ---------------------------------------------------------


def test_curated_only_contains_known_groups():
    """Guard against typos in CURATED introducing groups outside ALL_GROUPS."""
    bad = {bid: g for bid, g in CURATED.items() if g not in ALL_GROUPS}
    assert not bad, f"CURATED contains unknown groups: {bad}"
