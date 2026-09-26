"""Constants for the Apple TV Mgmt integration."""
from __future__ import annotations

DOMAIN = "appletv_mgmt"

# Config entry keys
CONF_APPLE_TV_ENTITY = "apple_tv_entity_id"
# v0.19.0 — multi-device support.
CONF_DEVICE_KIND = "device_kind"
CONF_ENFORCEMENT_SWITCH_ENTITY_ID = "enforcement_switch_entity_id"
# v0.20.0 — unify two segments into ONE profile. A list of secondary devices
# folded into the primary profile's single shared budget. Each item is a dict:
#   {"entity_id", "device_kind", "enforcement_switch_entity_id", "bundle_id"}
# The PRIMARY device stays apple_tv_entity_id; secondaries (e.g. an Xbox
# device_tracker) accrue to the SAME daily/group budget and are blocked on
# exhaustion (switch.turn_off on enforcement_switch_entity_id).
CONF_SECONDARY_DEVICES = "secondary_devices"
DEVICE_KIND_APPLE_TV = "apple_tv"
DEVICE_KIND_XBOX_PRESENCE = "xbox_presence"
DEVICE_KINDS: tuple[str, ...] = (DEVICE_KIND_APPLE_TV, DEVICE_KIND_XBOX_PRESENCE)
CONF_ADGUARD_URL = "adguard_url"
CONF_ADGUARD_USER = "adguard_username"
CONF_ADGUARD_PASSWORD = "adguard_password"
CONF_ADGUARD_CLIENT_NAME = "adguard_client_name"
CONF_ADGUARD_API_KEY = "adguard_api_key"
CONF_DAILY_BUDGET_MIN = "daily_budget_min"
CONF_GRACE_SECONDS = "grace_seconds"
CONF_DAY_ROLLOVER_HOUR = "day_rollover_hour"   # v0.23.0
CONF_WARN_THRESHOLDS = "warn_thresholds_min"
CONF_PROFILE_NAME = "profile_name"
CONF_IDLE_GRACE_MINUTES = "idle_grace_minutes"
# Optional TV shutdown (Phase 1.5). When enforcement triggers, we already
# call media_player.turn_off on the Apple TV (which usually triggers HDMI-CEC
# TV-off if the chain works). For TVs where CEC doesn't reliably propagate —
# or where you want a hard kill — we can also call turn_off on a separately
# configured media_player entity that represents the TV itself.
CONF_TV_ENTITY_ID = "tv_entity_id"
CONF_TV_SHUTDOWN_ENABLED = "tv_shutdown_enabled"
# v0.21.0 — native TV watching ("Live TV"). When enabled, time the configured
# TV (tv_entity_id) spends ON with a source that is NOT one of the tracked
# devices (Apple TV on HDMI1, Xbox on HDMI2/DVI) is booked to the linear_tv
# group under the same room budget. Ships disabled — zero behavior change.
CONF_TRACK_NATIVE_TV = "track_native_tv"
CONF_NATIVE_TV_EXCLUDED_SOURCES = "native_tv_excluded_sources"
# v0.14.0 — monitor mode toggle. When false, the integration tracks +
# warns + audits, but doesn't touch AdGuard or sleep the Apple TV.
CONF_ENFORCEMENT_ENABLED = "enforcement_enabled"
DEFAULT_ENFORCEMENT_ENABLED = True
# Quiet windows — time-of-day ranges during which the integration forces
# enforcement regardless of remaining budget. See quiet.py for the format.
CONF_QUIET_WINDOWS = "quiet_windows"
# Per-group daily budgets — see categorize.py for group names.
# Stored as JSON in entry.options as {group_name: minutes}. Missing groups
# are treated as "no budget" (unlimited).
CONF_GROUP_BUDGETS = "group_budgets"
# Adult mode — time-boxed override that bypasses all enforcement.
CONF_ADULT_MODE_DURATION_MIN = "adult_mode_duration_min"
# REST API (Phase 3).
CONF_API_KEY = "api_key"             # Bearer token for /api/appletv_mgmt/*
CONF_NOTIFY_TARGET = "notify_target" # HA notify service (e.g. mobile_app_your_phone)
CONF_REQUEST_EXPIRE_MIN = "request_expire_min"  # auto-deny after N minutes

# Defaults
DEFAULT_DAILY_BUDGET_MIN = 60
DEFAULT_GRACE_SECONDS = 60
DEFAULT_WARN_THRESHOLDS = [5, 2, 0]
DEFAULT_IDLE_GRACE_MINUTES = 5
DEFAULT_PROFILE_NAME = "Living Room"
DEFAULT_TV_SHUTDOWN_ENABLED = False  # Capability ships disabled — user toggles via switch entity.
# v0.21.0 — native TV watching. Disabled by default (opt-in). The excluded
# sources default to the owner's tracked inputs: Apple TV on HDMI1, Xbox on
# HDMI2/DVI. A TV whose current source is in this list is never booked as
# native TV (it belongs to a tracked device on that input).
DEFAULT_TRACK_NATIVE_TV = False
DEFAULT_NATIVE_TV_EXCLUDED_SOURCES: list[str] = ["HDMI1", "HDMI2/DVI"]
DEFAULT_QUIET_WINDOWS = ""           # Empty string = no quiet windows = disabled.
# Per-group defaults — generous out of the box; user tightens via options flow.
DEFAULT_GROUP_BUDGETS: dict[str, int] = {
    "movies":   60,
    "tv_shows": 60,
    "gaming":   30,
    "other":    60,
    # v0.21.0 — native TV ("Live TV"). Defaults to 0 = unlimited, so the
    # feature is purely opt-in: enabling track_native_tv only TRACKS Live TV
    # until the parent sets a real cap. (0 = unlimited by the enforcer's
    # `> 0` / `<= 0` convention; also gives the options-flow budget_linear_tv
    # field a valid integer default.)
    "linear_tv": 0,
}
DEFAULT_ADULT_MODE_DURATION_MIN = 120  # 2 hours
DEFAULT_REQUEST_EXPIRE_MIN = 10        # auto-deny pending requests after 10 min
# Sentinel state for the adult mode switch.
STATE_ADULT_MODE = "adult_mode"

# Events fired on the HA bus when the request flow transitions.
EVENT_REQUEST_CREATED  = f"{DOMAIN}_request_created"
EVENT_REQUEST_DECIDED  = f"{DOMAIN}_request_decided"
# Fired when limits are mutated via REST PATCH (v0.11.0).
EVENT_LIMITS_UPDATED   = f"{DOMAIN}_limits_updated"

# Actionable-notification prefix; the action handler matches on this.
NOTIFY_ACTION_PREFIX = "APPLETV_MGMT_"

# REST API base.
API_BASE = f"/api/{DOMAIN}"

# Coordinator
COORDINATOR_TICK_SECONDS = 30
ENFORCER_REASSERT_SECONDS = 60
# When an event would be opened with the same bundle_id within this many
# seconds of the previous one ending, stitch them into a single session
# instead. The Apple TV integration's pyatv link drops + reconnects
# periodically during playback (~30-60 s blips), producing spurious "stop
# / start" events on the same continuous viewing session. The stitched
# session reflects the user's reality: one movie, one entry.
EVENT_STITCH_SECONDS = 60

# Storage
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_data"

# Enforcement states
STATE_OK = "ok"
STATE_WARNING = "warning"
STATE_GRACE = "grace"
STATE_ENFORCING = "enforcing"

# Media-player states we treat as "device on"
ACTIVE_MEDIA_STATES = {"playing", "paused", "buffering", "on", "idle"}
INACTIVE_MEDIA_STATES = {"off", "standby", "unavailable", "unknown", None}

# Events
EVENT_USAGE_UPDATED = f"{DOMAIN}_usage_updated"
EVENT_ENFORCEMENT_CHANGED = f"{DOMAIN}_enforcement_changed"
EVENT_APP_STARTED = f"{DOMAIN}_app_started"
EVENT_APP_ENDED = f"{DOMAIN}_app_ended"

# Friendly names for common Apple TV apps. The integration falls back to the
# bundle id when an app is not in this table — extend as needed without breaking
# anything. Sourced from observed app_id values reported by pyatv.
APP_DISPLAY_NAMES: dict[str, str] = {
    "com.google.ios.youtube": "YouTube",
    "com.netflix.Netflix": "Netflix",
    "com.apple.TVWatchList": "Apple TV",
    "com.apple.TVMovies": "Movies",
    "com.apple.TVShows": "TV Shows",
    "com.apple.TVMusic": "Apple Music",
    "com.apple.podcasts": "Podcasts",
    "com.apple.Fitness": "Apple Fitness",
    "com.amazon.aiv.AIVApp": "Prime Video",
    "tv.twitch": "Twitch",
    "com.spotify.client": "Spotify",
    "com.disney.disneyplus": "Disney+",
    "de.zdf.zdfmediathek.tvos": "ZDF Mediathek",
    "de.ard.mediathek.tvos": "ARD Mediathek",
    "tv.plex.player": "Plex",
    "com.jellyfin.jellyfin": "Jellyfin",
    "org.jellyfin.expo-mobile": "Jellyfin",
    # v0.19.0 — Xbox MVP synthetic bundle_id (see categorize.XBOX_CONSOLE_BUNDLE_ID).
    "xbox.console": "Xbox",
    # v0.21.0 — native TV synthetic bundle_id (see categorize.NATIVE_TV_BUNDLE_ID).
    "tv.native": "Live TV",
}


def app_display_name(bundle_id: str | None) -> str:
    """Friendly app name for UIs/logbook. Falls back to bundle_id or 'Idle'."""
    if not bundle_id:
        return "Idle"
    if bundle_id == "unknown":
        return "Unknown app"
    return APP_DISPLAY_NAMES.get(bundle_id, bundle_id)
