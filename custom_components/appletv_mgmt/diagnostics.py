"""Diagnostics for Apple TV Mgmt.

Home Assistant auto-discovers `async_get_config_entry_diagnostics` — the
"Download diagnostics" button on the config-entry page calls it. This gives
another parent a one-click, REDACTED snapshot to attach to a bug report,
instead of hand-copying config + state (which is how sensitive values leak).

Redaction covers the AdGuard credentials + URL, the REST api_key, the Apple
TV LAN IP, and the parent-notify target (a mobile_app device name). Entity
ids and budgets are kept — they're what makes a report actionable and aren't
secrets.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

TO_REDACT = {
    "adguard_api_key",
    "adguard_password",
    "adguard_username",
    "adguard_url",
    "api_key",
    "apple_tv_ip",
    "notify_parent_target",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    diag: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
    }

    bundle = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if bundle is None:
        diag["note"] = "config entry not set up (no runtime bundle)"
        return diag

    profile = bundle.get("profile")
    if profile is not None and hasattr(profile, "to_dict"):
        diag["profile"] = async_redact_data(profile.to_dict(), TO_REDACT)

    coordinator = bundle.get("coordinator")
    if coordinator is not None:
        diag["coordinator_snapshot"] = async_redact_data(
            dict(getattr(coordinator, "data", None) or {}), TO_REDACT
        )

    enforcer = bundle.get("enforcer")
    if enforcer is not None:
        diag["enforcer"] = {
            "state": getattr(enforcer, "state", None),
            "enforce_reason": getattr(enforcer, "enforce_reason", None),
            "is_blocked": getattr(enforcer, "is_blocked", None),
            "effective_remaining_seconds": getattr(
                enforcer, "effective_remaining_seconds", None
            ),
            "last_enforcement_failed": getattr(
                enforcer, "_last_enforcement_failed", None
            ),
        }

    # Store SUMMARY only — counts, never raw usage events / audit rows (which
    # would carry timestamps + app history for a specific child).
    store = bundle.get("store")
    if store is not None:
        diag["store_summary"] = {
            "profiles": len(getattr(store, "_profiles", {}) or {}),
            "events": len(getattr(store, "_events", []) or []),
            "requests": len(getattr(store, "_requests", {}) or {}),
            "action_log": len(getattr(store, "_actions", []) or []),
        }

    return diag
