"""Minimal async REST client for AdGuard Home.

Stand-alone — only depends on `aiohttp` so it can be unit-tested without
Home Assistant. Used by `enforcer.EnforcementController`.

AdGuard Home's `POST /control/clients/update` requires the **full** client
object in the `data` block; we always fetch the current client first and
only mutate the two fields we care about (`use_global_blocked_services`
and `blocked_services`). Known quirk: `name` must be present in the data
block, not just the outer envelope.
"""
from __future__ import annotations

import base64
from typing import Any

import aiohttp


class AdGuardError(RuntimeError):
    """Raised when AdGuard Home returns an unexpected response."""


class AdGuardClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        username: str | None = None,
        password: str | None = None,
        api_key: str | None = None,
    ) -> None:
        """Build the client.

        Three auth modes:
        * `api_key`               -> sends `X-API-Key` (use this when talking to
                                     the appletv_adguard_proxy addon).
        * `username` + `password` -> sends HTTP Basic (direct AdGuard with auth).
        * neither                 -> no auth header (direct AdGuard without auth).

        `api_key` wins if both are supplied.
        """
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            self._headers["X-API-Key"] = api_key
        elif username and password:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            self._headers["Authorization"] = f"Basic {token}"
        # TTL cache for list_known_services. AdGuard's service catalog
        # changes only when AdGuard itself is upgraded; refetching on
        # every block transition cost ~50ms per call. Cached 1 hour.
        # (QA v0.10 P1 #8.)
        self._services_cache: list[str] | None = None
        self._services_cache_at: float = 0.0
        self._services_cache_ttl_s: float = 3600.0

    async def get_client(self, name: str) -> dict[str, Any]:
        url = f"{self._base_url}/control/clients"
        async with self._session.get(url, headers=self._headers, timeout=10) as resp:
            if resp.status != 200:
                raise AdGuardError(f"GET /clients returned {resp.status}")
            payload = await resp.json()

        for client in payload.get("clients", []) or []:
            if client.get("name") == name:
                return client
        raise AdGuardError(f"AdGuard client {name!r} not found")

    async def update_client(self, name: str, data: dict[str, Any]) -> None:
        url = f"{self._base_url}/control/clients/update"
        body = {"name": name, "data": data}
        async with self._session.post(
            url, headers=self._headers, json=body, timeout=10
        ) as resp:
            if resp.status not in (200, 204):
                text = await resp.text()
                raise AdGuardError(
                    f"POST /clients/update returned {resp.status}: {text}"
                )

    async def list_known_services(self) -> list[str]:
        """Return AdGuard's full list of blockable service IDs.

        Used by `set_blocked()` to construct a "block everything" list since
        AdGuard v0.107.x no longer accepts the "*" wildcard. Cached
        per-instance for `_services_cache_ttl_s` seconds (QA v0.10 P1 #8).
        """
        import time as _t

        if (
            self._services_cache is not None
            and (_t.monotonic() - self._services_cache_at) < self._services_cache_ttl_s
        ):
            return list(self._services_cache)

        url = f"{self._base_url}/control/blocked_services/services"
        async with self._session.get(url, headers=self._headers, timeout=10) as resp:
            if resp.status != 200:
                raise AdGuardError(
                    f"GET /blocked_services/services returned {resp.status}"
                )
            payload = await resp.json()
        if isinstance(payload, list):
            services = [str(s) for s in payload]
        elif isinstance(payload, dict):
            # Newer AdGuard versions wrap in {"blocked_services": [...]}.
            raw = payload.get("blocked_services") or payload.get("services") or []
            services = [str(s) for s in raw]
        else:
            raise AdGuardError(
                f"Unexpected blocked_services response shape: {payload!r}"
            )
        self._services_cache = services
        self._services_cache_at = _t.monotonic()
        return list(services)

    async def query_recent_dns(
        self,
        client_ip: str,
        *,
        limit: int = 200,
        timeout_s: float = 2.0,
        since_seconds: int = 300,
    ) -> list[tuple[str, str]]:
        """v0.18.0 — return a list of `(domain, iso_timestamp)` for DNS
        queries from `client_ip` within the last `since_seconds`.

        Used by the v0.18.0 attribution corroboration: when pyatv is
        push-silent we need to know what the Apple TV is actually doing on
        the network, and the AdGuard query log is the source of truth.

        Critical design points:
        - **Exact-IP filter.** AdGuard's `search=` parameter is a free-text
          substring filter — `search=192.168.1.24` ALSO matches
          `192.168.1.244` (the sibling-device DNS leak the workflow review
          identified). This method post-filters by exact client IP equality
          after the fetch.
        - **Fail-open.** Any network error / timeout / unexpected response
          returns `[]`. Callers (decide_attribution) treat that as
          `Confidence.NONE` and fall back to v0.17.3 behavior. We must NOT
          surface AdGuard outages as "device is offline."
        - **Bounded.** The `timeout_s` parameter caps the call — the
          coordinator tick runs every 30s and a slow AdGuard call would
          back up everything else. 2s is generous on LAN (~10-50ms typical).
        - **Recency filter.** AdGuard returns newest-first; we walk the
          response and stop once we cross `since_seconds`, so we don't pay
          for parsing rows we'll discard.

        Returns a list of `(domain, iso_timestamp)` tuples, newest first.
        The caller is responsible for converting the iso string to datetime.
        """
        url = f"{self._base_url}/control/querylog"
        # We pass `search` for the prefilter (it's cheap server-side and
        # narrows the response), but also post-filter by exact IP equality
        # to defuse the substring-prefix collision.
        params = {"search": client_ip, "limit": str(limit)}
        try:
            async with self._session.get(
                url,
                headers=self._headers,
                params=params,
                timeout=timeout_s,
            ) as resp:
                if resp.status != 200:
                    return []
                payload = await resp.json()
        except Exception:  # noqa: BLE001 — fail-open by design
            return []

        # AdGuard returns `{"data": [...]}` newest-first. Each row has
        # `client`, `time` (ISO 8601 with tz), and `question.name`.
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []

        # Compute cutoff in the same format AdGuard uses. We can't trust
        # AdGuard's clock to match ours so we compare lexicographically on
        # the timestamp string after slicing off TZ — within the same
        # logfile, AdGuard's timestamps are monotonically decreasing, so we
        # just stop once we cross `since_seconds` relative to the FIRST row
        # we see (newest). This is robust to clock drift between hosts.
        out: list[tuple[str, str]] = []
        from datetime import datetime, timedelta

        newest_ts: datetime | None = None
        cutoff: datetime | None = None
        for r in rows:
            client = r.get("client") if isinstance(r, dict) else None
            if client != client_ip:
                # Exact-IP filter — drops sibling-device noise.
                continue
            q = r.get("question") if isinstance(r, dict) else None
            name = q.get("name") if isinstance(q, dict) else None
            t = r.get("time") if isinstance(r, dict) else None
            if not name or not t:
                continue
            # Parse `t` so we can apply the recency cutoff. AdGuard emits
            # local-tz ISO ("...+02:00"); fromisoformat handles it natively.
            try:
                ts = datetime.fromisoformat(t)
            except (TypeError, ValueError):
                continue
            if newest_ts is None:
                newest_ts = ts
                cutoff = newest_ts - timedelta(seconds=since_seconds)
            if cutoff is not None and ts < cutoff:
                break  # walked past the recency window — stop parsing
            out.append((name, t))
        return out

    async def set_blocked(self, client_name: str, blocked: bool) -> None:
        """Block (or unblock) all services for a named client.

        AdGuard v0.107.74 dropped the "*" wildcard for `blocked_services`.
        To still emulate "block all", we fetch the live list of known
        service IDs and send every one of them. On unblock we send `[]`.
        """
        current = await self.get_client(client_name)
        updated = dict(current)
        updated["name"] = client_name  # known quirk: must be in data block
        updated["use_global_blocked_services"] = False
        if blocked:
            services = await self.list_known_services()
            updated["blocked_services"] = services
        else:
            updated["blocked_services"] = []
        await self.update_client(client_name, updated)
