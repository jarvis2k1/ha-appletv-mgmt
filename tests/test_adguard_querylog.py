"""Tests for v0.18.0 AdGuardClient.query_recent_dns — focused on the three
reviewer-flagged concerns: exact-IP filter, fail-open, recency cutoff."""
from __future__ import annotations

import re

import aiohttp
import pytest
from aioresponses import aioresponses

from custom_components.appletv_mgmt.adguard import AdGuardClient


# Helper — aioresponses matches URLs literally, INCLUDING query string. Since
# `query_recent_dns` adds `?search=…&limit=…`, we register with a regex that
# matches the path regardless of params.
QUERYLOG_PAT = re.compile(r"^http://adguard\.test:3000/control/querylog($|\?)")


async def _client():
    session = aiohttp.ClientSession()
    return session, AdGuardClient(session, "http://adguard.test:3000",
                                  api_key="testkey")


def _row(client_ip: str, domain: str, time_iso: str) -> dict:
    return {
        "client": client_ip,
        "time": time_iso,
        "question": {"name": domain, "type": "A"},
    }


# ============================================================================
# Happy path
# ============================================================================


async def test_returns_domain_timestamp_pairs_for_matching_client():
    async with aiohttp.ClientSession() as session:
        c = AdGuardClient(session, "http://adguard.test:3000", api_key="testkey")
        with aioresponses() as mock:
            mock.get(QUERYLOG_PAT, payload={"data": [
                _row("192.168.1.24", "disney.bamgrid.com", "2026-06-14T12:00:00+02:00"),
                _row("192.168.1.24", "kooappsservers.com", "2026-06-14T11:59:50+02:00"),
            ]})
            result = await c.query_recent_dns("192.168.1.24", since_seconds=300)
            assert len(result) == 2
            assert result[0] == ("disney.bamgrid.com", "2026-06-14T12:00:00+02:00")
            assert result[1] == ("kooappsservers.com", "2026-06-14T11:59:50+02:00")


# ============================================================================
# Exact-IP filter — the sibling-device leak fix
# ============================================================================


async def test_sibling_device_at_192_168_178_244_is_not_leaked_in():
    """`search=192.168.1.24` is a SUBSTRING filter on AdGuard's side, so
    it ALSO matches `192.168.1.244`, `192.168.1.249`, etc. — every
    sibling whose IP starts with the same prefix. The post-filter MUST
    drop rows that don't match exactly."""
    async with aiohttp.ClientSession() as session:
        c = AdGuardClient(session, "http://adguard.test:3000", api_key="testkey")
        with aioresponses() as mock:
            mock.get(QUERYLOG_PAT, payload={"data": [
                _row("192.168.1.24",  "disney.bamgrid.com",  "2026-06-14T12:00:00+02:00"),
                _row("192.168.1.244", "facebook.com",        "2026-06-14T12:00:01+02:00"),
                _row("192.168.1.245", "instagram.com",       "2026-06-14T12:00:02+02:00"),
                _row("192.168.1.24",  "kooappsservers.com",  "2026-06-14T12:00:03+02:00"),
            ]})
            result = await c.query_recent_dns("192.168.1.24", since_seconds=300)
            # Sibling-device noise (.244, .245) must NOT appear.
            domains = [d for d, _ in result]
            assert "facebook.com" not in domains
            assert "instagram.com" not in domains
            assert "disney.bamgrid.com" in domains
            assert "kooappsservers.com" in domains


# ============================================================================
# Fail-open — never surface AdGuard outages as "device offline"
# ============================================================================


async def test_returns_empty_on_http_500():
    """AdGuard error -> []. Caller treats as Confidence.NONE -> falls back
    to v0.17.3 behavior. We do NOT raise."""
    async with aiohttp.ClientSession() as session:
        c = AdGuardClient(session, "http://adguard.test:3000", api_key="testkey")
        with aioresponses() as mock:
            mock.get(QUERYLOG_PAT, status=500)
            result = await c.query_recent_dns("192.168.1.24")
            assert result == []


async def test_returns_empty_on_network_error():
    """Connection refused / DNS failure / etc. — fail-open."""
    async with aiohttp.ClientSession() as session:
        c = AdGuardClient(session, "http://adguard.test:3000", api_key="testkey")
        with aioresponses() as mock:
            mock.get(QUERYLOG_PAT, exception=aiohttp.ClientConnectionError())
            result = await c.query_recent_dns("192.168.1.24")
            assert result == []


async def test_returns_empty_on_timeout():
    """Slow AdGuard -> bounded by timeout_s -> []."""
    async with aiohttp.ClientSession() as session:
        c = AdGuardClient(session, "http://adguard.test:3000", api_key="testkey")
        with aioresponses() as mock:
            import asyncio
            mock.get(QUERYLOG_PAT, exception=asyncio.TimeoutError())
            result = await c.query_recent_dns(
                "192.168.1.24", timeout_s=0.1
            )
            assert result == []


async def test_returns_empty_on_unexpected_shape():
    """If AdGuard returns something that isn't `{data: [...]}` we fail-open."""
    async with aiohttp.ClientSession() as session:
        c = AdGuardClient(session, "http://adguard.test:3000", api_key="testkey")
        with aioresponses() as mock:
            mock.get(QUERYLOG_PAT, payload={"unexpected": "shape"})
            result = await c.query_recent_dns("192.168.1.24")
            assert result == []


# ============================================================================
# Recency cutoff
# ============================================================================


async def test_stops_walking_at_recency_cutoff():
    """AdGuard returns newest-first; we walk until we cross since_seconds.
    Older rows must not appear in the result."""
    async with aiohttp.ClientSession() as session:
        c = AdGuardClient(session, "http://adguard.test:3000", api_key="testkey")
        with aioresponses() as mock:
            mock.get(QUERYLOG_PAT, payload={"data": [
                _row("192.168.1.24", "now.com",         "2026-06-14T12:00:00+02:00"),
                _row("192.168.1.24", "100s-ago.com",    "2026-06-14T11:58:20+02:00"),
                _row("192.168.1.24", "200s-ago.com",    "2026-06-14T11:56:40+02:00"),
                _row("192.168.1.24", "400s-ago.com",    "2026-06-14T11:53:20+02:00"),  # outside 300s
                _row("192.168.1.24", "1000s-ago.com",   "2026-06-14T11:43:20+02:00"),
            ]})
            result = await c.query_recent_dns(
                "192.168.1.24", since_seconds=300)
            domains = [d for d, _ in result]
            assert "now.com" in domains
            assert "100s-ago.com" in domains
            assert "200s-ago.com" in domains
            assert "400s-ago.com" not in domains  # past cutoff
            assert "1000s-ago.com" not in domains


async def test_malformed_timestamp_skipped_not_crashed():
    """A row with a bad `time` should not crash the call — just skip it."""
    async with aiohttp.ClientSession() as session:
        c = AdGuardClient(session, "http://adguard.test:3000", api_key="testkey")
        with aioresponses() as mock:
            mock.get(QUERYLOG_PAT, payload={"data": [
                _row("192.168.1.24", "good.com",  "2026-06-14T12:00:00+02:00"),
                {"client": "192.168.1.24", "time": "not-a-date",
                 "question": {"name": "bad.com"}},
                _row("192.168.1.24", "good2.com", "2026-06-14T11:59:00+02:00"),
            ]})
            result = await c.query_recent_dns("192.168.1.24")
            domains = [d for d, _ in result]
            assert "good.com" in domains
            assert "good2.com" in domains
            assert "bad.com" not in domains
