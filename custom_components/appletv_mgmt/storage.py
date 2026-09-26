"""Persistent storage for Apple TV Mgmt.

Phase 1 scope: a single Profile per Apple TV plus an append-only log of
UsageEvents. AppPolicy and ExtensionRequest are introduced in Phase 2/3.

Storage shape on disk (HA `.storage/appletv_mgmt_data`):

    {
      "profiles": [Profile, ...],
      "events":   [UsageEvent, ...],
      "extensions_granted_today": {"<profile_id>": minutes, ...}
    }

`extensions_granted_today` is reset at the same midnight rollover that
clears `events` older than the retention window.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from secrets import token_hex as _token_hex
from typing import Any, TypeVar

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .schedule import day_start
from .const import STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

# Keep UsageEvents for 90 days — covers the panel addon's "quarter" range.
# Raised from 30 in v0.10.0 (QA review: panel was silently showing 60 days
# of empty bars on the quarter view).
EVENT_RETENTION_DAYS = 90

# Keep decided extension requests for 30 days; pending ones never get pruned
# (only expired via expire_old_requests when past auto_expires_at).
REQUEST_RETENTION_DAYS = 30

# Keep system-action audit entries for 90 days (matches event retention)
# so the panel's quarter analytics can correlate enforce events with usage.
ACTION_LOG_RETENTION_DAYS = 90

# When 3+ ENFORCING transitions land within 5 minutes for the same profile,
# coalesce them into ONE `bypass_attempt` entry with a count rather than
# polluting the log with N enforce_start rows. The window is reset by any
# entry that *isn't* an enforce_start. (v0.12.0 data-driven: 05-22 produced
# ~30 fight fragments inside a 30-min standoff.)
BYPASS_COALESCE_WINDOW_S = 300
BYPASS_COALESCE_THRESHOLD = 3


@dataclass
class Profile:
    id: str
    display_name: str
    apple_tv_entity_id: str
    adguard_client_name: str
    daily_budget_min: int
    grace_seconds: int
    warn_thresholds_min: list[int]
    idle_grace_minutes: int
    # v0.17.1 — staleness threshold for the apple_tv_entity_id. When the
    # media_player entity stays in an ACTIVE state (playing/paused/
    # buffering/on/idle) but stops being refreshed for this many minutes,
    # the coordinator closes the open UsageEvent at the entity's last
    # `last_updated` timestamp. Catches pyatv silent disconnect: the
    # companion protocol drops, HA's last cached state remains "playing
    # com.disney.disneyplus", and pre-v0.17.1 the integration would
    # accumulate hours of phantom usage. Live-reported 2026-05-31 (the owner's
    # kid's Disney+ tracked 12.9 hours of phantom usage overnight).
    # Default 5 min; set to 0 to disable the check.
    stale_session_minutes: int = 5
    # v0.23.0 — hour at which the usage day rolls over. A household day does
    # not end at midnight: an adult watching until 02:00 would otherwise be
    # charged to the NEXT day and consume the children's budget before they
    # wake. 0 = the original midnight behaviour, kept as the default so
    # existing installs do not silently re-account on upgrade.
    day_rollover_hour: int = 0
    # Optional TV shutdown — generic, points at any HA media_player entity.
    # When `tv_shutdown_enabled` is True AND `tv_entity_id` is set, the
    # enforcer calls media_player.turn_off on that entity in addition to
    # the Apple TV. Default is disabled — the user has to flip the switch
    # entity (or set it in the options flow) to opt in.
    tv_entity_id: str | None = None
    tv_shutdown_enabled: bool = False
    # Comma-separated quiet-window string (e.g. "20:30-07:00:Bedtime, 12:00-14:00:Lunch").
    # Empty == no quiet windows. See quiet.py for format.
    quiet_windows: str = ""
    # Per-group daily budgets (minutes). Group name -> minutes. Missing keys
    # mean "no per-group budget" — the group is effectively unlimited.
    group_budgets: dict[str, int] = field(default_factory=dict)
    # Adult-mode duration in minutes when the override switch is flipped on.
    adult_mode_duration_min: int = 120
    # v0.14.0: monitor mode. When False, the integration tracks usage +
    # records audit entries + speaks warnings, but does NOT call AdGuard
    # or media_player.turn_off when ENFORCING. Useful for setup phase
    # (observing patterns before committing limits) and for selective
    # "let it slide tonight" without changing the budget. Toggleable via
    # the new EnforcementEnabledSwitch entity.
    enforcement_enabled: bool = True
    # ----- Per-weekday overrides (v0.11.0) -----
    # All three default to empty -> behave exactly like prior versions
    # (single budget + single set of quiet windows for every day).
    # Keys: "mon","tue","wed","thu","fri","sat","sun" (see schedule.WEEKDAYS).
    #
    # When a key is present, it OVERRIDES the corresponding base field
    # for that weekday. When absent, the base field is used.
    weekday_budgets_min: dict[str, int] = field(default_factory=dict)
    weekday_group_budgets_min: dict[str, dict[str, int]] = field(default_factory=dict)
    weekday_quiet_windows: dict[str, str] = field(default_factory=dict)
    # ----- Voice announcements (v0.13.0) -----
    # When `notify_media_player_entity_id` is set, the integration speaks
    # the configured message via the configured tts entity on state-machine
    # transitions to WARNING and ENFORCING. Empty / unset → silent.
    #
    # Implementation notes:
    # - `notify_volume`: 0.0–1.0. If > 0, the integration sets the media
    #   player's volume to this value RIGHT BEFORE speaking. We don't
    #   restore the previous volume — Sonos / most players keep this
    #   level for music too, so the parent can tune it once.
    # - `warning_message` / `enforce_message` support {minutes} and {app}
    #   placeholders (substituted at speak time). Empty string disables
    #   that trigger without touching the media-player config.
    # - `notify_tts_entity_id`: typically an entity from the `tts.*`
    #   domain (e.g. tts.google_translate_en_com, tts.cloud_say). Empty
    #   = use whatever HA picks as default.
    notify_media_player_entity_id: str = ""
    notify_tts_entity_id: str = ""
    # Empty = fall back to HA's configured language at speak time (v0.20.1).
    # A per-profile override (e.g. "de") still wins when set.
    notify_tts_language: str = ""
    notify_volume: float = 0.35
    warning_message: str = ""
    enforce_message: str = ""
    # v0.14.1 — spoken when extension minutes are granted (manual +N
    # via /extension OR a kid's request being approved). Supports
    # {minutes} (the granted amount). Empty disables the trigger.
    extension_message: str = ""

    # ----- v0.15.0 mode redesign -----
    # See policy.py + spec §3.1, §3.4. "enforced" | "monitor_only" | "paused".
    # Replaces the legacy `enforcement_enabled` bool — that field is kept
    # on the dataclass for round-trip safety + the §4.2 reconciliation
    # rules, but `mode` is the runtime source of truth.
    mode: str = "enforced"

    # New canonical TV-shutdown target. See spec §3.5. The legacy
    # `tv_shutdown_enabled` + `tv_entity_id` fields above are reclassified
    # as "memory" — `tv_entity_id` is preserved as last-known target so a
    # legacy `_tv_shutdown` switch ON-toggle can restore it. None == off.
    tv_shutdown_target: str | None = None

    # v0.15.5 — opt-in AdGuard DNS blocking. Was always-on through v0.15.4;
    # now defaults OFF because the owner's setup uses Samsung TV (via the
    # samsungtv_encrypted integration / SmartTV-Tooling repo) as the
    # primary kill switch plus the v0.15.4 pyatv watchdog — AdGuard's
    # DNS-blocking role is supplementary, not essential. Opt back in via
    # PATCH /limits {"enable_adguard_block": true} if you want the
    # extra "no new DNS lookups for the Apple TV" layer.
    enable_adguard_block: bool = False

    # Per-profile flags surfaced via PATCH /limits.
    # Per PO D2: default OFF — monitor mode is silent by default.
    # Opt in by PATCHing warn_in_monitor_mode=True if you want to hear
    # what enforcement WOULD say while calibrating budgets.
    warn_in_monitor_mode: bool = False

    # If True, mode_change_message is spoken on every mode change.
    voice_on_mode_change: bool = False

    # New optional voice templates. Empty = silent (no-op).
    adult_mode_on_message: str = ""    # supports {duration_minutes}, {until}
    mode_change_message: str = ""      # supports {old_mode}, {new_mode}

    # ----- v0.16.0 anti-defeat features -----
    # Spoken at ~30s before enforce_at_time (the final cue after the
    # warn_thresholds_min voices). Empty = silent. No placeholders — the
    # message is static (the existing {minutes} placeholder would render
    # 0 at this point, which is confusing). Suggested German default:
    #   "Achtung! Noch 30 Sekunden Bildschirmzeit."
    countdown_message: str = ""
    # Spoken on the 1st Apple TV re-on under ENFORCING (friendly tone).
    # Empty = silent. No placeholders.
    # Suggested German default:
    #   "Bildschirmzeit ist vorbei. Apple TV bitte aus lassen."
    reactivation_message_friendly: str = ""
    # Spoken on the 2nd+ re-on under ENFORCING (stern tone). Also triggers
    # the parent push (see notify_parent_target). Empty = silent.
    # Suggested German default:
    #   "Apple TV bleibt aus. Die Eltern wurden jetzt informiert."
    reactivation_message_stern: str = ""
    # Notification target for the parent push on the 2nd+ re-on. Empty
    # uses HA's default `notify.notify` (fans out to all configured
    # notification services). Set to e.g. "mobile_app_your_phone" for a
    # targeted push.
    notify_parent_target: str = ""
    # v0.18.0 — DNS-corroborated attribution. When the apple_tv_entity_id's
    # pyatv push goes silent for long stretches (tvOS 26 4K issue), this
    # feature uses AdGuard's DNS query log for the Apple TV's IP to detect
    # mid-session app changes (kid switches Disney+ -> game) that pyatv
    # missed. Three modes:
    #   - "off":     v0.17.x behavior exactly; classifier NOT consulted.
    #                Default on upgrade — no regression risk.
    #   - "monitor": Classifier runs every coordinator tick, audit log
    #                records PROPOSED corrections, but UsageEvent.group_
    #                segments is NOT modified. Use for ~1-2 weeks to verify
    #                classifications match reality.
    #   - "correct": Classifier runs, corrections applied via group_segments,
    #                budgets honor the corrected groups.
    # apple_tv_ip MUST be set (and match the actual Apple TV's IP on the
    # LAN) for this feature to do anything regardless of mode. Empty
    # apple_tv_ip forces the feature OFF.
    apple_tv_ip: str = ""
    dns_corroboration_mode: str = "off"  # off | monitor | correct
    # v0.18.0 — HA ConfigEntry.entry_id of the *Apple TV core integration*
    # (the entry that owns the media_player.apple_tv_* entity). Used by
    # the proactive pyatv reload feature (see
    # media_attribution.decide_pyatv_reload): when pyatv's push channel
    # goes silent for >= 10 min while Samsung is `on` and DNS is firing,
    # the coordinator calls `hass.config_entries.async_reload(...)` on
    # this id to restart pyatv. Default empty string disables the
    # feature (and is the back-compat value for profiles persisted before
    # v0.18.x).
    #
    # NOTE: this is NOT Profile.id (which IS the entry_id of OUR
    # integration). The pyatv reload needs the OTHER integration's id.
    # Resolved at setup time by looking up `apple_tv_entity_id` in the
    # HA entity registry; can also be PATCHed via /limits as an override.
    apple_tv_entry_id: str = ""

    # v0.19.0 — multi-device support. The integration was Apple-TV-only
    # through v0.18.x. v0.19.0 adds a second device kind: Xbox via the
    # FRITZ!Box presence/switch surface (NO Xbox Live integration needed).
    #
    # Values:
    #   - "apple_tv"       — default for back-compat. Profile uses pyatv
    #                        media_player + AdGuard block + Samsung-TV
    #                        shutdown fallback. All v0.17/v0.18 pyatv-
    #                        specific hot paths (stale-session, in-
    #                        integration reload, DNS corroboration) ARE
    #                        active for this kind.
    #   - "xbox_presence"  — Xbox tracked via a device_tracker.* presence
    #                        entity (typically FRITZ!Box-driven). Activity
    #                        = device_tracker.state == "home". Enforcement
    #                        is a switch flip on enforcement_switch_entity_id
    #                        (typically the FRITZ!Box-driven
    #                        switch.xboxone_internet_access). No per-app
    #                        tracking — all Xbox time is bucketed as the
    #                        synthetic bundle_id "xbox.console" → "gaming"
    #                        group. Apple-TV-specific hot paths are SKIPPED.
    #
    # Field name `apple_tv_entity_id` is overloaded: for "apple_tv" it's
    # the media_player; for "xbox_presence" it's the device_tracker. The
    # name stayed for back-compat; renaming would invalidate stored configs
    # for the live install. Treat it as "the primary tracking entity".
    device_kind: str = "apple_tv"

    # v0.19.0 — Xbox MVP enforcement target. For device_kind="xbox_presence",
    # this is the switch.* entity the enforcer turns OFF to block Xbox
    # internet access (typically switch.xboxone_internet_access from the
    # FRITZ!Box integration). When set to None or empty, enforcement is a
    # no-op for the network side (TV-shutdown fallback may still apply if
    # configured separately). For device_kind="apple_tv" this field is
    # unused and ignored.
    enforcement_switch_entity_id: str | None = None

    # v0.20.0 — ONE system, not two. Secondary devices folded into THIS profile's
    # single shared budget. Each item is a dict:
    #   {"entity_id": str,                        # watched entity (e.g. device_tracker.xboxone)
    #    "device_kind": str,                      # "xbox_presence"
    #    "enforcement_switch_entity_id": str|None, # switch flipped OFF to block it
    #    "bundle_id": str}                         # synthetic bundle ("xbox.console" → gaming)
    # The PRIMARY device stays apple_tv_entity_id. Activity on EITHER the primary
    # OR any secondary accrues to the ONE event stream (room-as-unit, never
    # double-counted — Apple-TV-wins priority collapses overlap to a single
    # bundle). Empty list (default) ⇒ behaves bit-identically to pre-v0.20.0.
    # Round-trips automatically via asdict + the from_dict known-field filter.
    secondary_devices: list[dict] = field(default_factory=list)

    # v0.21.0 — native TV watching ("Live TV"). When True AND a TV entity is
    # configured (tv_entity_id — the SAME field the tv-on accumulator reads),
    # time the TV spends ON with a source NOT in `native_tv_excluded_sources`
    # is booked to the synthetic bundle "tv.native" → group "linear_tv" under
    # the same room budget. Precedence is apple_tv > secondaries > native TV,
    # so it only ever claims the room when nothing tracked is active (see
    # coordinator._resolve_room_activity). Ships disabled — an install that
    # never flips it behaves bit-identically to pre-v0.21.0.
    track_native_tv: bool = False
    # Sources that are NOT native TV (a tracked device owns that input).
    # Defaults to the owner's tracked inputs (Apple TV=HDMI1, Xbox=HDMI2/DVI).
    # A TV whose current `source` attribute is in this list is never booked as
    # native TV. Missing/empty source ⇒ fail closed (no booking).
    native_tv_excluded_sources: list[str] = field(
        default_factory=lambda: ["HDMI1", "HDMI2/DVI"]
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        """Reconstruct a Profile from persisted dict, with v0.15.0 migrations.

        Per spec §4.2 — silent reconciliation (no audit rows). The migration
        runs at the top of `from_dict`, before the field-filter pass so the
        existing round-trip semantics are preserved for unknown keys.
        """
        # Step 1: migrate mode if not present
        if "mode" not in data:
            ee = data.get("enforcement_enabled", True)
            data = {**data, "mode": "enforced" if ee else "monitor_only"}
        else:
            # Step 2: if both present, mode wins; reconcile enforcement_enabled
            ee_implied = data["mode"] == "enforced"
            if data.get("enforcement_enabled") != ee_implied:
                data = {**data, "enforcement_enabled": ee_implied}
        # Step 3: migrate tv_shutdown_target (see §3.5)
        if "tv_shutdown_target" not in data:
            if data.get("tv_shutdown_enabled") and data.get("tv_entity_id"):
                data = {**data, "tv_shutdown_target": data["tv_entity_id"]}
            else:
                data = {**data, "tv_shutdown_target": None}
        # Step 4: filter unknown keys, construct
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class ExtensionRequest:
    """A request from an external consumer (typically a kid via OpenClaw)
    asking the parent for more screen time. Persisted so the kid can poll
    for the decision asynchronously.

    Status transitions:
      pending -> approved   (parent tapped "Approve")
      pending -> denied     (parent tapped "Deny")
      pending -> expired    (auto, when now >= auto_expires_at)
    """

    id: str
    profile_id: str
    requested_minutes: int
    reason: str
    requested_at: datetime
    auto_expires_at: datetime
    status: str = "pending"
    granted_minutes: int | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None
    bundle_id: str | None = None  # what app the kid was using

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "profile_id": self.profile_id,
            "requested_minutes": self.requested_minutes,
            "reason": self.reason,
            "requested_at": self.requested_at.isoformat(),
            "auto_expires_at": self.auto_expires_at.isoformat(),
            "status": self.status,
            "granted_minutes": self.granted_minutes,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "decided_by": self.decided_by,
            "bundle_id": self.bundle_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExtensionRequest":
        return cls(
            id=data["id"],
            profile_id=data["profile_id"],
            requested_minutes=int(data["requested_minutes"]),
            reason=data.get("reason", ""),
            requested_at=datetime.fromisoformat(data["requested_at"]),
            auto_expires_at=datetime.fromisoformat(data["auto_expires_at"]),
            status=data.get("status", "pending"),
            granted_minutes=(
                int(data["granted_minutes"]) if data.get("granted_minutes") is not None else None
            ),
            decided_at=(
                datetime.fromisoformat(data["decided_at"]) if data.get("decided_at") else None
            ),
            decided_by=data.get("decided_by"),
            bundle_id=data.get("bundle_id"),
        )


@dataclass
class ActionLogEntry:
    """One row in the system-action audit log.

    Added v0.12.0 — surfaces "what did the integration do?" on the panel
    Dashboard. Previously a hidden state machine; now an auditable timeline.

    `action` is one of:
      - "enforce_start"   — entered ENFORCING (TV was slept)
      - "enforce_end"     — returned to OK (TV released)
      - "bypass_attempt"  — coalesced: kid kept reopening after enforce
                            (set `count` to how many times in the window)
      - "warn"            — entered WARNING (approaching budget)
      - "decision"        — request approved/denied/expired (see detail)
      - "adult_mode_on" / "adult_mode_off"
      - "extension"       — minutes granted (see detail)
      - "limits_changed"  — profile config updated via PATCH /limits

    `reason` is free-form ("daily_limit", "group:movies", "quiet:bedtime",
    or a short human string for the decision/admin actions).
    """

    id: str
    profile_id: str
    at: datetime
    action: str
    reason: str | None = None
    detail: str | None = None      # human-readable summary (1-line)
    count: int = 1                 # coalesce counter for bypass_attempt
    bundle_id: str | None = None   # app in play when action fired
    # v0.15.0 — who/what triggered this action. Documented enum (free-form
    # string, accepted verbatim for forward-compat):
    #   switch_entity | select_entity | service | rest | panel |
    #   options_flow | companion | automation | voice_assist | migration
    # `None` for old persisted rows that pre-date v0.15.0.
    actor: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "profile_id": self.profile_id,
            "at": self.at.isoformat(),
            "action": self.action,
            "reason": self.reason,
            "detail": self.detail,
            "count": int(self.count or 1),
            "bundle_id": self.bundle_id,
            "actor": self.actor,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionLogEntry":
        return cls(
            id=data["id"],
            profile_id=data["profile_id"],
            at=datetime.fromisoformat(data["at"]),
            action=data["action"],
            reason=data.get("reason"),
            detail=data.get("detail"),
            count=int(data.get("count", 1) or 1),
            bundle_id=data.get("bundle_id"),
            actor=data.get("actor"),
        )


@dataclass
class GroupSegment:
    """v0.18.0 — A sub-slice of a UsageEvent annotated with a corrected group.

    Exists because pyatv on tvOS 26 4K goes push-silent during real playback,
    leaving `bundle_id` frozen at whatever was reported when the event opened.
    The v0.18.0 DNS classifier can detect that the kid switched apps mid-
    session (Disney+ -> game) and annotate the event WITHOUT closing it —
    preserving the audit trail's truth ("event was opened as Disney+ but
    minutes M..N reclassified to gaming by DNS evidence").

    `ended_at == None` means this segment is still active (the event hasn't
    closed and the group hasn't changed since this segment was appended).

    Segments are append-only — corrections appear as new segments rather than
    mutations of earlier ones. The order is `started_at` ascending; the most
    recent open segment is the current attribution.
    """

    started_at: datetime
    ended_at: datetime | None
    group: str
    source: str = "dns_classifier"   # who proposed this segment
    confidence: str = ""              # DnsClassification.confidence as string

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "group": self.group,
            "source": self.source,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GroupSegment":
        return cls(
            started_at=datetime.fromisoformat(data["started_at"]),
            ended_at=(
                datetime.fromisoformat(data["ended_at"])
                if data.get("ended_at") else None
            ),
            group=data["group"],
            source=data.get("source", "dns_classifier"),
            confidence=data.get("confidence", ""),
        )


@dataclass
class UsageEvent:
    """One stretch of time the Apple TV was attributed to a bundle id.

    `ended_at == None` means the event is still open (the device is still
    on that app right now). Exactly one event per profile may be open.

    v0.18.0: `group_segments` is an optional list of `GroupSegment` annotations
    that override the bundle's curated group for sub-slices of the event.
    Empty list means the entire event maps to the bundle's curated group
    (bit-identical to pre-v0.18.0 behavior — used by every legacy event on
    disk).
    """

    id: str
    profile_id: str
    bundle_id: str
    started_at: datetime
    ended_at: datetime | None = None
    group_segments: list[GroupSegment] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "profile_id": self.profile_id,
            "bundle_id": self.bundle_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
        }
        # Only serialize segments when present — keeps storage bit-identical
        # for legacy events that never had any corrections.
        if self.group_segments:
            d["group_segments"] = [s.to_dict() for s in self.group_segments]
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UsageEvent":
        return cls(
            id=data["id"],
            profile_id=data["profile_id"],
            bundle_id=data["bundle_id"],
            started_at=datetime.fromisoformat(data["started_at"]),
            ended_at=datetime.fromisoformat(data["ended_at"]) if data["ended_at"] else None,
            group_segments=[
                GroupSegment.from_dict(s)
                for s in (data.get("group_segments") or [])
            ],
        )

    def duration_seconds(self, now: datetime | None = None) -> int:
        """Length of this event. Open events count up to `now` (or dt_util.utcnow)."""
        end = self.ended_at or now or dt_util.utcnow()
        return max(0, int((end - self.started_at).total_seconds()))

    def group_seconds_breakdown(
        self,
        bundle_curated_group: str,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """v0.18.0 — return {group: seconds} for this event, honoring
        `group_segments` where present.

        Algorithm:
        - If `group_segments` is empty (legacy events, no corrections), the
          full duration is attributed to `bundle_curated_group`. Bit-identical
          to pre-v0.18.0 storage.
        - Otherwise the event timeline `[started_at, end]` is sliced. Each
          GroupSegment's `[started_at, ended_at or end]` window contributes
          to its `group`. Time NOT covered by any segment maps to the
          bundle's curated group (typically the time before the first
          correction fired).

        Caller passes `bundle_curated_group` rather than the function looking
        it up because categorize() depends on HA-installed components and we
        keep storage HA-free where possible.
        """
        end = self.ended_at or now or dt_util.utcnow()
        total = max(0, int((end - self.started_at).total_seconds()))
        if not self.group_segments or total == 0:
            return {bundle_curated_group: total}

        # Build {group: seconds} from segments, then assign leftover (gaps
        # between segments or before the first one) to the curated group.
        breakdown: dict[str, int] = {}
        # Walk segments in started_at order — they are append-only so already
        # sorted, but we sort defensively.
        segs = sorted(self.group_segments, key=lambda s: s.started_at)
        cursor = self.started_at
        for seg in segs:
            seg_start = max(self.started_at, seg.started_at)
            seg_end = min(end, seg.ended_at or end)
            if seg_end <= seg_start:
                continue
            # Gap from cursor to seg_start belongs to curated group.
            if seg_start > cursor:
                gap = int((seg_start - cursor).total_seconds())
                if gap > 0:
                    breakdown[bundle_curated_group] = (
                        breakdown.get(bundle_curated_group, 0) + gap
                    )
            seg_secs = int((seg_end - seg_start).total_seconds())
            if seg_secs > 0:
                breakdown[seg.group] = breakdown.get(seg.group, 0) + seg_secs
            cursor = max(cursor, seg_end)

        # Tail (after the last segment, before `end`) belongs to curated.
        if cursor < end:
            tail = int((end - cursor).total_seconds())
            if tail > 0:
                breakdown[bundle_curated_group] = (
                    breakdown.get(bundle_curated_group, 0) + tail
                )

        return breakdown


@dataclass
class RuntimeState:
    """v0.17.0 F-E (Opus BA audit P1) — persisted enforce-cycle state.

    Lets the EnforcementController survive HA restarts in the middle of
    an enforcement cycle. Without this, the constructor defaults
    (`_state=OK`, `_grace_started_at=None`, `_reactivation_count=0`,
    etc.) erased any in-flight state — a kid past their budget could
    earn ~75 s of free TV per HA restart because the first post-restart
    evaluate would walk a fresh OK→WARN→GRACE cycle. Worst-case for
    the owner's setup (v0.15.5 default `enable_adguard_block=False`) because
    that disables the only other state-recovery path (seed_from_adguard).

    All fields default to the same values the controller's constructor
    would set, so an empty/None RuntimeState is equivalent to "fresh
    boot, no persistence". Adversarial-required sanity check on restore
    lives in `EnforcementController.seed_from_runtime_state`: if the
    persisted `grace_started_at` is older than 2× grace_seconds, the
    cycle is treated as expired and state is dropped to ENFORCING
    rather than computing a negative grace delta.
    """

    state: str = "ok"
    grace_started_at: datetime | None = None
    enforce_reason: str | None = None
    reactivation_count: int = 0
    was_apple_tv_active_last_tick: bool = False
    last_enforcement_failed: bool = False
    # Updated on every persist — used by load-time sanity checks (e.g.
    # if the state was written months ago, it's definitely stale).
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "grace_started_at": (
                self.grace_started_at.isoformat() if self.grace_started_at else None
            ),
            "enforce_reason": self.enforce_reason,
            "reactivation_count": int(self.reactivation_count),
            "was_apple_tv_active_last_tick": bool(self.was_apple_tv_active_last_tick),
            "last_enforcement_failed": bool(self.last_enforcement_failed),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeState":
        def _dt(value: str | None) -> datetime | None:
            if not value:
                return None
            try:
                return datetime.fromisoformat(value)
            except (TypeError, ValueError):
                return None

        return cls(
            state=str(data.get("state", "ok")),
            grace_started_at=_dt(data.get("grace_started_at")),
            enforce_reason=data.get("enforce_reason"),
            reactivation_count=int(data.get("reactivation_count", 0)),
            was_apple_tv_active_last_tick=bool(
                data.get("was_apple_tv_active_last_tick", False)
            ),
            last_enforcement_failed=bool(data.get("last_enforcement_failed", False)),
            updated_at=_dt(data.get("updated_at")),
        )


_T = TypeVar("_T")


def _safe_decode_list(
    items: list[Any],
    *,
    decode: Callable[[dict[str, Any]], _T],
    label: str,
) -> list[_T]:
    """Decode items, logging + skipping malformed records.

    Added in v0.10.0 (QA: prevent a single bad record from killing the
    integration on restart).
    """
    out: list[_T] = []
    for i, raw in enumerate(items):
        try:
            out.append(decode(raw))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Dropping malformed %s at index %d: %s (raw=%r)", label, i, err, raw
            )
    return out


def _safe_decode_map(
    items: list[Any],
    *,
    key: Callable[[dict[str, Any]], str],
    decode: Callable[[dict[str, Any]], _T],
    label: str,
) -> dict[str, _T]:
    """Like _safe_decode_list, but materializes a dict keyed by `key(raw)`."""
    out: dict[str, _T] = {}
    for raw in items:
        try:
            out[key(raw)] = decode(raw)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Dropping malformed %s: %s (raw=%r)", label, err, raw
            )
    return out


class AppleTVMgmtStore:
    """Wrapper around HA's Store with Phase-1 query helpers."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._profiles: dict[str, Profile] = {}
        self._events: list[UsageEvent] = []
        self._extensions_granted_today: dict[str, int] = {}
        # v0.19.1 — per-group extension pool. `{profile_id: {group: minutes}}`.
        # When the parent grants an extension while a GROUP budget is the
        # binding constraint (e.g. movies 30m exhausted while the daily pool
        # still has hours), the grant is tagged to that group so it lifts the
        # group budget too — not just the daily pool. Without this, "+30 min"
        # was a no-op exactly when a group sub-limit was what's biting (live-
        # reported 2026-06-19: parent granted +90 against a 30m movies cap and
        # the nagging continued until adult mode). The daily pool
        # (`_extensions_granted_today`) STILL receives every grant — group
        # tagging is additive, so an extension always lifts daily AND
        # (optionally) one group. Tagged to a single group by design so
        # switching apps can't transfer the granted time (anti-defeat).
        self._group_extensions_today: dict[str, dict[str, int]] = {}
        # Global (cross-profile) cache of bundle_id -> group, populated by
        # the iTunes lookup. CURATED in categorize.py takes precedence.
        self._app_categories: dict[str, str] = {}
        # Adult mode expiry per profile (UTC datetime ISO). `None` = off.
        self._adult_mode_until: dict[str, datetime] = {}
        # Extension requests keyed by id, across all profiles. Pruned daily.
        self._requests: dict[str, ExtensionRequest] = {}
        # System-action audit log (v0.12.0). Append-only, pruned to
        # `ACTION_LOG_RETENTION_DAYS`. Newest entries are at the end.
        self._actions: list[ActionLogEntry] = []
        # v0.17.0 F-E — persisted enforce-cycle state per profile.
        # Lets the EnforcementController survive HA restarts mid-cycle
        # without resetting `_state`, `_grace_started_at`, or the
        # reactivation counter.
        self._runtime_states: dict[str, RuntimeState] = {}

    # ----- lifecycle -----

    async def async_load(self) -> None:
        """Load persisted state. Per-record errors are logged and the
        bad record is skipped — a single corrupt entry must not crash
        the whole integration on restart (caught by QA review 2026-05).
        """
        try:
            data = await self._store.async_load() or {}
        except Exception as err:  # noqa: BLE001 -- storage backend is opaque
            _LOGGER.error(
                "Failed to load %s; falling back to empty state: %s",
                STORAGE_KEY,
                err,
            )
            data = {}

        self._profiles = _safe_decode_map(
            data.get("profiles", []),
            key=lambda p: p["id"],
            decode=Profile.from_dict,
            label="profile",
        )
        self._events = _safe_decode_list(
            data.get("events", []),
            decode=UsageEvent.from_dict,
            label="event",
        )
        self._extensions_granted_today = dict(data.get("extensions_granted_today", {}))
        # v0.19.1 — per-group extension pool. Back-compat: absent key → {}.
        # Coerce the inner dicts so a malformed dump can't crash the load.
        self._group_extensions_today = {}
        for pid, groups in (data.get("group_extensions_today") or {}).items():
            if isinstance(groups, dict):
                self._group_extensions_today[pid] = {
                    str(g): int(m) for g, m in groups.items()
                    if isinstance(m, (int, float))
                }
        self._app_categories = dict(data.get("app_categories", {}))
        self._adult_mode_until = {}
        for pid, ts in (data.get("adult_mode_until") or {}).items():
            try:
                self._adult_mode_until[pid] = datetime.fromisoformat(ts)
            except (TypeError, ValueError) as err:
                _LOGGER.warning(
                    "Dropping bad adult_mode_until for %s (%r): %s", pid, ts, err
                )
        self._requests = _safe_decode_map(
            data.get("requests") or [],
            key=lambda r: r["id"],
            decode=ExtensionRequest.from_dict,
            label="request",
        )
        self._actions = _safe_decode_list(
            data.get("action_log") or [],
            decode=ActionLogEntry.from_dict,
            label="action",
        )
        # v0.17.0 F-E — restore per-profile runtime state. Optional key:
        # absent → empty dict → controllers boot with constructor defaults
        # (same as pre-v0.17.0 behavior). Safe migration.
        self._runtime_states = {}
        for pid, payload in (data.get("runtime_states") or {}).items():
            try:
                self._runtime_states[pid] = RuntimeState.from_dict(payload)
            except (TypeError, ValueError, KeyError) as err:
                _LOGGER.warning(
                    "Dropping bad runtime_state for %s (%r): %s",
                    pid, payload, err,
                )
        # Prune old events on load so the file never grows unbounded.
        self._prune_events()
        self._prune_actions()

    async def async_save(self) -> None:
        self._prune_events()
        self._prune_actions()
        await self._store.async_save(
            {
                "profiles": [p.to_dict() for p in self._profiles.values()],
                "events": [e.to_dict() for e in self._events],
                "extensions_granted_today": self._extensions_granted_today,
                "group_extensions_today": self._group_extensions_today,
                "app_categories": self._app_categories,
                "adult_mode_until": {
                    pid: ts.isoformat() for pid, ts in self._adult_mode_until.items()
                },
                "requests": [r.to_dict() for r in self._requests.values()],
                "action_log": [a.to_dict() for a in self._actions],
                # v0.17.0 F-E — enforce-cycle persistence per profile.
                "runtime_states": {
                    pid: rs.to_dict() for pid, rs in self._runtime_states.items()
                },
            }
        )

    # ----- app categorization cache -----

    def get_cached_category(self, bundle_id: str) -> str | None:
        return self._app_categories.get(bundle_id)

    def cache_category(self, bundle_id: str, group: str) -> None:
        self._app_categories[bundle_id] = group

    def app_categories(self) -> dict[str, str]:
        """Returns a copy of the iTunes-derived bundle_id -> group cache."""
        return dict(self._app_categories)

    # ----- adult mode -----

    def set_adult_mode_until(self, profile_id: str, until: datetime | None) -> None:
        # v0.15.0 — guard against TZ-naive callers landing in storage and
        # then crashing later when the policy module compares with
        # tz-aware `now`. Cheap defense; surfaces caller bug at the right
        # layer.
        assert until is None or until.tzinfo is not None, (
            "adult_mode_until must be TZ-aware"
        )
        if until is None:
            self._adult_mode_until.pop(profile_id, None)
        else:
            self._adult_mode_until[profile_id] = until

    def adult_mode_until(self, profile_id: str) -> datetime | None:
        return self._adult_mode_until.get(profile_id)

    # ----- runtime state (v0.17.0 F-E) -----

    def get_runtime_state(self, profile_id: str) -> RuntimeState | None:
        """Return the persisted enforce-cycle state for `profile_id`,
        or None if none has been recorded yet (fresh boot / no prior
        transitions persisted). The caller (EnforcementController) is
        responsible for the sanity check on `grace_started_at` staleness.
        """
        return self._runtime_states.get(profile_id)

    def set_runtime_state(
        self, profile_id: str, state: RuntimeState
    ) -> None:
        """Replace the persisted enforce-cycle state for `profile_id`.
        Caller is expected to set `state.updated_at = dt_util.utcnow()`
        before calling so load-time staleness checks have a timestamp
        to consult. Persistence happens on the next `async_save()`.
        """
        self._runtime_states[profile_id] = state

    def is_adult_mode_active_at(
        self, profile_id: str, now: datetime
    ) -> bool:
        """Pure: returns True iff adult mode is active for `profile_id` at `now`.

        Added in v0.15.0 to provide a side-effect-free accessor for the
        policy module (see spec §3.4). Does NOT mutate state — if the
        entry has expired, this just returns False; cleanup is the job
        of `purge_expired_adult_mode`.
        """
        until = self._adult_mode_until.get(profile_id)
        if until is None:
            return False
        return until > now

    def purge_expired_adult_mode(self, *, now: datetime | None = None) -> list[str]:
        """Drop expired adult_mode entries from disk.

        Returns the list of profile_ids whose entry was just dropped, so
        callers can `await async_save()` if needed. Pure-ish: only
        mutates internal state; no HA bus events.
        """
        now = now or dt_util.utcnow()
        dropped: list[str] = []
        for pid in list(self._adult_mode_until):
            until = self._adult_mode_until[pid]
            if now >= until:
                self._adult_mode_until.pop(pid, None)
                dropped.append(pid)
        return dropped

    def is_adult_mode_active(
        self, profile_id: str, *, now: datetime | None = None
    ) -> bool:
        """Legacy accessor — kept for back-compat with v0.14.x callers.

        Mutates internal state on expiry (pops the entry). New code should
        prefer `is_adult_mode_active_at(now)` (pure) + a separate
        `purge_expired_adult_mode()` mutator.
        """
        now = now or dt_util.utcnow()
        until = self._adult_mode_until.get(profile_id)
        if until is None:
            return False
        if now >= until:
            # Auto-expire — caller is expected to async_save() shortly.
            self._adult_mode_until.pop(profile_id, None)
            return False
        return True

    # ----- extension requests -----

    def add_request(self, request: ExtensionRequest) -> None:
        self._requests[request.id] = request

    def get_request(self, request_id: str) -> ExtensionRequest | None:
        return self._requests.get(request_id)

    def requests_for_profile(
        self, profile_id: str, *, status: str | None = None
    ) -> list[ExtensionRequest]:
        out = [r for r in self._requests.values() if r.profile_id == profile_id]
        if status:
            out = [r for r in out if r.status == status]
        out.sort(key=lambda r: r.requested_at, reverse=True)
        return out

    def update_request(
        self,
        request_id: str,
        *,
        status: str,
        granted_minutes: int | None = None,
        decided_by: str | None = None,
        now: datetime | None = None,
    ) -> ExtensionRequest | None:
        req = self._requests.get(request_id)
        if req is None:
            return None
        req.status = status
        req.granted_minutes = granted_minutes
        req.decided_by = decided_by
        req.decided_at = now or dt_util.utcnow()
        return req

    def expire_old_requests(self, *, now: datetime | None = None) -> list[ExtensionRequest]:
        """Mark pending requests as 'expired' once past their auto_expires_at.
        Returns the list of requests that were just expired (callers can
        notify / log)."""
        now = now or dt_util.utcnow()
        expired: list[ExtensionRequest] = []
        for req in self._requests.values():
            if req.status == "pending" and now >= req.auto_expires_at:
                req.status = "expired"
                req.decided_at = now
                expired.append(req)
        return expired

    def prune_old_requests(
        self,
        *,
        retain_days: int = REQUEST_RETENTION_DAYS,
        now: datetime | None = None,
    ) -> int:
        """Delete *decided* requests older than `retain_days` from disk.

        Pending requests are never pruned (they should be auto-expired
        by `expire_old_requests` first, then pruned on the next pass).
        Returns the number of requests removed.

        Added in v0.10.0 (QA: storage was growing unbounded).
        """
        now = now or dt_util.utcnow()
        cutoff = now - timedelta(days=retain_days)
        to_remove: list[str] = []
        for rid, req in self._requests.items():
            if req.status == "pending":
                continue
            ref_time = req.decided_at or req.requested_at
            if ref_time < cutoff:
                to_remove.append(rid)
        for rid in to_remove:
            self._requests.pop(rid, None)
        return len(to_remove)

    # ----- profiles -----

    def upsert_profile(self, profile: Profile) -> None:
        self._profiles[profile.id] = profile

    def get_profile(self, profile_id: str) -> Profile | None:
        """Public accessor — returns the persisted Profile or None."""
        return self._profiles.get(profile_id)

    # ----- system-action audit log (v0.12.0) -----

    def record_action(
        self,
        *,
        profile_id: str,
        action: str,
        at: datetime | None = None,
        reason: str | None = None,
        detail: str | None = None,
        bundle_id: str | None = None,
        actor: str | None = None,
    ) -> ActionLogEntry:
        """Append an entry to the audit log, with bypass coalescing.

        If `action == "enforce_start"` and there is already an unbroken run
        of recent enforce_start entries (>= BYPASS_COALESCE_THRESHOLD inside
        BYPASS_COALESCE_WINDOW_S), the existing entry is upgraded in place
        to action="bypass_attempt" and `count` is incremented. This keeps the
        log readable when a kid repeatedly reopens the Apple TV after it
        gets slept by the enforcer.

        Returns the entry that was created or updated.
        """
        at = at or dt_util.utcnow()

        # --- bypass-attempt coalescing (only for enforce_start) ---
        # If there are already ≥ (BYPASS_COALESCE_THRESHOLD - 1)
        # enforce_start/bypass_attempt entries for this profile in the
        # window, replace them all with a single bypass_attempt carrying
        # the total count. Keeps the audit log readable when the kid
        # repeatedly reopens the Apple TV (data-driven: 05-22 had ~30
        # fight fragments in a 30-min standoff that we'd otherwise log
        # as 30 separate enforce_start entries).
        if action == "enforce_start":
            window_start = at - timedelta(seconds=BYPASS_COALESCE_WINDOW_S)
            in_window_idx: list[int] = []
            total_count = 0
            for i, e in enumerate(self._actions):
                if (
                    e.profile_id == profile_id
                    and e.at >= window_start
                    and e.action in ("enforce_start", "bypass_attempt")
                ):
                    in_window_idx.append(i)
                    total_count += int(getattr(e, "count", 1) or 1)
            if total_count + 1 >= BYPASS_COALESCE_THRESHOLD:
                # Drop the prior in-window entries, then append the
                # coalesced one. Pop in reverse so indices stay valid.
                for i in reversed(in_window_idx):
                    self._actions.pop(i)
                merged = ActionLogEntry(
                    id=f"act_{int(at.timestamp() * 1000)}_{_token_hex(4)}",
                    profile_id=profile_id,
                    at=at,
                    action="bypass_attempt",
                    reason=reason,
                    detail=detail,
                    count=total_count + 1,
                    bundle_id=bundle_id,
                    actor=actor,
                )
                self._actions.append(merged)
                return merged

        entry = ActionLogEntry(
            id=f"act_{int(at.timestamp() * 1000)}_{_token_hex(4)}",
            profile_id=profile_id,
            at=at,
            action=action,
            reason=reason,
            detail=detail,
            bundle_id=bundle_id,
            actor=actor,
        )
        self._actions.append(entry)
        return entry

    def actions_for_profile(
        self,
        profile_id: str,
        *,
        limit: int = 50,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[ActionLogEntry]:
        """Newest-first list. `limit` caps the returned size."""
        out = [
            e
            for e in self._actions
            if e.profile_id == profile_id
            and (since is None or e.at >= since)
            and (until is None or e.at <= until)
        ]
        out.sort(key=lambda e: e.at, reverse=True)
        return out[: max(0, limit)]

    def _prune_actions(self, *, now: datetime | None = None) -> int:
        """Drop entries older than ACTION_LOG_RETENTION_DAYS."""
        now = now or dt_util.utcnow()
        cutoff = now - timedelta(days=ACTION_LOG_RETENTION_DAYS)
        before = len(self._actions)
        self._actions = [e for e in self._actions if e.at >= cutoff]
        return before - len(self._actions)

    def prune_profiles(self, keep_ids: set[str]) -> int:
        """Drop profiles whose ID is not in `keep_ids` and their events.

        Returns the number of profiles removed. Called from __init__ on every
        config entry setup so stale profiles (e.g. from a config entry that
        was injected manually with a non-ULID id and later replaced) don't
        accumulate over time.
        """
        stale = [pid for pid in self._profiles if pid not in keep_ids]
        for pid in stale:
            self._profiles.pop(pid, None)
            self._events = [e for e in self._events if e.profile_id != pid]
            self._extensions_granted_today.pop(pid, None)
            self._group_extensions_today.pop(pid, None)
        return len(stale)

    def all_profiles(self) -> list[Profile]:
        return list(self._profiles.values())

    # ----- events -----

    def open_event(self, profile_id: str, bundle_id: str, *, at: datetime) -> UsageEvent:
        """Open a new event. Caller must have closed any prior open event first."""
        event = UsageEvent(
            id=f"{int(at.timestamp() * 1000)}-{profile_id}",
            profile_id=profile_id,
            bundle_id=bundle_id,
            started_at=at,
            ended_at=None,
        )
        self._events.append(event)
        return event

    def open_event_for(self, profile_id: str) -> UsageEvent | None:
        for event in reversed(self._events):
            if event.profile_id == profile_id and event.ended_at is None:
                return event
        return None

    def close_open_event(self, profile_id: str, *, at: datetime) -> UsageEvent | None:
        event = self.open_event_for(profile_id)
        if event is None:
            return None
        event.ended_at = at
        return event

    def reopen_recent_event_if_match(
        self,
        profile_id: str,
        *,
        bundle_id: str,
        now: datetime,
        max_gap_seconds: int,
    ) -> UsageEvent | None:
        """Stitch a transient disconnect: reopen the most recent closed event
        if it has the same bundle_id and ended within `max_gap_seconds` of
        `now`. Returns the reopened event, or None if no merge happened."""
        most_recent: UsageEvent | None = None
        for event in reversed(self._events):
            if event.profile_id != profile_id:
                continue
            # If there's already an open event, never stitch — caller's bug.
            if event.ended_at is None:
                return None
            most_recent = event
            break
        if most_recent is None or most_recent.bundle_id != bundle_id:
            return None
        gap = (now - most_recent.ended_at).total_seconds()
        if gap > max_gap_seconds:
            return None
        most_recent.ended_at = None
        return most_recent

    def events_for_profile(self, profile_id: str) -> list[UsageEvent]:
        return [e for e in self._events if e.profile_id == profile_id]

    # ----- aggregates -----

    def used_seconds_today(
        self, profile_id: str, *, now: datetime | None = None, rollover_hour: int = 0
    ) -> int:
        """Total seconds the profile has used in the current LOGICAL day.

        Events spanning the rollover are correctly clipped to the window.

        `rollover_hour` shifts the day boundary off midnight (see
        schedule.day_start). With 5, viewing at 01:00 is charged to the evening
        it belongs to rather than to the day that is only just starting.
        """
        now = now or dt_util.utcnow()
        local_now = dt_util.as_local(now)
        start_of_day_local = day_start(local_now, rollover_hour)
        start_of_day_utc = dt_util.as_utc(start_of_day_local)
        return self._sum_seconds_in_window(profile_id, start_of_day_utc, now)

    def _sum_seconds_in_window(
        self, profile_id: str, window_start: datetime, window_end: datetime
    ) -> int:
        total = 0
        for event in self._events:
            if event.profile_id != profile_id:
                continue
            evt_end = event.ended_at or window_end
            start = max(event.started_at, window_start)
            end = min(evt_end, window_end)
            if end > start:
                total += int((end - start).total_seconds())
        return total

    # ----- today's history (for UI surfaces) -----

    def events_today(
        self, profile_id: str, *, now: datetime | None = None, rollover_hour: int = 0
    ) -> list[UsageEvent]:
        """Events that overlap today's local window, oldest first.

        Each returned event has its start/end clipped to today, so callers
        can sum durations or render a timeline without re-clipping.
        """
        now = now or dt_util.utcnow()
        local_now = dt_util.as_local(now)
        start_of_day_utc = dt_util.as_utc(day_start(local_now, rollover_hour))
        out: list[UsageEvent] = []
        for event in self._events:
            if event.profile_id != profile_id:
                continue
            evt_end = event.ended_at or now
            if evt_end <= start_of_day_utc:
                continue  # entirely in the past
            clipped_start = max(event.started_at, start_of_day_utc)
            clipped_end = min(evt_end, now)
            if clipped_end <= clipped_start:
                continue
            out.append(
                UsageEvent(
                    id=event.id,
                    profile_id=event.profile_id,
                    bundle_id=event.bundle_id,
                    started_at=clipped_start,
                    ended_at=None if event.ended_at is None else clipped_end,
                )
            )
        out.sort(key=lambda e: e.started_at)
        return out

    def events_in_range(
        self,
        profile_id: str,
        *,
        local_from: datetime,
        local_to: datetime,
    ) -> list[UsageEvent]:
        """Events overlapping `[local_from, local_to)`, clipped to that window.

        Caller passes timezone-aware local datetimes — usually
        `start_of_day_local(date_from)` and `start_of_day_local(date_to + 1d)`.
        Returned events have `started_at` / `ended_at` clipped to the window,
        so callers can sum without re-clipping.

        Open events use `now` as their notional end.
        """
        window_start = dt_util.as_utc(local_from)
        window_end = dt_util.as_utc(local_to)
        now = dt_util.utcnow()
        out: list[UsageEvent] = []
        for event in self._events:
            if event.profile_id != profile_id:
                continue
            evt_end = event.ended_at or now
            if evt_end <= window_start or event.started_at >= window_end:
                continue
            clipped_start = max(event.started_at, window_start)
            clipped_end = min(evt_end, window_end)
            if clipped_end <= clipped_start:
                continue
            out.append(
                UsageEvent(
                    id=event.id,
                    profile_id=event.profile_id,
                    bundle_id=event.bundle_id,
                    started_at=clipped_start,
                    ended_at=None if event.ended_at is None else clipped_end,
                )
            )
        out.sort(key=lambda e: e.started_at)
        return out

    def app_totals_today(
        self, profile_id: str, *, now: datetime | None = None, rollover_hour: int = 0
    ) -> dict[str, dict[str, int | float]]:
        """Aggregate today's usage per bundle_id. Returns {bundle_id: {seconds, sessions}}."""
        now = now or dt_util.utcnow()
        totals: dict[str, dict[str, int | float]] = {}
        for event in self.events_today(profile_id, now=now, rollover_hour=rollover_hour):
            slot = totals.setdefault(event.bundle_id, {"seconds": 0, "sessions": 0})
            slot["seconds"] = int(slot["seconds"]) + event.duration_seconds(now=now)
            slot["sessions"] = int(slot["sessions"]) + 1
        return totals

    def group_totals_today(
        self,
        profile_id: str,
        *,
        bundle_to_group: Callable[[str], str | None],
        now: datetime | None = None,
        rollover_hour: int = 0,
    ) -> dict[str, int]:
        """Aggregate today's seconds-used per group.

        `bundle_to_group` is supplied by the caller (closes over CURATED +
        the iTunes cache) so this module stays HA-agnostic. Bundles that
        return `None` from the mapper are skipped — caller can also map
        them to a default like 'other'.
        """
        totals: dict[str, int] = {}
        for event in self.events_today(profile_id, now=now, rollover_hour=rollover_hour):
            group = bundle_to_group(event.bundle_id)
            if not group:
                continue
            totals[group] = totals.get(group, 0) + event.duration_seconds(now=now)
        return totals

    # ----- extensions (per-day pool + v0.19.1 per-group tagging) -----

    def add_extension_minutes(
        self, profile_id: str, minutes: int, *, group: str | None = None
    ) -> int:
        """Grant (or revoke, with negative minutes) extension time.

        The daily pool ALWAYS receives the grant. When `group` is given, the
        same minutes are ALSO credited to that group's per-group pool so the
        enforcer can lift the binding group budget (v0.19.1). Both pools floor
        at 0 independently. Returns the new daily total (unchanged contract).
        """
        current = self._extensions_granted_today.get(profile_id, 0)
        new_total = max(0, current + minutes)
        self._extensions_granted_today[profile_id] = new_total
        if group:
            per_group = self._group_extensions_today.setdefault(profile_id, {})
            per_group[group] = max(0, per_group.get(group, 0) + minutes)
        return new_total

    def extension_minutes_today(self, profile_id: str) -> int:
        return self._extensions_granted_today.get(profile_id, 0)

    def group_extension_minutes_today(self, profile_id: str, group: str) -> int:
        """v0.19.1 — minutes of extension tagged to `group` today (0 if none)."""
        return self._group_extensions_today.get(profile_id, {}).get(group, 0)

    def group_extensions_today(self, profile_id: str) -> dict[str, int]:
        """v0.19.1 — full {group: minutes} extension map for a profile today."""
        return dict(self._group_extensions_today.get(profile_id, {}))

    def reset_daily_state(self, profile_id: str | None = None) -> None:
        """Reset a profile's daily extension pools (called at local midnight
        per-coordinator, and by the reset_usage service).

        v0.20.1 — PROFILE-SCOPED. The store is now shared across all config
        entries, so a global `.clear()` here would wipe OTHER profiles' granted
        extensions when one profile resets (e.g. reset_usage for kid A zeroing
        kid B's minutes). Pass a profile_id to clear only that profile. The
        no-arg form keeps the legacy global clear for any future full-reset
        caller (none in-tree)."""
        if profile_id is None:
            self._extensions_granted_today.clear()
            self._group_extensions_today.clear()
            return
        self._extensions_granted_today.pop(profile_id, None)
        self._group_extensions_today.pop(profile_id, None)

    # ----- maintenance -----

    def _prune_events(self) -> None:
        cutoff = dt_util.utcnow() - timedelta(days=EVENT_RETENTION_DAYS)
        # Never prune an open event.
        self._events = [
            e for e in self._events if e.ended_at is None or e.ended_at >= cutoff
        ]
