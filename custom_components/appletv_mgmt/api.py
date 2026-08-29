"""REST API for Apple TV Mgmt.

All endpoints are mounted under `/api/appletv_mgmt/*` via HA's
`HomeAssistantView` mechanism. Designed for external automation — the
primary consumer is OpenClaw running on the owner's Mac mini.

Auth (v0.22.0 — BREAKING, security):
- Every endpoint requires Home Assistant's own authentication. Callers send
  `Authorization: Bearer <HA long-lived access token>`; add-ons reach it
  through the Supervisor's `homeassistant_api` proxy, which injects valid
  auth on their behalf. `/health` and `/openapi.json` are covered too — they
  used to leak the version, profile count and full endpoint schema to
  anonymous callers.
- Previously these views set `requires_auth = False` and relied on a custom
  `api_key` gate that returned None when no key was configured, so a fresh
  install left the limit-changing endpoints wide open. That mechanism is
  gone; there is no integration-specific key any more.

Endpoints (full reference in `docs/API.md`):
  GET    /api/appletv_mgmt/health
  GET    /api/appletv_mgmt/openapi.json
  GET    /api/appletv_mgmt/profiles
  GET    /api/appletv_mgmt/profiles/{id}/status
  GET    /api/appletv_mgmt/profiles/{id}/usage         (today only; per-range, use /events)
  GET    /api/appletv_mgmt/profiles/{id}/groups
  GET    /api/appletv_mgmt/profiles/{id}/events?from=YYYY-MM-DD&to=YYYY-MM-DD
  GET    /api/appletv_mgmt/profiles/{id}/limits
  PATCH  /api/appletv_mgmt/profiles/{id}/limits
  POST   /api/appletv_mgmt/profiles/{id}/adult_mode      {"minutes": int?}
  DELETE /api/appletv_mgmt/profiles/{id}/adult_mode
  POST   /api/appletv_mgmt/profiles/{id}/extension       {"minutes": int}
  POST   /api/appletv_mgmt/profiles/{id}/request_extension {minutes, reason, bundle_id?}
  GET    /api/appletv_mgmt/requests/{id}
  POST   /api/appletv_mgmt/requests/{id}/decide          {"approve": bool, "minutes": int?}
  GET    /api/appletv_mgmt/profiles/{id}/requests?status=pending
  GET    /api/appletv_mgmt/profiles/{id}/actions?limit=N&from=&to=    (v0.12.0)
"""
from __future__ import annotations

import logging
import secrets
import time
from datetime import timedelta
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .categorize import XBOX_CONSOLE_BUNDLE_ID
from .const import (
    API_BASE,
    CONF_NOTIFY_TARGET,
    CONF_REQUEST_EXPIRE_MIN,
    CONF_SECONDARY_DEVICES,
    DEFAULT_REQUEST_EXPIRE_MIN,
    DEVICE_KIND_XBOX_PRESENCE,
    DEVICE_KINDS,
    DOMAIN,
    EVENT_LIMITS_UPDATED,
    EVENT_REQUEST_CREATED,
)
from .quiet import parse_windows, validate_windows_string
from .schedule import (
    WEEKDAYS,
    effective_daily_budget,
    effective_group_budgets,
    effective_quiet_windows_string,
    normalize_weekday_dict,
    normalize_weekday_group_dict,
    weekday_key,
)
from .audit import record_admin_action
from .notify import handle_external_decision, send_request_notification
from .storage import AppleTVMgmtStore, ExtensionRequest, Profile

_LOGGER = logging.getLogger(__name__)


# ---------- helpers --------------------------------------------------------




def _bundle_for(hass: HomeAssistant, profile_id: str) -> dict[str, Any] | None:
    """Look up our per-entry bundle by profile_id (== entry_id)."""
    for bundle in hass.data.get(DOMAIN, {}).values():
        if bundle["profile"].id == profile_id:
            return bundle
    return None


def _profile_summary(bundle: dict[str, Any]) -> dict[str, Any]:
    p: Profile = bundle["profile"]
    snap = bundle["coordinator"].data or {}
    used_s = int(snap.get("used_seconds_today") or 0)
    rem_s = int(snap.get("remaining_seconds_today") or 0)
    # `effective_budget_today_min` includes today's extension grant so
    # the invariant `remaining + used = effective_budget` holds. The
    # base `budget_today_min` (config value) is kept for callers that
    # want to render "60 + 15 extension = 75" separately. Fixed in
    # v0.10.0 (QA P1 #7).
    extension_min = int(snap.get("extension_minutes_today") or 0)
    # The snapshot's `today_budget_min` reflects the weekday override (if
    # any); fall back to the profile's base when the snapshot hasn't been
    # populated yet (first tick after restart).
    base_today_min = int(snap.get("today_budget_min") or p.daily_budget_min)
    return {
        "id": p.id,
        "display_name": p.display_name,
        "apple_tv_entity_id": p.apple_tv_entity_id,
        "state": snap.get("enforcement_state"),
        "enforce_reason": snap.get("enforce_reason"),
        "current_bundle_id": snap.get("current_bundle_id"),
        "current_group": snap.get("current_group"),
        # v0.12.2 — current session info for "Netflix — since 10:13 (12m)".
        "current_session_started_at": snap.get("current_session_started_at"),
        "current_session_duration_min": round(
            int(snap.get("current_session_duration_s") or 0) / 60, 1
        ),
        "used_today_min": round(used_s / 60, 1),
        "remaining_today_min": round(rem_s / 60, 1),
        # `budget_today_min` is the RAW config default. New in v0.10:
        # `base_budget_today_min` reflects today's weekday override (if
        # any); `effective_budget_today_min` adds the extension pool on
        # top so `remaining + used == effective` always holds.
        "budget_today_min": p.daily_budget_min,
        "base_budget_today_min": base_today_min,
        "effective_budget_today_min": base_today_min + extension_min,
        "extension_minutes_today": extension_min,
        "is_blocked": bool(snap.get("is_blocked")),
        "adult_mode_active": bool(snap.get("adult_mode_active")),
        "adult_mode_until": snap.get("adult_mode_until"),
        "active_quiet_window": snap.get("active_quiet_window"),
        # v0.14.0 — monitor mode visibility (panel renders a banner).
        # In v0.15.0+ this is a derived alias for mode=="enforced" kept
        # for back-compat. New consumers should look at `mode` instead.
        "enforcement_enabled": bool(getattr(p, "enforcement_enabled",
                                            getattr(p, "mode", "enforced") == "enforced")),
        # v0.15.0 — mode redesign surface (spec §3.1, §3.3).
        "mode": getattr(p, "mode", "enforced"),
        "effective_state": _compute_effective_state(p, snap, bundle),
        "tv_shutdown_target": getattr(p, "tv_shutdown_target", None),
        "enforcement_failed": bool(snap.get("enforcement_failed")),
        # v0.20.0 — unified-profile visibility for the panel/migration.
        "device_kind": getattr(p, "device_kind", "apple_tv"),
        "secondary_devices": list(getattr(p, "secondary_devices", None) or []),
    }


def _compute_effective_state(profile, snap: dict, bundle: dict) -> str:
    """Mirror of EffectiveStateSensor.native_value for the REST surface.

    Kept here (not imported from sensor.py) to avoid pulling sensor entity
    classes into the API import chain. The matrix is small + the spec
    contract is locked, so duplication risk is low.
    """
    from .policy import should_act
    from homeassistant.util import dt as dt_util
    store = bundle.get("store")
    until = store.adult_mode_until(profile.id) if store is not None else None
    mode = getattr(profile, "mode",
                   "enforced" if getattr(profile, "enforcement_enabled", True)
                   else "monitor_only")
    decision = should_act(mode=mode, adult_mode_until=until, now=dt_util.utcnow())
    if decision.reason == "adult_mode":
        return "adult_mode"
    if decision.reason == "paused":
        return "paused"
    raw = snap.get("enforcement_state") or "ok"
    if decision.reason == "monitor_only":
        return {
            "ok": "observing",
            "warning": "observing_warn",
            "grace": "observing_over_budget",
            "enforcing": "observing_over_budget",
        }.get(raw, "observing")
    # ACT — enforced
    if raw == "enforcing" and snap.get("enforcement_failed"):
        return "enforcing_failed"
    return raw


def _limits_payload(hass: HomeAssistant, bundle: dict[str, Any]) -> dict[str, Any]:
    """Build the structured /limits response.

    Includes the base config + per-weekday overrides + a `today` block
    that shows the effective values after the schedule is applied — so
    panel/OpenClaw consumers don't have to re-resolve it themselves.
    """
    from datetime import date as _date

    p: Profile = bundle["profile"]
    entry = bundle["entry"]

    def _parse_qw(s: str) -> list[dict[str, str]]:
        try:
            return [
                {
                    "start": w.start.isoformat(timespec="minutes"),
                    "end": w.end.isoformat(timespec="minutes"),
                    "label": w.label or "",
                }
                for w in parse_windows(s or "")
            ]
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("limits: parse_windows(%r) failed: %s", s, err)
            return []

    today_local = _date.today()
    today_budget_min = effective_daily_budget(
        base_min=p.daily_budget_min,
        weekday_overrides=p.weekday_budgets_min or {},
        on=today_local,
    )
    today_group_budgets = effective_group_budgets(
        base_groups=p.group_budgets or {},
        weekday_group_overrides=p.weekday_group_budgets_min or {},
        on=today_local,
    )
    today_qw_raw = effective_quiet_windows_string(
        base_string=p.quiet_windows or "",
        weekday_overrides=p.weekday_quiet_windows or {},
        on=today_local,
    )

    return {
        "profile_id": p.id,
        "display_name": p.display_name,
        "apple_tv_entity_id": p.apple_tv_entity_id,
        "adguard_client_name": p.adguard_client_name,
        # Base (default) values — applied any day without a per-weekday override.
        "daily_budget_min": p.daily_budget_min,
        "group_budgets_min": dict(p.group_budgets or {}),
        "quiet_windows_raw": p.quiet_windows or "",
        "quiet_windows": _parse_qw(p.quiet_windows or ""),
        # Per-weekday overrides (v0.11.0). All optional.
        "weekdays": list(WEEKDAYS),
        "weekday_budgets_min": dict(p.weekday_budgets_min or {}),
        "weekday_group_budgets_min": {
            wd: dict(g) for wd, g in (p.weekday_group_budgets_min or {}).items()
        },
        "weekday_quiet_windows": dict(p.weekday_quiet_windows or {}),
        # Misc settings (unchanged from earlier versions).
        "grace_seconds": p.grace_seconds,
        "warn_thresholds_min": list(p.warn_thresholds_min or []),
        "idle_grace_minutes": p.idle_grace_minutes,
        "stale_session_minutes": int(
            getattr(p, "stale_session_minutes", 5)
        ),  # v0.17.1
        "adult_mode_duration_min": p.adult_mode_duration_min,
        "tv_entity_id": p.tv_entity_id,
        "tv_shutdown_enabled": bool(p.tv_shutdown_enabled),
        # v0.15.0 — canonical TV shutdown target. tv_entity_id stays as
        # the memory field; tv_shutdown_target is the active one.
        "tv_shutdown_target": getattr(p, "tv_shutdown_target", None),
        # v0.19.0 / v0.20.0 — multi-device: the device kind + any folded-in
        # secondary devices (e.g. an Xbox). Surfaced here (not just in the
        # profile summary) so read-modify-write clients on GET /limits see and
        # can round-trip the unified-profile wiring PATCH /limits accepts.
        "device_kind": getattr(p, "device_kind", "apple_tv"),
        "enforcement_switch_entity_id": getattr(p, "enforcement_switch_entity_id", None),
        "secondary_devices": list(getattr(p, "secondary_devices", None) or []),
        # v0.21.0 — native TV watching ("Live TV") config, so read-modify-write
        # clients on GET /limits can round-trip the two new fields.
        "track_native_tv": bool(getattr(p, "track_native_tv", False)),
        "native_tv_excluded_sources": list(
            getattr(p, "native_tv_excluded_sources", None) or []
        ),
        # v0.14.0 — monitor mode toggle. In v0.15.0+ this is a derived
        # alias for mode=="enforced" kept for back-compat.
        "enforcement_enabled": bool(getattr(p, "enforcement_enabled",
                                            getattr(p, "mode", "enforced") == "enforced")),
        # v0.15.0 — mode redesign (spec §3.1, §3.7).
        "mode": getattr(p, "mode", "enforced"),
        "warn_in_monitor_mode": bool(getattr(p, "warn_in_monitor_mode", False)),
        "voice_on_mode_change": bool(getattr(p, "voice_on_mode_change", False)),
        "enable_adguard_block": bool(getattr(p, "enable_adguard_block", False)),
        # ----- Voice announcements (v0.13.0 + v0.15.0) -----
        "notify_media_player_entity_id": p.notify_media_player_entity_id or "",
        "notify_tts_entity_id": p.notify_tts_entity_id or "",
        "notify_tts_language": p.notify_tts_language,
        "notify_volume": float(p.notify_volume or 0),
        "warning_message": p.warning_message or "",
        "enforce_message": p.enforce_message or "",
        "extension_message": getattr(p, "extension_message", "") or "",
        "adult_mode_on_message": getattr(p, "adult_mode_on_message", "") or "",
        "mode_change_message": getattr(p, "mode_change_message", "") or "",
        # v0.16.0 anti-defeat features
        "countdown_message": getattr(p, "countdown_message", "") or "",
        "reactivation_message_friendly":
            getattr(p, "reactivation_message_friendly", "") or "",
        "reactivation_message_stern":
            getattr(p, "reactivation_message_stern", "") or "",
        "notify_parent_target": getattr(p, "notify_parent_target", "") or "",
        # v0.18.0 — DNS-corroborated attribution
        "apple_tv_ip": getattr(p, "apple_tv_ip", "") or "",
        "dns_corroboration_mode":
            getattr(p, "dns_corroboration_mode", "off") or "off",
        # v0.18.0 — proactive pyatv reload (in-integration self-heal).
        "apple_tv_entry_id": getattr(p, "apple_tv_entry_id", "") or "",
        # v0.19.0 — multi-device fields. device_kind echoes how this profile
        # is wired; enforcement_switch_entity_id is the Xbox-MVP block target.
        "device_kind": getattr(p, "device_kind", "apple_tv"),
        "enforcement_switch_entity_id":
            getattr(p, "enforcement_switch_entity_id", None),
        # Today's resolved values — saves clients a round-trip.
        "today": {
            "weekday": weekday_key(today_local),
            "date": today_local.isoformat(),
            "daily_budget_min": today_budget_min,
            "group_budgets_min": today_group_budgets,
            "quiet_windows_raw": today_qw_raw,
            "quiet_windows": _parse_qw(today_qw_raw),
        },
        # Deep-link the user can open to edit via HA's options flow.
        "edit_url_path": f"/config/integrations/integration/{DOMAIN}",
        "config_entry_id": entry.entry_id,
    }


class _LimitsValidationError(ValueError):
    """Raised by `_validate_limits_patch` with a user-facing message."""


_VALID_MODES = ("enforced", "monitor_only", "paused")


# v0.17.0 F-J (Opus BA audit P2, the owner Q3 decision: auto-generate from
# this matrix) — single source of truth for the PATCH /limits OpenAPI
# schema. `_build_openapi` consumes this dict so the documented schema
# can never silently drift from the actual validator (each new field
# REJECTS via _validate_limits_patch's allowed-set unless it has an
# entry here; the LIMITS_PATCH_FIELDS dict is the rule book).
#
# Constraints expressed here MUST match the imperative rules inside
# `_validate_field` below. The drift-detection test
# `test_openapi_limits_patch_constraints_match_validator` in
# tests/test_api_validators.py enforces equivalence for the integer
# range fields (the failure modes that motivated F-J in the first
# place; string-length / enum drifts are caught by the same suite).
LIMITS_PATCH_FIELDS: dict[str, dict[str, Any]] = {
    "daily_budget_min": {"type": "integer", "minimum": 0, "maximum": 1440},
    "group_budgets": {
        "type": "object",
        "additionalProperties": {"type": "integer", "minimum": 0, "maximum": 1440},
        "description": "Per-group budgets, keyed by group name.",
    },
    "quiet_windows": {"type": "string"},
    "weekday_budgets_min": {
        "type": "object",
        "description": "Per-weekday overrides keyed by mon/tue/wed/thu/fri/sat/sun.",
        "additionalProperties": {"type": "integer", "minimum": 0, "maximum": 1440},
    },
    "weekday_group_budgets_min": {
        "type": "object",
        "description": "Per-weekday per-group overrides; outer key=weekday, inner={group:minutes}.",
        "additionalProperties": {
            "type": "object",
            "additionalProperties": {"type": "integer", "minimum": 0, "maximum": 1440},
        },
    },
    "weekday_quiet_windows": {
        "type": "object",
        "description": "Per-weekday quiet windows string; replaces base quiet_windows for that day.",
        "additionalProperties": {"type": "string"},
    },
    "adult_mode_duration_min": {"type": "integer", "minimum": 1, "maximum": 1440},
    "grace_seconds": {"type": "integer", "minimum": 0, "maximum": 600},
    "idle_grace_minutes": {"type": "integer", "minimum": 0, "maximum": 60},
    "stale_session_minutes": {
        "type": "integer",
        "minimum": 0,
        "maximum": 60,
        "description": "v0.17.1 — close the open UsageEvent if the apple_tv_entity_id stays at a stuck ACTIVE state for this many minutes. Catches pyatv silent disconnect. 0 disables the check.",
    },
    "warn_thresholds_min": {
        "type": "array",
        "items": {"type": "integer", "minimum": 0, "maximum": 1440},
    },
    "tv_entity_id": {
        "type": ["string", "null"],
        "description": "Legacy field; prefer tv_shutdown_target.",
    },
    "tv_shutdown_enabled": {"type": "boolean", "description": "Legacy field."},
    "tv_shutdown_target": {
        "type": ["string", "null"],
        "maxLength": 200,
        "description": "HA entity_id of the secondary TV to turn off on enforce.",
    },
    "enforcement_enabled": {
        "type": "boolean",
        "description": "Legacy v0.14.0 flag; prefer `mode`.",
    },
    "mode": {"type": "string", "enum": list(_VALID_MODES)},
    "warn_in_monitor_mode": {"type": "boolean"},
    "voice_on_mode_change": {"type": "boolean"},
    "enable_adguard_block": {"type": "boolean"},
    "notify_media_player_entity_id": {"type": "string", "maxLength": 200},
    "notify_tts_entity_id": {"type": "string", "maxLength": 200},
    "notify_tts_language": {"type": "string", "maxLength": 200},
    "notify_volume": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    "warning_message": {"type": "string", "maxLength": 500},
    "enforce_message": {"type": "string", "maxLength": 500},
    "extension_message": {"type": "string", "maxLength": 500},
    "adult_mode_on_message": {"type": "string", "maxLength": 500},
    "mode_change_message": {"type": "string", "maxLength": 500},
    "countdown_message": {"type": "string", "maxLength": 500},
    "reactivation_message_friendly": {"type": "string", "maxLength": 500},
    "reactivation_message_stern": {"type": "string", "maxLength": 500},
    "notify_parent_target": {"type": "string", "maxLength": 100},
    # v0.18.0 — DNS-corroborated attribution.
    "apple_tv_ip": {
        "type": "string",
        "maxLength": 45,  # IPv6-safe
        "description": (
            "v0.18.0 — IP address of the Apple TV on the LAN. Used by the "
            "DNS-corroborated attribution feature to query AdGuard's recent "
            "query log for this device. Empty disables the feature regardless "
            "of dns_corroboration_mode."
        ),
    },
    "dns_corroboration_mode": {
        "type": "string",
        "enum": ["off", "monitor", "correct"],
        "description": (
            "v0.18.0 — How the DNS classifier interacts with usage tracking. "
            "off (default): v0.17.x behavior exactly, classifier not consulted. "
            "monitor: classifier runs every tick, audit log records proposed "
            "corrections, UsageEvent NOT mutated. "
            "correct: classifier runs, corrections applied via group_segments."
        ),
    },
    "apple_tv_entry_id": {
        "type": "string",
        "maxLength": 64,
        "description": (
            "v0.18.0 — HA ConfigEntry.entry_id of the Apple TV core integration "
            "owning the apple_tv_entity_id. Used by the proactive pyatv reload "
            "feature: when pyatv's push channel goes silent for >= 10 min while "
            "Samsung is on and DNS is firing, the coordinator calls "
            "hass.config_entries.async_reload(...) on this id to restart pyatv. "
            "Populated automatically by the integration at setup from the entity "
            "registry; PATCH override is allowed but rarely needed. Empty string "
            "disables the feature."
        ),
    },
    "enforcement_switch_entity_id": {
        "type": ["string", "null"],
        "description": (
            "v0.19.0 — Xbox MVP. switch.* entity that the enforcer flips OFF "
            "to block this profile's network access (typically the FRITZ!Box-"
            "driven switch.<host>_internet_access for the Xbox). Ignored for "
            "device_kind != 'xbox_presence'. Null or empty disables the "
            "network-block primitive for this profile (TV-shutdown fallback "
            "may still apply if configured)."
        ),
    },
    # v0.20.0 — secondary devices folded into THIS profile's single shared
    # budget (the "one system, not two" merge). Used by the live migration to
    # attach an Xbox to the Living Room (Apple TV) profile via one POST /limits.
    "secondary_devices": {
        "type": "array",
        "items": {
            "type": "object",
            "required": ["entity_id", "device_kind"],
            "properties": {
                "entity_id": {"type": "string", "maxLength": 200},
                "device_kind": {"type": "string", "enum": list(DEVICE_KINDS)},
                "enforcement_switch_entity_id": {"type": ["string", "null"], "maxLength": 200},
                "bundle_id": {"type": "string", "maxLength": 100},
            },
        },
        "description": (
            "v0.20.0 — devices folded into this profile's ONE shared budget. "
            "Each item: {entity_id, device_kind, enforcement_switch_entity_id, "
            "bundle_id}. Activity on a secondary accrues to the same daily/group "
            "budget as the primary; on exhaustion its switch is turned OFF. "
            "Empty list detaches all secondaries."
        ),
    },
    # v0.21.0 — native TV watching ("Live TV"). Book time the TV spends on with
    # a non-tracked source to the linear_tv group under the same room budget.
    "track_native_tv": {
        "type": "boolean",
        "description": (
            "v0.21.0 — track native TV watching (tuner / SCART / smart-TV app) "
            "as the linear_tv group. Requires tv_entity_id to be set. Default "
            "off; enabling it changes nothing until the TV is on with a "
            "source outside native_tv_excluded_sources."
        ),
    },
    "native_tv_excluded_sources": {
        "type": "array",
        "items": {"type": "string", "maxLength": 100},
        "description": (
            "v0.21.0 — TV source names that are NOT native TV because a tracked "
            "device owns that input (e.g. HDMI1=Apple TV, HDMI2/DVI=Xbox). A TV "
            "whose current source is in this list is never booked as native TV."
        ),
    },
}


def _validate_limits_patch(body: dict[str, Any]) -> dict[str, Any]:
    """Validate a PATCH body and return the subset of Profile fields to set.

    Unknown fields are REJECTED (not silently dropped) to catch typos —
    a client patching `quiet_window` (typo) would otherwise overwrite
    nothing and never know.

    v0.15.0+: accepts `mode` + `tv_shutdown_target` (new canonical fields).
    Legacy `enforcement_enabled` + `tv_shutdown_enabled` + `tv_entity_id`
    still accepted (deprecated). Conflicting values for the same logical
    setting (e.g. `mode=monitor_only` + `enforcement_enabled=True`) return
    422 rather than silently picking one (per spec §3.6).
    """
    allowed = {
        "daily_budget_min",
        "group_budgets",
        "quiet_windows",
        "weekday_budgets_min",
        "weekday_group_budgets_min",
        "weekday_quiet_windows",
        "adult_mode_duration_min",
        "grace_seconds",
        "idle_grace_minutes",
        "stale_session_minutes",   # v0.17.1
        "warn_thresholds_min",
        "tv_entity_id",             # legacy / memory
        "tv_shutdown_enabled",      # legacy
        "tv_shutdown_target",       # v0.15.0 canonical
        # v0.14.0 monitor mode (legacy in v0.15.0+)
        "enforcement_enabled",
        # v0.15.0 mode redesign
        "mode",
        "warn_in_monitor_mode",
        "voice_on_mode_change",
        # v0.15.5 opt-in AdGuard blocking
        "enable_adguard_block",
        # v0.13.0 voice announcements
        "notify_media_player_entity_id",
        "notify_tts_entity_id",
        "notify_tts_language",
        "notify_volume",
        "warning_message",
        "enforce_message",
        "extension_message",
        # v0.15.0 voice templates
        "adult_mode_on_message",
        "mode_change_message",
        # v0.16.0 anti-defeat features
        "countdown_message",
        "reactivation_message_friendly",
        "reactivation_message_stern",
        "notify_parent_target",
        # v0.18.0 DNS-corroborated attribution
        "apple_tv_ip",
        "dns_corroboration_mode",
        # v0.18.0 proactive pyatv reload (self-heal)
        "apple_tv_entry_id",
        # v0.19.0 Xbox MVP enforcement target
        "enforcement_switch_entity_id",
        # v0.20.0 — secondary devices folded into the one shared budget
        "secondary_devices",
        # v0.21.0 — native TV watching
        "track_native_tv",
        "native_tv_excluded_sources",
    }
    unknown = set(body.keys()) - allowed
    if unknown:
        raise _LimitsValidationError(
            f"unknown field(s): {sorted(unknown)} — allowed: {sorted(allowed)}"
        )

    # Conflict check: mode vs enforcement_enabled (per spec §3.6).
    if "mode" in body and "enforcement_enabled" in body:
        mode_says_enforced = body["mode"] == "enforced"
        ee = bool(body["enforcement_enabled"])
        if mode_says_enforced != ee:
            raise _LimitsValidationError(
                "conflicting fields: mode=%r implies enforcement_enabled=%s "
                "but body sets enforcement_enabled=%s — pick one"
                % (body["mode"], mode_says_enforced, ee)
            )
    # Conflict check: tv_shutdown_target vs tv_shutdown_enabled.
    if "tv_shutdown_target" in body and "tv_shutdown_enabled" in body:
        target_says_enabled = body["tv_shutdown_target"] not in (None, "")
        legacy_enabled = bool(body["tv_shutdown_enabled"])
        if target_says_enabled != legacy_enabled:
            raise _LimitsValidationError(
                "conflicting fields: tv_shutdown_target=%r implies "
                "tv_shutdown_enabled=%s but body sets %s"
                % (body["tv_shutdown_target"], target_says_enabled, legacy_enabled)
            )

    out: dict[str, Any] = {}
    for field_name, value in body.items():
        try:
            out[field_name] = _validate_field(field_name, value)
        except (_LimitsValidationError, ValueError) as err:
            raise _LimitsValidationError(f"{field_name}: {err}") from err
    return out


def _validate_field(field_name: str, value: Any) -> Any:
    if field_name == "daily_budget_min":
        v = int(value)
        if v < 0 or v > 24 * 60:
            raise _LimitsValidationError("must be 0..1440")
        return v
    if field_name == "group_budgets":
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise _LimitsValidationError("must be an object mapping group -> minutes")
        out: dict[str, int] = {}
        for g, m in value.items():
            mins = int(m)
            if mins < 0 or mins > 24 * 60:
                raise _LimitsValidationError(f"group {g!r} minutes 0..1440 required")
            out[str(g)] = mins
        return out
    if field_name == "quiet_windows":
        s = str(value or "")
        if s:
            try:
                validate_windows_string(s)
            except ValueError as err:
                raise _LimitsValidationError(str(err)) from err
        return s
    if field_name == "weekday_budgets_min":
        def _decode(v: Any) -> int:
            mins = int(v)
            if mins < 0 or mins > 24 * 60:
                raise _LimitsValidationError("0..1440 required")
            return mins
        return normalize_weekday_dict(value, value_decoder=_decode)
    if field_name == "weekday_group_budgets_min":
        return normalize_weekday_group_dict(value)
    if field_name == "weekday_quiet_windows":
        def _decode_qw(v: Any) -> str:
            s = str(v or "")
            if s:
                try:
                    validate_windows_string(s)
                except ValueError as err:
                    raise _LimitsValidationError(str(err)) from err
            return s
        return normalize_weekday_dict(value, value_decoder=_decode_qw)
    if field_name == "adult_mode_duration_min":
        v = int(value)
        if v < 1 or v > 24 * 60:
            raise _LimitsValidationError("must be 1..1440")
        return v
    if field_name == "grace_seconds":
        v = int(value)
        if v < 0 or v > 600:
            raise _LimitsValidationError("must be 0..600")
        return v
    if field_name == "idle_grace_minutes":
        v = int(value)
        if v < 0 or v > 60:
            raise _LimitsValidationError("must be 0..60")
        return v
    if field_name == "stale_session_minutes":
        # v0.17.1 — 0 disables the staleness check; >60 risks letting a
        # phantom session accrue an hour before being closed.
        v = int(value)
        if v < 0 or v > 60:
            raise _LimitsValidationError("must be 0..60")
        return v
    if field_name == "warn_thresholds_min":
        if not isinstance(value, list):
            raise _LimitsValidationError("must be a list of ints")
        out_list: list[int] = []
        for t in value:
            n = int(t)
            if n < 0 or n > 24 * 60:
                raise _LimitsValidationError(f"threshold {n} out of 0..1440")
            out_list.append(n)
        return out_list
    if field_name == "tv_entity_id":
        if value is None:
            return None
        return str(value)
    if field_name == "tv_shutdown_enabled":
        return bool(value)
    # v0.15.0 canonical tv shutdown target -------------------------------
    if field_name == "tv_shutdown_target":
        # Canonical "off" is None. Empty string is coerced to None
        # (per spec §3.5 + Sonnet QA-1 S3).
        if value is None or value == "":
            return None
        s = str(value).strip()
        # Light validation: must look like an entity_id with a domain
        # prefix. Don't restrict to media_player only — the user might
        # configure a switch/light/script as the target (e.g. a Harmony
        # activity that turns off the AV stack).
        if "." not in s or len(s) > 200:
            raise _LimitsValidationError(
                "must be an HA entity_id (e.g. media_player.samsung_tv)"
            )
        return s
    # v0.14.0 monitor-mode toggle (legacy in v0.15.0+) -------------------
    if field_name == "enforcement_enabled":
        return bool(value)
    # v0.15.0 mode + per-profile mode flags ------------------------------
    if field_name == "mode":
        s = str(value).strip()
        if s not in _VALID_MODES:
            raise _LimitsValidationError(
                f"must be one of: {', '.join(_VALID_MODES)}"
            )
        return s
    if field_name in ("warn_in_monitor_mode", "voice_on_mode_change",
                      "enable_adguard_block",
                      # v0.21.0 — native TV watching opt-in
                      "track_native_tv"):
        return bool(value)
    if field_name in ("adult_mode_on_message", "mode_change_message"):
        if value is None:
            return ""
        s = str(value)
        if len(s) > 500:
            raise _LimitsValidationError("too long (max 500 chars)")
        return s
    # v0.13.0 voice announcement fields ----------------------------------
    if field_name in ("notify_media_player_entity_id", "notify_tts_entity_id",
                      "notify_tts_language"):
        if value is None:
            return ""
        s = str(value).strip()
        if len(s) > 200:
            raise _LimitsValidationError("too long (max 200 chars)")
        return s
    if field_name == "notify_volume":
        try:
            v = float(value or 0)
        except (TypeError, ValueError) as err:
            raise _LimitsValidationError("must be a number 0..1") from err
        if v < 0 or v > 1:
            raise _LimitsValidationError("must be 0..1")
        return v
    if field_name in ("warning_message", "enforce_message", "extension_message"):
        if value is None:
            return ""
        s = str(value)
        if len(s) > 500:
            raise _LimitsValidationError("too long (max 500 chars)")
        return s
    # v0.16.0 anti-defeat voice templates -------------------------------
    if field_name in (
        "countdown_message",
        "reactivation_message_friendly",
        "reactivation_message_stern",
    ):
        if value is None:
            return ""
        s = str(value)
        if len(s) > 500:
            raise _LimitsValidationError("too long (max 500 chars)")
        return s
    # v0.16.0 parent push target -- HA notify service name (without
    # the `notify.` prefix). Empty = fall back to `notify.notify`.
    if field_name == "notify_parent_target":
        if value is None:
            return ""
        s = str(value).strip()
        if len(s) > 100:
            raise _LimitsValidationError("too long (max 100 chars)")
        return s
    # v0.18.0 — DNS-corroborated attribution
    if field_name == "apple_tv_ip":
        if value is None:
            return ""
        s = str(value).strip()
        if len(s) > 45:  # IPv6-safe
            raise _LimitsValidationError("apple_tv_ip too long (max 45 chars)")
        # Empty string is valid (forces feature off regardless of mode).
        return s
    if field_name == "dns_corroboration_mode":
        if value is None:
            return "off"
        s = str(value).strip().lower()
        if s not in ("off", "monitor", "correct"):
            raise _LimitsValidationError(
                "dns_corroboration_mode must be one of: off, monitor, correct"
            )
        return s
    # v0.18.0 — proactive pyatv reload. The Apple TV core integration's
    # ConfigEntry.entry_id (resolved at setup via the entity registry).
    # Empty string is explicitly allowed and disables the feature.
    if field_name == "apple_tv_entry_id":
        if value is None:
            return ""
        s = str(value).strip()
        if len(s) > 64:
            raise _LimitsValidationError(
                "apple_tv_entry_id too long (max 64 chars)"
            )
        return s
    # v0.19.0 — Xbox MVP. The switch.* entity the enforcer flips OFF to
    # block the Xbox profile's internet access. None / empty string is
    # explicitly allowed (disables the network-block primitive for this
    # profile). Accepts any string up to 128 chars — HA entity_id format
    # validation is done by the entity selector at config-flow time, not
    # at PATCH time.
    if field_name == "enforcement_switch_entity_id":
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        if len(s) > 128:
            raise _LimitsValidationError(
                "enforcement_switch_entity_id too long (max 128 chars)"
            )
        return s
    # v0.20.0 — secondary devices folded into the one shared budget. Validate
    # + normalize each item (this is load-bearing: the live migration's
    # POST /limits would 422 at the final raise below without this branch).
    if field_name == "secondary_devices":
        if value is None:
            return []
        if not isinstance(value, list):
            raise _LimitsValidationError("must be a list of device objects")
        out_list: list[dict] = []
        for i, item in enumerate(value):
            if not isinstance(item, dict):
                raise _LimitsValidationError(f"item {i}: must be an object")
            eid = str(item.get("entity_id") or "").strip()
            if not eid:
                raise _LimitsValidationError(f"item {i}: entity_id is required")
            if len(eid) > 200:
                raise _LimitsValidationError(f"item {i}: entity_id too long (max 200)")
            dk = str(item.get("device_kind") or "").strip()
            if dk not in DEVICE_KINDS:
                raise _LimitsValidationError(
                    f"item {i}: device_kind must be one of {sorted(DEVICE_KINDS)}"
                )
            sw = item.get("enforcement_switch_entity_id")
            sw = str(sw).strip() if sw not in (None, "") else None
            if sw is not None and len(sw) > 200:
                raise _LimitsValidationError(
                    f"item {i}: enforcement_switch_entity_id too long (max 200)"
                )
            bundle = str(item.get("bundle_id") or "").strip()
            if not bundle:
                # Default the synthetic bundle so attribution lands in the
                # right group (xbox_presence → gaming via categorize).
                bundle = (
                    XBOX_CONSOLE_BUNDLE_ID
                    if dk == DEVICE_KIND_XBOX_PRESENCE
                    else eid
                )
            out_list.append({
                "entity_id": eid,
                "device_kind": dk,
                "enforcement_switch_entity_id": sw,
                "bundle_id": bundle,
            })
        return out_list
    # v0.21.0 — native TV excluded sources. Normalize to a de-duped list of
    # trimmed, non-empty source-name strings. None ⇒ empty list.
    if field_name == "native_tv_excluded_sources":
        if value is None:
            return []
        if not isinstance(value, list):
            raise _LimitsValidationError(
                "must be a list of source-name strings"
            )
        out_srcs: list[str] = []
        for i, item in enumerate(value):
            s = str(item).strip()
            if not s:
                continue
            if len(s) > 100:
                raise _LimitsValidationError(
                    f"item {i}: source name too long (max 100)"
                )
            if s not in out_srcs:
                out_srcs.append(s)
        return out_srcs
    raise _LimitsValidationError("unsupported field")


def _resolve_actor(request: web.Request) -> str:
    """Map the X-AppleTV-Mgmt-Source header to an audit actor (per spec §3.6).

    Callers (typically the panel addon or HA Assist) set the header to
    distinguish themselves from bare REST traffic. Unknown values are
    accepted verbatim (forward-compat — audit rows store the raw string;
    panel renders it as-is).
    """
    raw = (request.headers.get("X-AppleTV-Mgmt-Source") or "").strip().lower()
    if not raw:
        return "rest"
    # Known values get returned as-is. Unknown values pass through so a
    # future actor (e.g. "homekit") works without a code change here.
    return raw


def _request_json(req: ExtensionRequest) -> dict[str, Any]:
    return {
        "id": req.id,
        "profile_id": req.profile_id,
        "requested_minutes": req.requested_minutes,
        "granted_minutes": req.granted_minutes,
        "reason": req.reason,
        "bundle_id": req.bundle_id,
        "requested_at": req.requested_at.isoformat(),
        "auto_expires_at": req.auto_expires_at.isoformat(),
        "status": req.status,
        "decided_at": req.decided_at.isoformat() if req.decided_at else None,
        "decided_by": req.decided_by,
    }


# ---------- views ----------------------------------------------------------


class _Base(HomeAssistantView):
    """Shared base — auth + boilerplate.

    v0.22.0 (SECURITY) — `requires_auth = False` used to be set here, which
    made EVERY view anonymous: the endpoints that lift a child's limits
    (`PATCH /limits`, `POST /adult_mode`, `POST /extension`,
    `POST /requests/{id}/decide`) were reachable by anything on the LAN,
    and by anything at all when the instance is exposed via Nabu Casa, a
    reverse proxy or a forwarded port. The custom `api_key` gate that was
    meant to cover this opened with `if not key: return None`, so it was a
    no-op on a fresh install — and the key was only ever offered in the
    OPTIONS flow, never at initial setup, so a fresh install never had one.

    We now inherit Home Assistant's own bearer check instead of
    reimplementing authentication. Callers use a normal HA long-lived
    access token; add-ons (like the companion panel) reach it through the
    Supervisor's `homeassistant_api` proxy, which injects valid auth.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def _ok(self, body: Any, status: int = 200) -> web.Response:
        return web.json_response(body, status=status)

    def _err(self, code: str, message: str, status: int) -> web.Response:
        return web.json_response({"error": code, "message": message}, status=status)


class HealthView(_Base):
    url = f"{API_BASE}/health"
    name = f"api:{DOMAIN}:health"

    async def get(self, request: web.Request) -> web.Response:
        return self._ok(
            {
                "status": "ok",
                "domain": DOMAIN,
                "version": _integration_version(self._hass),
                # v0.22.0 — always true; kept for API compatibility.
                "auth_required": True,
                "profile_count": len(self._hass.data.get(DOMAIN, {})),
                "now": dt_util.utcnow().isoformat(),
            }
        )


class OpenAPIView(_Base):
    url = f"{API_BASE}/openapi.json"
    name = f"api:{DOMAIN}:openapi"

    async def get(self, request: web.Request) -> web.Response:
        return self._ok(_build_openapi(self._hass))


class ProfilesView(_Base):
    url = f"{API_BASE}/profiles"
    name = f"api:{DOMAIN}:profiles"

    async def get(self, request: web.Request) -> web.Response:
        return self._ok(
            [_profile_summary(b) for b in self._hass.data.get(DOMAIN, {}).values()]
        )


class ProfileStatusView(_Base):
    url = f"{API_BASE}/profiles/{{profile_id}}/status"
    name = f"api:{DOMAIN}:profile_status"

    async def get(self, request: web.Request, profile_id: str) -> web.Response:
        bundle = _bundle_for(self._hass, profile_id)
        if bundle is None:
            return self._err("not_found", f"profile {profile_id!r}", 404)
        return self._ok(_profile_summary(bundle))


def _effective_group_rows(
    totals_s: dict[str, int],
    budgets_min: dict[str, int],
    group_ext_min: dict[str, int],
) -> list[dict]:
    """Build the ``/groups`` rows, folding each group's TAGGED extension into
    its reported budget so the payload matches what the enforcer enforces
    (``enforcer.eff_group_budgets``).

    v0.20.3 fix: previously the rows used the *base* group budget, so a "+60m"
    granted against the movies cap lifted the real limit but the panel card
    still showed the base cap as maxed-out (0m left, red) while the profile sat
    at OK. Only POSITIVE base budgets receive the extension; a ``0`` budget
    means "unlimited" and stays unlimited (same guard as the enforcer). Pure —
    unit-tested in test_api_validators.
    """
    out: list[dict] = []
    for g in sorted(set(totals_s) | set(budgets_min)):
        used_s = int(totals_s.get(g, 0))
        base = budgets_min.get(g)
        ext = int(group_ext_min.get(g, 0))
        budget = base + ext if (base is not None and base > 0) else base
        out.append(
            {
                "group": g,
                "used_today_min": round(used_s / 60, 1),
                # `budget_today_min` is the EFFECTIVE cap (base + tagged
                # extension); `base_budget_today_min` + `extension_today_min`
                # let the UI show "30m + 60m granted" if it wants.
                "budget_today_min": budget,
                "base_budget_today_min": base,
                "extension_today_min": ext,
                "remaining_today_min": (
                    None
                    if budget is None
                    else round(max(0, budget * 60 - used_s) / 60, 1)
                ),
            }
        )
    return out


class ProfileGroupsView(_Base):
    url = f"{API_BASE}/profiles/{{profile_id}}/groups"
    name = f"api:{DOMAIN}:profile_groups"

    async def get(self, request: web.Request, profile_id: str) -> web.Response:
        bundle = _bundle_for(self._hass, profile_id)
        if bundle is None:
            return self._err("not_found", f"profile {profile_id!r}", 404)
        snap = bundle["coordinator"].data or {}
        totals_s: dict[str, int] = snap.get("group_totals_seconds") or {}
        budgets_min: dict[str, int] = snap.get("group_budgets_minutes") or {}
        # v0.20.3 — fold each group's TAGGED extension into the reported budget
        # so the panel's category card matches what the enforcer actually
        # applies (mirrors enforcer.eff_group_budgets). Without this, a parent's
        # "+60 min" granted against the movies cap lifted the real limit but the
        # card still showed the base 30 min as maxed-out (0m left, red) while the
        # profile sat at OK — the exact contradiction reported 2026-07-05.
        # Only POSITIVE base budgets receive the extension; a 0 budget means
        # "unlimited" and must stay unlimited (see the same guard in enforcer).
        group_ext_min: dict[str, int] = bundle["store"].group_extensions_today(
            profile_id
        )
        return self._ok(_effective_group_rows(totals_s, budgets_min, group_ext_min))


class ProfileUsageView(_Base):
    url = f"{API_BASE}/profiles/{{profile_id}}/usage"
    name = f"api:{DOMAIN}:profile_usage"

    async def get(self, request: web.Request, profile_id: str) -> web.Response:
        bundle = _bundle_for(self._hass, profile_id)
        if bundle is None:
            return self._err("not_found", f"profile {profile_id!r}", 404)
        store: AppleTVMgmtStore = bundle["store"]
        # Per-app breakdown for today. The "week" range can be added in a
        # follow-up — would need a wider window aggregator in the store.
        now = dt_util.utcnow()
        totals = store.app_totals_today(profile_id, now=now)
        out = sorted(
            [
                {
                    "bundle_id": bid,
                    "minutes": round(int(slot["seconds"]) / 60, 1),
                    "sessions": int(slot["sessions"]),
                }
                for bid, slot in totals.items()
            ],
            key=lambda r: r["minutes"],
            reverse=True,
        )
        return self._ok({"range": "today", "apps": out})


class ProfileEventsView(_Base):
    """Chronological usage events.

    Query params (all optional):
      - `from=YYYY-MM-DD`  Inclusive local-date start. Default: today.
      - `to=YYYY-MM-DD`    Inclusive local-date end.   Default: same as from.
      - `date=YYYY-MM-DD`  Convenience alias for from=to=date.
    """

    url = f"{API_BASE}/profiles/{{profile_id}}/events"
    name = f"api:{DOMAIN}:profile_events"

    async def get(self, request: web.Request, profile_id: str) -> web.Response:
        bundle = _bundle_for(self._hass, profile_id)
        if bundle is None:
            return self._err("not_found", f"profile {profile_id!r}", 404)
        store: AppleTVMgmtStore = bundle["store"]

        from datetime import date as _date

        single = request.query.get("date")
        q_from = request.query.get("from") or single
        q_to = request.query.get("to") or single

        local_now = dt_util.as_local(dt_util.utcnow())
        today = local_now.date()

        def _parse(s: str | None, default: _date) -> _date | None:
            if not s:
                return default
            try:
                return _date.fromisoformat(s)
            except ValueError:
                return None

        d_from = _parse(q_from, today)
        d_to = _parse(q_to, d_from or today)
        if d_from is None or d_to is None:
            return self._err("invalid_payload", "from/to must be YYYY-MM-DD", 422)
        if d_to < d_from:
            return self._err("invalid_payload", "to must be >= from", 422)
        # Cap window to 1 year.
        if (d_to - d_from).days > 366:
            return self._err("invalid_payload", "window must be <= 366 days", 422)

        # Local midnight at d_from and d_to + 1 day.
        tz = local_now.tzinfo
        from datetime import datetime as _dt, time as _time, timedelta as _td

        local_from = _dt.combine(d_from, _time.min, tzinfo=tz)
        local_to = _dt.combine(d_to + _td(days=1), _time.min, tzinfo=tz)

        events = store.events_in_range(
            profile_id, local_from=local_from, local_to=local_to
        )
        return self._ok(
            {
                "from": d_from.isoformat(),
                "to": d_to.isoformat(),
                "events": [
                    {
                        "id": e.id,
                        "bundle_id": e.bundle_id,
                        "started_at": e.started_at.isoformat(),
                        "ended_at": e.ended_at.isoformat() if e.ended_at else None,
                        "duration_minutes": round(e.duration_seconds() / 60, 1),
                        "open": e.ended_at is None,
                    }
                    for e in events
                ],
            }
        )


class ProfileLimitsView(_Base):
    """Read or mutate all configured limits for a profile.

    GET returns the full Profile config plus a `today` snapshot showing
    the effective values after the per-weekday schedule is applied.
    PATCH accepts a subset of fields and validates + persists them.

    v0.11.0 added the per-weekday fields and the PATCH endpoint so the
    panel addon can edit limits in-place rather than redirecting to HA's
    options flow.
    """

    url = f"{API_BASE}/profiles/{{profile_id}}/limits"
    name = f"api:{DOMAIN}:profile_limits"

    async def get(self, request: web.Request, profile_id: str) -> web.Response:
        bundle = _bundle_for(self._hass, profile_id)
        if bundle is None:
            return self._err("not_found", f"profile {profile_id!r}", 404)
        return self._ok(_limits_payload(self._hass, bundle))

    async def patch(self, request: web.Request, profile_id: str) -> web.Response:
        return await self._handle_mutation(request, profile_id)

    async def post(self, request: web.Request, profile_id: str) -> web.Response:
        """POST alias for PATCH.

        Reason: HA Supervisor's `http://supervisor/core` reverse proxy
        does NOT forward PATCH methods to HA Core — it returns 405.
        Addons that talk through the supervisor proxy (the panel) must
        therefore use POST. Direct HA Core (port 8123) still accepts PATCH.
        """
        return await self._handle_mutation(request, profile_id)

    async def _handle_mutation(
        self, request: web.Request, profile_id: str
    ) -> web.Response:
        bundle = _bundle_for(self._hass, profile_id)
        if bundle is None:
            return self._err("not_found", f"profile {profile_id!r}", 404)
        try:
            body = await request.json() if request.body_exists else {}
        except ValueError:
            return self._err("invalid_payload", "body is not JSON", 422)
        if not isinstance(body, dict):
            return self._err("invalid_payload", "body must be a JSON object", 422)
        try:
            updates = _validate_limits_patch(body)
        except _LimitsValidationError as err:
            return self._err("invalid_payload", str(err), 422)

        p: Profile = bundle["profile"]
        actor = _resolve_actor(request)

        # Capture pre-change values for the dedicated audit rows that
        # supersede generic limits_changed for mode + tv_shutdown_target.
        old_mode = getattr(p, "mode", "enforced")
        old_target = getattr(p, "tv_shutdown_target", None)

        # Apply legacy-key translations BEFORE writing fields, so the
        # canonical fields end up consistent. The conflict check in
        # _validate_limits_patch already guaranteed the two halves agree
        # if both were sent.
        if "enforcement_enabled" in updates and "mode" not in updates:
            updates["mode"] = "enforced" if updates["enforcement_enabled"] else "monitor_only"
        if "tv_shutdown_enabled" in updates and "tv_shutdown_target" not in updates:
            # On legacy enable→ translate via memory (per spec §4.3).
            if updates["tv_shutdown_enabled"]:
                memory = updates.get("tv_entity_id") or p.tv_entity_id
                if memory:
                    updates["tv_shutdown_target"] = memory
            else:
                updates["tv_shutdown_target"] = None

        for field_name, value in updates.items():
            setattr(p, field_name, value)
        # Round-trip safety: keep legacy fields in sync with new ones.
        if "mode" in updates:
            p.enforcement_enabled = p.mode == "enforced"
        if "tv_shutdown_target" in updates:
            p.tv_shutdown_enabled = p.tv_shutdown_target is not None
            if p.tv_shutdown_target:
                p.tv_entity_id = p.tv_shutdown_target  # update memory
        bundle["store"].upsert_profile(p)
        await bundle["store"].async_save()
        await bundle["coordinator"].async_request_refresh()
        # v0.20.0 — when the secondary device set changed (the live-migration
        # attach path), re-wire the coordinator's state-change listener so the
        # newly-folded-in device fires immediately, not just on the next 30s
        # tick / restart. getattr-guarded for forward/back-compat safety.
        if "secondary_devices" in updates:
            _resub = getattr(
                bundle["coordinator"], "resubscribe_watched_entities", None
            )
            if callable(_resub):
                _resub()

        # PATCH dedup (spec §3.6 + Opus QA-2 S5): mode + tv_shutdown_target
        # have their own audit actions. Strip them from the
        # limits_changed field list so we don't double-emit.
        dedicated_keys = {"mode", "tv_shutdown_target",
                          "enforcement_enabled", "tv_shutdown_enabled"}
        residual = sorted(set(updates.keys()) - dedicated_keys)

        if "mode" in updates and p.mode != old_mode:
            record_admin_action(
                self._hass,
                profile_id=p.id,
                action="mode_changed",
                detail=f"{old_mode} → {p.mode}",
                actor=actor,
            )
            # v0.15.6 — fire mode_change voice if profile opts in
            from .voice_notifier import fire_mode_change_voice
            self._hass.async_create_task(
                fire_mode_change_voice(
                    self._hass, p,
                    old_mode=old_mode, new_mode=p.mode,
                )
            )
        if "tv_shutdown_target" in updates and p.tv_shutdown_target != old_target:
            record_admin_action(
                self._hass,
                profile_id=p.id,
                action="tv_shutdown_target_changed",
                detail=f"{old_target or 'none'} → {p.tv_shutdown_target or 'none'}",
                actor=actor,
            )
        # Only fire the generic limits_changed event if there's anything
        # left to report after dedup. Empty residual → silent (the
        # dedicated rows above are the full audit story).
        if residual:
            self._hass.bus.async_fire(
                EVENT_LIMITS_UPDATED,
                {
                    "profile_id": p.id,
                    "updated_fields": residual,
                    "actor": actor,
                },
            )

        return self._ok(_limits_payload(self._hass, bundle))


class AdultModeView(_Base):
    url = f"{API_BASE}/profiles/{{profile_id}}/adult_mode"
    name = f"api:{DOMAIN}:adult_mode"

    async def post(self, request: web.Request, profile_id: str) -> web.Response:
        """Enable adult mode for either N minutes OR until an ISO datetime.

        v0.15.0+ (spec §3.2.1): accepts `{minutes: N}` (legacy) OR
        `{until: "2026-05-25T01:00:00+02:00"}` (new). Validation matrix:

        | Condition                              | Response |
        |---|---|
        | both `minutes` AND `until` provided    | 422 |
        | `until` not parseable as ISO 8601      | 422 |
        | `until` TZ-naive                       | 422 |
        | `until` in the past                    | 422 |
        | `until` >24h in the future             | 422 (parity with minutes cap) |
        | both absent                            | use profile.adult_mode_duration_min |
        """
        bundle = _bundle_for(self._hass, profile_id)
        if bundle is None:
            return self._err("not_found", f"profile {profile_id!r}", 404)
        try:
            body = await request.json() if request.body_exists else {}
        except ValueError:
            return self._err("invalid_payload", "body is not JSON", 422)
        profile = bundle["profile"]
        actor = _resolve_actor(request)

        minutes_provided = "minutes" in body and body["minutes"] is not None
        until_provided = "until" in body and body["until"] is not None

        # v0.22.0 — proxy-friendly disable. `DELETE` is the correct verb and
        # still works for direct callers, but Supervisor's `core` proxy
        # forwards only GET and POST (PATCH/PUT/DELETE all 405), so an
        # add-on can never reach the DELETE handler. That made "cancel adult
        # mode" silently fail from the companion panel — a parent could grant
        # an override but not revoke it early. Accept `{"minutes": 0}` as an
        # explicit disable so the same action is reachable over POST.
        if minutes_provided and not until_provided:
            try:
                if int(body["minutes"]) == 0:
                    return await self.delete(request, profile_id)
            except (TypeError, ValueError):
                pass  # fall through to the normal validator's 422

        if minutes_provided and until_provided:
            return self._err(
                "invalid_payload",
                "exactly one of minutes or until must be provided",
                422,
            )

        now = dt_util.utcnow()

        if until_provided:
            until_raw = body["until"]
            if not isinstance(until_raw, str):
                return self._err(
                    "invalid_payload", "until must be an ISO 8601 string", 422
                )
            parsed = dt_util.parse_datetime(until_raw)
            if parsed is None:
                return self._err(
                    "invalid_payload",
                    "until must be ISO 8601 with timezone (got %r)" % until_raw,
                    422,
                )
            if parsed.tzinfo is None:
                return self._err(
                    "invalid_payload",
                    "until must include a timezone offset (got %r)" % until_raw,
                    422,
                )
            until = dt_util.as_utc(parsed)
            if until <= now:
                return self._err("invalid_payload", "until must be in the future", 422)
            if (until - now) > timedelta(minutes=24 * 60):
                return self._err(
                    "invalid_payload", "until must be within 24 hours", 422,
                )
            duration_minutes = int((until - now).total_seconds() // 60)
            detail = f"until {until.isoformat()}"
        else:
            minutes = int(body.get("minutes") or profile.adult_mode_duration_min)
            if minutes < 1 or minutes > 24 * 60:
                return self._err(
                    "invalid_payload", "minutes must be 1..1440", 422,
                )
            until = now + timedelta(minutes=minutes)
            duration_minutes = minutes
            detail = f"{minutes} min"

        bundle["store"].set_adult_mode_until(profile_id, until)
        record_admin_action(
            self._hass,
            profile_id=profile_id,
            action="adult_mode_on",
            detail=detail,
            actor=actor,
        )
        # v0.15.6 — fire adult_mode_on voice if profile opts in
        from .voice_notifier import fire_adult_mode_on_voice
        self._hass.async_create_task(
            fire_adult_mode_on_voice(self._hass, profile)
        )
        await bundle["store"].async_save()
        await bundle["coordinator"].async_request_refresh()
        return self._ok(
            {
                "profile_id": profile_id,
                "adult_mode_until": until.isoformat(),
                "duration_minutes": duration_minutes,
            }
        )

    async def delete(self, request: web.Request, profile_id: str) -> web.Response:
        bundle = _bundle_for(self._hass, profile_id)
        if bundle is None:
            return self._err("not_found", f"profile {profile_id!r}", 404)
        bundle["store"].set_adult_mode_until(profile_id, None)
        record_admin_action(
            self._hass, profile_id=profile_id, action="adult_mode_off",
        )
        await bundle["store"].async_save()
        await bundle["coordinator"].async_request_refresh()
        return self._ok({"profile_id": profile_id, "adult_mode_until": None})


class ExtensionView(_Base):
    """Direct minutes grant — bypasses the request/approval flow.

    Useful for "I'm the parent, just add 30 min". Distinct from the
    request_extension flow, which goes through the Companion approval
    UX.
    """

    url = f"{API_BASE}/profiles/{{profile_id}}/extension"
    name = f"api:{DOMAIN}:extension"

    async def post(self, request: web.Request, profile_id: str) -> web.Response:
        bundle = _bundle_for(self._hass, profile_id)
        if bundle is None:
            return self._err("not_found", f"profile {profile_id!r}", 404)
        try:
            body = await request.json() if request.body_exists else {}
        except ValueError:
            return self._err("invalid_payload", "body is not JSON", 422)
        try:
            minutes = int(body.get("minutes"))
        except (TypeError, ValueError):
            return self._err("invalid_payload", "minutes is required (int)", 422)
        if abs(minutes) > 240:
            return self._err("invalid_payload", "minutes must be -240..240", 422)
        # v0.19.1 — tag the grant to the binding/active group so it lifts the
        # GROUP budget, not just the daily pool. Callers may force a specific
        # group (or "daily" for daily-only) via the optional `group` field;
        # default auto-detects what's being blocked right now.
        from .audit import record_extension_granted, resolve_extension_target_group
        target_group = resolve_extension_target_group(
            self._hass, profile_id, body.get("group"), fallback_most_used=True
        )
        new_total = bundle["store"].add_extension_minutes(
            profile_id, minutes, group=target_group
        )
        # v0.17.0 F-K — shared helper for audit row + positive-minutes
        # voice. Was previously inlined here; now both paths (REST +
        # HA service) go through audit.record_extension_granted.
        record_extension_granted(
            self._hass,
            profile_id=profile_id,
            minutes=minutes,
            new_total=new_total,
            actor="rest",
            group=target_group,
        )
        await bundle["store"].async_save()
        await bundle["coordinator"].async_request_refresh()
        return self._ok(
            {
                "profile_id": profile_id,
                "extension_minutes_today": new_total,
                # v0.19.1 — which group (if any) this grant lifted, plus the
                # full per-group extension map so the panel can show
                # "+30 movies" feedback.
                "extension_group": target_group,
                "group_extensions_today": bundle["store"].group_extensions_today(
                    profile_id
                ),
            }
        )


class RequestExtensionView(_Base):
    """The kid-facing endpoint. Creates a pending request + fires a
    Companion notification to the parent."""

    url = f"{API_BASE}/profiles/{{profile_id}}/request_extension"
    name = f"api:{DOMAIN}:request_extension"

    async def post(self, request: web.Request, profile_id: str) -> web.Response:
        bundle = _bundle_for(self._hass, profile_id)
        if bundle is None:
            return self._err("not_found", f"profile {profile_id!r}", 404)
        try:
            body = await request.json() if request.body_exists else {}
        except ValueError:
            return self._err("invalid_payload", "body is not JSON", 422)
        try:
            minutes = int(body.get("minutes"))
        except (TypeError, ValueError):
            return self._err("invalid_payload", "minutes (int) is required", 422)
        if minutes < 1 or minutes > 240:
            return self._err("invalid_payload", "minutes must be 1..240", 422)
        reason = (body.get("reason") or "").strip()[:280]
        bundle_id = (body.get("bundle_id") or None)

        profile: Profile = bundle["profile"]
        store: AppleTVMgmtStore = bundle["store"]
        entry = bundle["entry"]
        merged = {**entry.data, **entry.options}
        expire_min = int(
            merged.get(CONF_REQUEST_EXPIRE_MIN, DEFAULT_REQUEST_EXPIRE_MIN)
        )
        notify_target = (merged.get(CONF_NOTIFY_TARGET) or "").strip() or None

        now = dt_util.utcnow()
        req = ExtensionRequest(
            # Cryptographic random suffix avoids ms-collisions when two
            # requests land in the same millisecond (QA P1 #6).
            id=f"req_{int(time.time() * 1000)}_{secrets.token_hex(4)}",
            profile_id=profile_id,
            requested_minutes=minutes,
            reason=reason,
            requested_at=now,
            auto_expires_at=now + timedelta(minutes=expire_min),
            bundle_id=bundle_id
            or (bundle["coordinator"].data or {}).get("current_bundle_id"),
        )
        store.add_request(req)
        await store.async_save()
        self._hass.bus.async_fire(
            EVENT_REQUEST_CREATED,
            {
                "profile_id": profile_id,
                "request_id": req.id,
                "requested_minutes": minutes,
                "reason": reason,
                "bundle_id": req.bundle_id,
            },
        )
        if notify_target:
            await send_request_notification(
                self._hass,
                request=req,
                profile=profile,
                notify_target=notify_target,
            )
        else:
            _LOGGER.info(
                "No notify_target configured — request %s created but no push sent",
                req.id,
            )
        return self._ok(_request_json(req), status=201)


class RequestView(_Base):
    url = f"{API_BASE}/requests/{{request_id}}"
    name = f"api:{DOMAIN}:request"

    async def get(self, request: web.Request, request_id: str) -> web.Response:
        for bundle in self._hass.data.get(DOMAIN, {}).values():
            req = bundle["store"].get_request(request_id)
            if req is not None:
                return self._ok(_request_json(req))
        return self._err("not_found", f"request {request_id!r}", 404)


class RequestDecideView(_Base):
    """POST a parent decision from a non-Companion source (the
    Lovelace card, the Mac mini, an automation, …). The Companion
    actionable notification is wired via `notify.register_action_handler`
    and goes through the same downstream code."""

    url = f"{API_BASE}/requests/{{request_id}}/decide"
    name = f"api:{DOMAIN}:request_decide"

    async def post(self, request: web.Request, request_id: str) -> web.Response:
        try:
            body = await request.json() if request.body_exists else {}
        except ValueError:
            return self._err("invalid_payload", "body is not JSON", 422)
        # Reject ambiguous payloads: missing `approve` was previously
        # silently treated as deny (QA P1 #13).
        if "approve" not in body or not isinstance(body["approve"], bool):
            return self._err(
                "invalid_payload",
                "`approve` (bool) is required",
                422,
            )
        approve = body["approve"]
        minutes = body.get("minutes")
        if minutes is not None:
            try:
                minutes = int(minutes)
            except (TypeError, ValueError):
                return self._err("invalid_payload", "minutes must be int", 422)
            # v0.17.0 F-M (Opus BA audit P2) — bound the approved minutes
            # range to match POST /extension + the appletv_mgmt.grant_extension
            # service. Pre-v0.17.0 this endpoint accepted arbitrary values
            # (notify.py:223 only clamped negatives to 0). A misbehaving
            # automation or API-key holder could grant +99999 minutes,
            # effectively disabling the daily budget for the rest of the day.
            # Negative magnitudes also rejected (was silently clamped to 0 —
            # contract change, see API.md changelog).
            if abs(minutes) > 240:
                return self._err(
                    "invalid_payload",
                    "minutes must be -240..240 (matches POST /extension cap)",
                    422,
                )
        await handle_external_decision(
            self._hass, request_id, approve=approve, minutes=minutes
        )
        # Echo current state.
        for bundle in self._hass.data.get(DOMAIN, {}).values():
            req = bundle["store"].get_request(request_id)
            if req is not None:
                return self._ok(_request_json(req))
        return self._err("not_found", f"request {request_id!r}", 404)


class ProfileRequestsView(_Base):
    url = f"{API_BASE}/profiles/{{profile_id}}/requests"
    name = f"api:{DOMAIN}:profile_requests"

    async def get(self, request: web.Request, profile_id: str) -> web.Response:
        bundle = _bundle_for(self._hass, profile_id)
        if bundle is None:
            return self._err("not_found", f"profile {profile_id!r}", 404)
        status = request.query.get("status")
        reqs = bundle["store"].requests_for_profile(profile_id, status=status)
        return self._ok([_request_json(r) for r in reqs])


class ProfileActionsView(_Base):
    """v0.12.0 — system-action audit log.

    Newest-first, default limit 50, capped at 500. Supports `from` and
    `to` (local YYYY-MM-DD) for windowed queries. Powers the panel
    Dashboard's "Recent activity" card.
    """

    url = f"{API_BASE}/profiles/{{profile_id}}/actions"
    name = f"api:{DOMAIN}:profile_actions"

    async def get(self, request: web.Request, profile_id: str) -> web.Response:
        bundle = _bundle_for(self._hass, profile_id)
        if bundle is None:
            return self._err("not_found", f"profile {profile_id!r}", 404)
        try:
            limit = int(request.query.get("limit", "50"))
        except ValueError:
            return self._err("invalid_payload", "limit must be int", 422)
        limit = max(1, min(limit, 500))

        from datetime import date as _date, datetime as _dt, time as _time, timedelta as _td

        local_now = dt_util.as_local(dt_util.utcnow())
        tz = local_now.tzinfo
        since = until = None
        if q := request.query.get("from"):
            try:
                d = _date.fromisoformat(q)
                since = dt_util.as_utc(_dt.combine(d, _time.min, tzinfo=tz))
            except ValueError:
                return self._err("invalid_payload", "from must be YYYY-MM-DD", 422)
        if q := request.query.get("to"):
            try:
                d = _date.fromisoformat(q)
                until = dt_util.as_utc(_dt.combine(d + _td(days=1), _time.min, tzinfo=tz))
            except ValueError:
                return self._err("invalid_payload", "to must be YYYY-MM-DD", 422)

        rows = bundle["store"].actions_for_profile(
            profile_id, limit=limit, since=since, until=until
        )
        return self._ok({
            "profile_id": profile_id,
            "count": len(rows),
            "limit": limit,
            "actions": [r.to_dict() for r in rows],
        })


# ---------- registration ---------------------------------------------------


VIEWS: tuple[type[_Base], ...] = (
    HealthView,
    OpenAPIView,
    ProfilesView,
    ProfileStatusView,
    ProfileGroupsView,
    ProfileUsageView,
    ProfileEventsView,
    ProfileLimitsView,
    AdultModeView,
    ExtensionView,
    RequestExtensionView,
    RequestView,
    RequestDecideView,
    ProfileRequestsView,
    ProfileActionsView,
)


def register_views(hass: HomeAssistant) -> None:
    """Register all REST views once per HA boot. Idempotent — HA's view
    registry refuses duplicates, so on subsequent setup_entry calls we
    silently skip."""
    for view_cls in VIEWS:
        try:
            hass.http.register_view(view_cls(hass))
        except RuntimeError:
            # Already registered — HA raises this on duplicate names.
            pass


# ---------- helpers --------------------------------------------------------


def _integration_version(hass: HomeAssistant) -> str:
    """Best-effort: pull the manifest version from HA's integration registry."""
    try:
        # Cheap, sync — HA caches the manifest on first read.
        from homeassistant.loader import async_get_loaded_integration

        integ = async_get_loaded_integration(hass, DOMAIN)
        return integ.version or "?"
    except Exception:  # noqa: BLE001 -- private API, never fatal
        return "?"


# ---------- OpenAPI --------------------------------------------------------


def _build_openapi(hass: HomeAssistant) -> dict[str, Any]:
    """Hand-rolled OpenAPI 3.1 spec for the views above. Kept compact —
    designed for autonomous agents (OpenClaw) to introspect."""
    base = API_BASE

    error = {
        "type": "object",
        "required": ["error", "message"],
        "properties": {
            "error": {"type": "string"},
            "message": {"type": "string"},
        },
    }
    profile_summary = {
        "type": "object",
        "required": [
            "id",
            "state",
            "used_today_min",
            "remaining_today_min",
            "budget_today_min",
            "effective_budget_today_min",
            "extension_minutes_today",
            "is_blocked",
            "adult_mode_active",
        ],
        "properties": {
            "id": {"type": "string"},
            "display_name": {"type": "string"},
            "apple_tv_entity_id": {"type": "string"},
            "state": {"type": "string", "enum": ["ok", "warning", "grace", "enforcing"]},
            "enforce_reason": {"type": ["string", "null"]},
            "current_bundle_id": {"type": ["string", "null"]},
            "current_group": {"type": ["string", "null"]},
            "used_today_min": {"type": "number"},
            "remaining_today_min": {"type": "number"},
            "budget_today_min": {
                "type": "integer",
                "description": "Base daily budget from config (excludes extensions)",
            },
            "effective_budget_today_min": {
                "type": "integer",
                "description": "budget_today_min + extension_minutes_today. The invariant remaining + used = effective_budget always holds.",
            },
            "extension_minutes_today": {"type": "integer"},
            "is_blocked": {"type": "boolean"},
            "adult_mode_active": {"type": "boolean"},
            "adult_mode_until": {"type": ["string", "null"], "format": "date-time"},
            "active_quiet_window": {"type": ["string", "null"]},
        },
    }
    extension_request = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "profile_id": {"type": "string"},
            "requested_minutes": {"type": "integer"},
            "granted_minutes": {"type": ["integer", "null"]},
            "reason": {"type": "string"},
            "bundle_id": {"type": ["string", "null"]},
            "requested_at": {"type": "string", "format": "date-time"},
            "auto_expires_at": {"type": "string", "format": "date-time"},
            "status": {"type": "string", "enum": ["pending", "approved", "denied", "expired"]},
            "decided_at": {"type": ["string", "null"], "format": "date-time"},
            "decided_by": {"type": ["string", "null"]},
        },
    }

    def _path(summary: str, ok_schema: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "summary": summary,
            "security": [{"bearerAuth": []}],
            "responses": {
                "200": {
                    "description": "OK",
                    "content": {"application/json": {"schema": ok_schema or {}}},
                },
                "401": {
                    "description": "Auth failed",
                    "content": {"application/json": {"schema": error}},
                },
                "404": {
                    "description": "Not found",
                    "content": {"application/json": {"schema": error}},
                },
            },
        }

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Apple TV Mgmt",
            "version": "1",
            "description": "Parental controls for Apple TV via Home Assistant.",
        },
        "servers": [{"url": ""}],  # relative to whatever host hits this
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"},
                "apiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                    "description": "Alternative to Authorization: Bearer for legacy clients",
                },
            },
            "parameters": {
                "ProfileId": {
                    "name": "profile_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                    "description": "Config entry id (also used as the profile identifier)",
                },
                "RequestId": {
                    "name": "request_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                },
                "DateFrom": {
                    "name": "from",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string", "format": "date"},
                    "description": "Inclusive local-date start (YYYY-MM-DD). Default: today.",
                },
                "DateTo": {
                    "name": "to",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string", "format": "date"},
                    "description": "Inclusive local-date end (YYYY-MM-DD). Default: same as from.",
                },
                "RequestStatusFilter": {
                    "name": "status",
                    "in": "query",
                    "required": False,
                    "schema": {
                        "type": "string",
                        "enum": ["pending", "approved", "denied", "expired"],
                    },
                },
            },
            "schemas": {
                "Error": error,
                "ProfileSummary": profile_summary,
                "ExtensionRequest": extension_request,
            },
        },
        "paths": {
            f"{base}/health": {
                "get": {
                    "summary": "Liveness + version + auth requirement",
                    "responses": {"200": {"description": "OK"}},
                }
            },
            f"{base}/profiles": {
                "get": _path("List configured profiles", {"type": "array", "items": profile_summary})
            },
            f"{base}/profiles/{{profile_id}}/status": {
                "parameters": [{"$ref": "#/components/parameters/ProfileId"}],
                "get": _path("Get current state for one profile", profile_summary),
            },
            f"{base}/profiles/{{profile_id}}/groups": {
                "parameters": [{"$ref": "#/components/parameters/ProfileId"}],
                "get": _path("Per-group totals + budgets for today"),
            },
            f"{base}/profiles/{{profile_id}}/usage": {
                "parameters": [{"$ref": "#/components/parameters/ProfileId"}],
                "get": _path("Per-app minutes used today"),
            },
            f"{base}/profiles/{{profile_id}}/events": {
                "parameters": [
                    {"$ref": "#/components/parameters/ProfileId"},
                    {"$ref": "#/components/parameters/DateFrom"},
                    {"$ref": "#/components/parameters/DateTo"},
                ],
                "get": _path(
                    "Chronological usage events for a date range. "
                    "Window capped at min(api_max=366, retention=90) days."
                ),
            },
            f"{base}/profiles/{{profile_id}}/limits": {
                "parameters": [{"$ref": "#/components/parameters/ProfileId"}],
                "get": _path(
                    "Read all configured limits for a profile, including "
                    "per-weekday overrides and a `today` snapshot of the "
                    "effective values."
                ),
                "post": {
                    "summary": (
                        "Mutate limits via POST (alias for PATCH — needed by "
                        "callers behind the HA Supervisor proxy, which doesn't "
                        "forward PATCH). Same request/response shape."
                    ),
                    "security": [{"bearerAuth": []}],
                    "requestBody": {
                        "content": {"application/json": {"schema": {"type": "object"}}}
                    },
                    "responses": {
                        "200": {"description": "Updated limits (full GET payload)"},
                        "422": {"description": "Validation failure"},
                    },
                },
                "patch": {
                    "summary": (
                        "Mutate any subset of profile limits. Fields not in "
                        "the body are left unchanged. Unknown fields are "
                        "rejected (422). NOTE: callers behind the HA Supervisor "
                        "proxy must use POST instead — the proxy drops PATCH."
                    ),
                    "security": [{"bearerAuth": []}],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                # v0.17.0 F-J — schema generated from the
                                # single-source-of-truth LIMITS_PATCH_FIELDS dict
                                # at the top of this module. Pre-v0.17.0 this
                                # was a hand-rolled subset of ~12 fields that
                                # silently lagged the validator's actual ~31-key
                                # allowed set; spec-respecting clients (like
                                # OpenClaw's skill generator) couldn't set mode,
                                # voice templates, monitor flags, etc.
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": dict(LIMITS_PATCH_FIELDS),
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {"description": "Updated limits (full GET payload)"},
                        "422": {"description": "Validation failure"},
                    },
                },
            },
            f"{base}/profiles/{{profile_id}}/adult_mode": {
                "parameters": [{"$ref": "#/components/parameters/ProfileId"}],
                "post": {
                    "summary": "Enable adult mode for N minutes (default: profile setting)",
                    "security": [{"bearerAuth": []}],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "minutes": {"type": "integer", "minimum": 1, "maximum": 1440}
                                    },
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "OK"}},
                },
                "delete": {
                    "summary": "Disable adult mode immediately",
                    "security": [{"bearerAuth": []}],
                    "responses": {"200": {"description": "OK"}},
                },
            },
            f"{base}/profiles/{{profile_id}}/extension": {
                "parameters": [{"$ref": "#/components/parameters/ProfileId"}],
                "post": {
                    "summary": "Add (or subtract) minutes to today's pool — no approval needed",
                    "security": [{"bearerAuth": []}],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["minutes"],
                                    "properties": {
                                        "minutes": {"type": "integer", "minimum": -240, "maximum": 240}
                                    },
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "OK"}},
                }
            },
            f"{base}/profiles/{{profile_id}}/request_extension": {
                "parameters": [{"$ref": "#/components/parameters/ProfileId"}],
                "post": {
                    "summary": "Kid asks for more time — creates a pending request + push to parent",
                    "security": [{"bearerAuth": []}],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["minutes"],
                                    "properties": {
                                        "minutes": {"type": "integer", "minimum": 1, "maximum": 240},
                                        "reason": {"type": "string", "maxLength": 280},
                                        "bundle_id": {"type": "string"},
                                    },
                                }
                            }
                        }
                    },
                    "responses": {
                        "201": {
                            "description": "Request created",
                            "content": {"application/json": {"schema": extension_request}},
                        }
                    },
                }
            },
            f"{base}/profiles/{{profile_id}}/requests": {
                "parameters": [
                    {"$ref": "#/components/parameters/ProfileId"},
                    {"$ref": "#/components/parameters/RequestStatusFilter"},
                ],
                "get": _path("List requests for a profile (optionally filtered by status)"),
            },
            f"{base}/profiles/{{profile_id}}/actions": {
                "parameters": [
                    {"$ref": "#/components/parameters/ProfileId"},
                    {"$ref": "#/components/parameters/DateFrom"},
                    {"$ref": "#/components/parameters/DateTo"},
                ],
                "get": _path(
                    "System-action audit log: enforce/release, "
                    "bypass_attempt (coalesced), decision, adult_mode, "
                    "extension, limits_changed. Newest first."
                ),
            },
            f"{base}/requests/{{request_id}}": {
                "parameters": [{"$ref": "#/components/parameters/RequestId"}],
                "get": _path("Poll an extension request", extension_request),
            },
            f"{base}/requests/{{request_id}}/decide": {
                "parameters": [{"$ref": "#/components/parameters/RequestId"}],
                "post": {
                    "summary": "Decide a request from a non-Companion source",
                    "security": [{"bearerAuth": []}],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["approve"],
                                    "properties": {
                                        "approve": {"type": "boolean"},
                                        "minutes": {"type": "integer"},
                                    },
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "Decided",
                            "content": {"application/json": {"schema": extension_request}},
                        }
                    },
                }
            },
        },
    }
