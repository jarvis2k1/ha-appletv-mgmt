"""Tests for the AdGuard REST client.

Verifies the exact request shape AdGuard expects — especially the known
quirk that `name` must appear in both the envelope and the `data` block,
and that `update_client` always sends the full client doc back so we
don't accidentally wipe other fields.
"""
import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from custom_components.appletv_mgmt.adguard import AdGuardClient, AdGuardError


CLIENT = {
    "name": "AppleTV-LivingRoom",
    "ids": ["AA:BB:CC:DD:EE:FF"],
    "use_global_blocked_services": True,
    "blocked_services": [],
    "tags": ["device_apple"],   # extra field we must preserve
    "filtering_enabled": True,
}


async def _make_client():
    session = aiohttp.ClientSession()
    return session, AdGuardClient(session, "http://adguard:3000", "admin", "secret")


async def test_get_client_finds_named_entry():
    async with aiohttp.ClientSession() as session:
        client = AdGuardClient(session, "http://adguard:3000", "admin", "secret")
        with aioresponses() as mock:
            mock.get(
                "http://adguard:3000/control/clients",
                payload={"clients": [CLIENT, {"name": "Other"}]},
            )
            result = await client.get_client("AppleTV-LivingRoom")
            assert result == CLIENT


async def test_get_client_raises_when_not_found():
    async with aiohttp.ClientSession() as session:
        client = AdGuardClient(session, "http://adguard:3000", "admin", "secret")
        with aioresponses() as mock:
            mock.get(
                "http://adguard:3000/control/clients",
                payload={"clients": [{"name": "Other"}]},
            )
            with pytest.raises(AdGuardError, match="not found"):
                await client.get_client("AppleTV-LivingRoom")


async def test_get_client_raises_on_http_error():
    async with aiohttp.ClientSession() as session:
        client = AdGuardClient(session, "http://adguard:3000", "admin", "secret")
        with aioresponses() as mock:
            mock.get("http://adguard:3000/control/clients", status=401)
            with pytest.raises(AdGuardError, match="401"):
                await client.get_client("AppleTV-LivingRoom")


def _last_post_body(mock: aioresponses, url: str) -> dict:
    """Pull the json body from the most recent POST against `url`."""
    key = ("POST", URL(url))
    history = mock.requests[key]
    return history[-1].kwargs["json"]


async def test_set_blocked_sends_correct_payload_for_block():
    async with aiohttp.ClientSession() as session:
        client = AdGuardClient(session, "http://adguard:3000", "admin", "secret")
        with aioresponses() as mock:
            mock.get(
                "http://adguard:3000/control/clients",
                payload={"clients": [CLIENT]},
            )
            # v0.6.0 fetches the service list dynamically — return a fake one.
            mock.get(
                "http://adguard:3000/control/blocked_services/services",
                payload=["youtube", "netflix", "disneyplus"],
            )
            mock.post("http://adguard:3000/control/clients/update", status=200)
            await client.set_blocked("AppleTV-LivingRoom", True)

            body = _last_post_body(mock, "http://adguard:3000/control/clients/update")

        # Envelope.
        assert body["name"] == "AppleTV-LivingRoom"
        # Inner data block — name preserved (known quirk).
        assert body["data"]["name"] == "AppleTV-LivingRoom"
        # The two fields we manage.
        assert body["data"]["use_global_blocked_services"] is False
        # blocked_services now carries the full known-service list (not "*").
        assert body["data"]["blocked_services"] == ["youtube", "netflix", "disneyplus"]
        # Untouched fields preserved.
        assert body["data"]["ids"] == ["AA:BB:CC:DD:EE:FF"]
        assert body["data"]["tags"] == ["device_apple"]
        assert body["data"]["filtering_enabled"] is True


async def test_set_blocked_sends_correct_payload_for_unblock():
    async with aiohttp.ClientSession() as session:
        client = AdGuardClient(session, "http://adguard:3000", "admin", "secret")
        with aioresponses() as mock:
            mock.get(
                "http://adguard:3000/control/clients",
                payload={"clients": [CLIENT]},
            )
            mock.post("http://adguard:3000/control/clients/update", status=200)
            await client.set_blocked("AppleTV-LivingRoom", False)

            body = _last_post_body(mock, "http://adguard:3000/control/clients/update")

        assert body["data"]["blocked_services"] == []
        assert body["data"]["name"] == "AppleTV-LivingRoom"


async def test_set_blocked_raises_when_update_fails():
    async with aiohttp.ClientSession() as session:
        client = AdGuardClient(session, "http://adguard:3000", "admin", "secret")
        with aioresponses() as mock:
            mock.get(
                "http://adguard:3000/control/clients",
                payload={"clients": [CLIENT]},
            )
            mock.get(
                "http://adguard:3000/control/blocked_services/services",
                payload=["youtube"],
            )
            mock.post(
                "http://adguard:3000/control/clients/update",
                status=500,
                body="boom",
            )
            with pytest.raises(AdGuardError, match="500"):
                await client.set_blocked("AppleTV-LivingRoom", True)


def test_auth_header_is_basic():
    """The Authorization header must be `Basic <base64(user:pass)>` when creds given."""
    import asyncio
    import base64
    expected = "Basic " + base64.b64encode(b"admin:secret").decode()

    async def _run():
        async with aiohttp.ClientSession() as session:
            client = AdGuardClient(session, "http://adguard:3000", "admin", "secret")
            assert client._headers["Authorization"] == expected  # noqa: SLF001

    asyncio.run(_run())


def test_auth_header_omitted_when_no_credentials():
    """Ingress-only AdGuard installs have no HTTP auth — we must not send Basic."""
    import asyncio

    async def _run():
        async with aiohttp.ClientSession() as session:
            # All forms of "no creds" must skip both headers.
            for u, p in [(None, None), ("", ""), (None, "secret"), ("admin", None)]:
                client = AdGuardClient(session, "http://adguard:3000", u, p)
                assert "Authorization" not in client._headers, f"creds={u!r},{p!r}"  # noqa: SLF001
                assert "X-API-Key" not in client._headers, f"creds={u!r},{p!r}"  # noqa: SLF001

    asyncio.run(_run())


def test_api_key_sets_x_api_key_header():
    """When using the AppleTV-AdGuard-Proxy, we send X-API-Key, not Basic."""
    import asyncio

    async def _run():
        async with aiohttp.ClientSession() as session:
            client = AdGuardClient(
                session, "http://homeassistant.local:8101", api_key="sekrit"
            )
            assert client._headers["X-API-Key"] == "sekrit"  # noqa: SLF001
            assert "Authorization" not in client._headers  # noqa: SLF001

    asyncio.run(_run())


def test_api_key_wins_over_basic_when_both_supplied():
    """If a user fills in both auth modes, the proxy path wins (cleaner default)."""
    import asyncio

    async def _run():
        async with aiohttp.ClientSession() as session:
            client = AdGuardClient(
                session,
                "http://h:8101",
                username="admin",
                password="pw",
                api_key="sekrit",
            )
            assert client._headers["X-API-Key"] == "sekrit"  # noqa: SLF001
            assert "Authorization" not in client._headers  # noqa: SLF001

    asyncio.run(_run())


async def test_set_blocked_works_without_auth():
    """The block call still works against an unauthenticated AdGuard."""
    async with aiohttp.ClientSession() as session:
        client = AdGuardClient(session, "http://adguard:3000")  # no creds
        with aioresponses() as mock:
            mock.get(
                "http://adguard:3000/control/clients",
                payload={"clients": [CLIENT]},
            )
            mock.get(
                "http://adguard:3000/control/blocked_services/services",
                payload=["youtube"],
            )
            mock.post("http://adguard:3000/control/clients/update", status=200)
            await client.set_blocked("AppleTV-LivingRoom", True)
            body = _last_post_body(mock, "http://adguard:3000/control/clients/update")
        assert body["data"]["blocked_services"] == ["youtube"]
