"""Coordinator: watch the Apple TV media_player, attribute usage time.

Two inputs drive this module:

  1. State changes on the `media_player.apple_tv_*` entity from HA's built-in
     Apple TV integration. The entity exposes `app_id` (the bundle id of the
     **currently playing** app). pyatv has no API for the foreground app when
     nothing is playing, so we keep a short "last-known" memory to bridge
     pauses.
  2. A periodic tick (30 s) that triggers the enforcement controller.

This module never talks to AdGuard or pyatv directly; it just keeps the
usage log accurate and asks the EnforcementController to re-evaluate.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .categorize import (
    GROUP_OTHER,
    NATIVE_TV_BUNDLE_ID,
    XBOX_CONSOLE_BUNDLE_ID,
    categorize,
    group_from_curated,
    lookup_itunes,
)
from .const import (
    COORDINATOR_TICK_SECONDS,
    DEVICE_KIND_XBOX_PRESENCE,
    DOMAIN,
    ENFORCER_REASSERT_SECONDS,
    EVENT_APP_ENDED,
    EVENT_APP_STARTED,
    EVENT_STITCH_SECONDS,
    EVENT_USAGE_UPDATED,
    app_display_name,
)
from .enforcer import EnforcementController
from .dns_classifier import (
    DEFAULT_BUNDLE_RECENCY_S,
    DEFAULT_GROUP_RECENCY_S,
    classify_dns_window,
)
from .media_attribution import (
    DNS_RECENT_WINDOW_S,
    PYATV_RELOAD_MIN_STUCK_S,
    PYATV_RELOAD_RATE_LIMIT_S,
    STALE_RUNAWAY_CEILING_S,
    AttributionAction,
    PyatvReloadInputs,
    StaleAction,
    decide_attribution,
    decide_pyatv_reload,
    decide_stale_action,
    excluded_sources_match_source_list,
    native_tv_is_active,
    resolve_effective_bundle_id,
)
from .storage import GroupSegment
from .schedule import (
    effective_daily_budget,
    effective_group_budgets,
    effective_quiet_windows_string,
    weekday_key,
)
from .storage import AppleTVMgmtStore, Profile, UsageEvent

_LOGGER = logging.getLogger(__name__)

# Synthetic bundle id used when the device is on but pyatv reports no app.
# Re-exported here for backward compat — the canonical source is now
# `media_attribution.UNKNOWN_APP_BUNDLE_ID`.
from .media_attribution import UNKNOWN_APP_BUNDLE_ID  # noqa: E402


class AppleTVMgmtCoordinator(DataUpdateCoordinator[dict]):
    def __init__(
        self,
        hass: HomeAssistant,
        store: AppleTVMgmtStore,
        enforcer: EnforcementController,
        profile: Profile,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{profile.id}",
            update_interval=timedelta(seconds=COORDINATOR_TICK_SECONDS),
        )
        self._store = store
        self._enforcer = enforcer
        self._profile = profile
        self._last_known_bundle_id: str | None = None
        self._last_known_seen_at: datetime | None = None
        self._unsub_state: callable | None = None
        self._unsub_midnight: callable | None = None
        self._unsub_reassert: callable | None = None
        # Bundle ids we've already attempted to look up via iTunes. Prevents
        # repeated lookups for an app that legitimately doesn't resolve.
        self._itunes_lookups_attempted: set[str] = set()
        # DST-safe midnight-reset dedup (added v0.10.0). Initialized to
        # today so the first tick of a fresh boot doesn't re-reset.
        self._last_midnight_reset_date: date | None = (
            dt_util.as_local(dt_util.utcnow()).date()
        )
        # v0.18.0 — DNS-corroborated attribution state.
        # Counts coordinator ticks where AdGuard reported AMBIENT_ONLY (device
        # online, no foreground app traffic). After AMBIENT_CLOSE_STREAK ticks
        # the visible-gap close path fires. Reset to 0 on any non-ambient
        # classification or on event open/close.
        self._consecutive_ambient_ticks: int = 0
        # TTL cache for the AdGuard query. The cache key includes the open
        # event id so close-then-open invalidates the cache automatically.
        # `_dns_cache` is (open_event_id, classification, cached_at).
        self._dns_cache: tuple[str | None, Any, datetime] | None = None
        # Single-flight lock so a state-change callback and the periodic
        # tick can't both fire an AdGuard query concurrently for the same
        # data. Created lazily because asyncio.Lock binds to the loop.
        self._dns_lock: Any = None
        # Dedup audit rows — write at most one app_group_corrected per
        # (open_event_id, new_group) transition. Reset on event close.
        self._last_corrected_group: dict[str, str] = {}
        # v0.18.0 — last time `_check_pyatv_reload` actually fired the reload.
        # Used to rate-limit reloads at PYATV_RELOAD_RATE_LIMIT_S (10 min) so
        # we don't reload-storm on a stuck-Samsung firmware bug. NOT
        # persisted across HA restart by design — HA restart already resets
        # pyatv state, so re-enabling reload at +30s post-boot is legitimate.
        self._last_pyatv_reload_at: datetime | None = None
        # v0.18.0 diagnostic sensors — surface the latest classifier verdict.
        # `_last_attribution_source` tracks WHICH path produced the current
        # attribution: pyatv (fresh / stale-preserved), DNS (bundle / group
        # annotation), AMBIENT close proposal, "no_open_event", or "disabled"
        # when dns_corroboration_mode is off. `_last_dns_confidence` mirrors
        # `DnsClassification.confidence.name` from the last classifier run
        # ("NONE" when feature off / no signal). Both are read by the
        # diagnostic sensors via the coordinator snapshot.
        self._last_attribution_source: str | None = None
        self._last_dns_confidence: str = "NONE"
        # v0.18.1 — TV-on tracking (observability only, no enforcement).
        # Accumulates seconds the configured `tv_entity_id` reports an "on-ish"
        # state across the local day. Per-tick (~30s) accumulator; midnight
        # reset wipes the counter. NOT persisted across HA restart — restart
        # mid-day discards in-day total (acceptable for a diagnostic). When
        # `tv_entity_id` is empty the helper short-circuits and the sensor
        # reports 0 (still registered so dashboards don't churn).
        self._tv_on_seconds_today: float = 0.0
        self._tv_on_last_seen_at: datetime | None = None
        # v0.21.1 (FIX 2b) — one-time guard for the native-TV excluded-source
        # misconfig warning. Set True the first time we evaluate a non-empty
        # `source_list` from the TV entity (whether or not we warned), so the
        # check runs at most once per coordinator lifetime.
        self._native_source_list_checked: bool = False

    async def async_start(self) -> None:
        """Wire up state-change listener and midnight reset, then do first tick."""
        # v0.20.0 — watch the PRIMARY entity AND every secondary device's entity
        # so an Xbox home/not_home transition re-resolves the room immediately
        # (not just on the 30s tick). _handle_state_change re-resolves the whole
        # room rather than trusting the single changed entity, so a primary
        # Apple-TV change can never clobber an open secondary (Xbox) event.
        # v0.21.0 — also watch the TV entity when native-TV tracking is on so a
        # source/state flip (e.g. HDMI1 → TV tuner) re-resolves the room live.
        watched = self._watched_entity_ids()
        self._unsub_state = async_track_state_change_event(
            self.hass,
            watched,
            self._handle_state_change,
        )
        self._unsub_midnight = async_track_time_interval(
            self.hass, self._handle_midnight_check, timedelta(minutes=1)
        )
        # Periodic AdGuard re-assert — heals drift between desired state
        # and what AdGuard actually has (HA restart, AdGuard restart, etc.).
        self._unsub_reassert = async_track_time_interval(
            self.hass,
            self._handle_reassert_tick,
            timedelta(seconds=ENFORCER_REASSERT_SECONDS),
        )
        # v0.17.0 F-E — restore persisted enforce-cycle state FIRST
        # (state, grace_started_at, reactivation_count, etc.). This is
        # the authoritative recovery path: HA restart mid-cycle resumes
        # exactly where it left off rather than walking a fresh
        # OK→WARN→GRACE cycle (the pre-fix bug handed the kid ~75 s
        # of free time per restart). Works regardless of
        # enable_adguard_block setting (v0.15.5 made AdGuard opt-in,
        # which had silently broken the AdGuard-based recovery for
        # the owner's setup).
        self._enforcer.seed_from_runtime_state()
        # Seed enforcer from live AdGuard state BEFORE the first refresh
        # so the initial evaluate() can correctly transition out of
        # ENFORCING when the kid is now under budget post-midnight.
        # v0.17.0 — secondary signal now; runtime state already restored.
        await self._enforcer.seed_from_adguard()
        # Seed from current state so we don't miss an in-progress session.
        await self._seed_from_current_state()
        await self.async_config_entry_first_refresh()

    def resubscribe_watched_entities(self) -> None:
        """v0.20.0 — re-wire the state-change listener to the CURRENT primary +
        secondary entities. Called after a live secondary_devices change (REST
        PATCH attach) so a freshly-folded-in Xbox fires _handle_state_change
        immediately, not just on the next 30s tick or HA restart. Idempotent."""
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        watched = self._watched_entity_ids()
        self._unsub_state = async_track_state_change_event(
            self.hass,
            watched,
            self._handle_state_change,
        )

    async def async_stop(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_midnight:
            self._unsub_midnight()
            self._unsub_midnight = None
        if self._unsub_reassert:
            self._unsub_reassert()
            self._unsub_reassert = None
        # Close any open event so we don't lose accuracy after a restart.
        self._store.close_open_event(self._profile.id, at=dt_util.utcnow())
        await self._store.async_save()

    # ----- HA callbacks -----

    # v0.19.0 — translate the raw HA entity state into the (media_state, app_id)
    # pair `_sync_open_event` expects. For "apple_tv" profiles it's a pass-
    # through (state.state + state.attributes["app_id"]). For "xbox_presence"
    # profiles the entity is a device_tracker.* whose state is "home"/"not_home";
    # we synthesize media_state="playing" + app_id=XBOX_CONSOLE_BUNDLE_ID
    # while at home and the off shape otherwise, so the rest of the pipeline
    # treats the Xbox like a normal foreground app.
    def _extract_activity_signal(
        self, state: object
    ) -> tuple[str | None, str | None]:
        if state is None:
            return (None, None)
        if getattr(self._profile, "device_kind", "apple_tv") == "xbox_presence":
            if state.state == "home":
                return ("playing", XBOX_CONSOLE_BUNDLE_ID)
            return (state.state, None)
        return (state.state, state.attributes.get("app_id"))

    # v0.20.0 — ONE system, not two. Helpers + resolver that fold the primary
    # device and any secondary devices into a SINGLE (effective bundle) signal
    # for the one shared event stream.

    def _secondary_entity_ids(self) -> list[str]:
        """Entity ids of all configured secondary devices (empty list ⇒ none)."""
        return [
            d["entity_id"]
            for d in (getattr(self._profile, "secondary_devices", None) or [])
            if d.get("entity_id")
        ]

    def _native_tv_entity_id(self) -> str | None:
        """v0.21.0 — the TV media_player entity to consult for native-TV
        tracking, or None when the feature is off / no TV configured. Uses
        `tv_entity_id` — the SAME field the tv-on-minutes accumulator + the
        stale-session liveness gate already read (NOT `tv_shutdown_target`,
        which is None unless the Apple-TV tv-shutdown switch is on; native TV
        must work independently of that switch)."""
        if not getattr(self._profile, "track_native_tv", False):
            return None
        return (getattr(self._profile, "tv_entity_id", None) or None)

    def _maybe_warn_native_source_list_mismatch(self) -> None:
        """v0.21.1 (FIX 2b) — one-time misconfig check for native-TV tracking.

        When native TV is on and a TV entity is configured, verify that at least
        one of the configured `native_tv_excluded_sources` actually appears in
        the TV entity's `source_list` attribute (case/whitespace-insensitive).
        If NONE match, the excluded list is almost certainly wrong (guessed
        default vs. what this TV really reports), which means a tracked device's
        input (e.g. the Apple TV's HDMI) can be silently mis-booked as native
        TV. Log ONE warning naming both lists so the mismatch is visible instead
        of silently mis-counting. Skipped when `source_list` is missing/empty
        (retried on a later tick, once the entity reports it)."""
        if self._native_source_list_checked:
            return
        if not getattr(self._profile, "track_native_tv", False):
            return
        tv_eid = getattr(self._profile, "tv_entity_id", None) or None
        if not tv_eid:
            return
        st = self.hass.states.get(tv_eid)
        if st is None:
            return
        source_list = st.attributes.get("source_list")
        if not source_list:
            return  # entity hasn't reported its inputs yet — retry next tick
        # We now have a non-empty source_list: this is our single evaluation.
        self._native_source_list_checked = True
        excluded = list(
            getattr(self._profile, "native_tv_excluded_sources", None) or []
        )
        if not excluded:
            return  # nothing configured to exclude — nothing to validate
        if not excluded_sources_match_source_list(excluded, source_list):
            _LOGGER.warning(
                "v0.21.1 native-TV misconfig: none of the configured "
                "native_tv_excluded_sources %s match TV entity %s reported "
                "source_list %s (case/whitespace-insensitive). Inputs owned by "
                "a tracked device (e.g. the Apple TV's HDMI) may be mis-booked "
                "as native TV. Update native_tv_excluded_sources via the panel "
                "/ PATCH /limits to match the names the TV actually reports.",
                excluded,
                tv_eid,
                list(source_list),
            )

    def _watched_entity_ids(self) -> list[str]:
        """v0.21.0 — the full set of entities whose state changes must trigger a
        room re-resolution: the primary Apple TV, every secondary device, and
        (when native-TV tracking is on) the TV entity. Deduped, order-stable."""
        watched = [self._profile.apple_tv_entity_id, *self._secondary_entity_ids()]
        native_tv = self._native_tv_entity_id()
        if native_tv and native_tv not in watched:
            watched.append(native_tv)
        return watched

    def _resolve_room_activity(
        self, now: datetime
    ) -> tuple[str | None, bool]:
        """Fold primary + secondary devices into ONE effective bundle for the
        single shared UsageEvent. Returns (effective_bundle_id, from_secondary).

        Priority — room-as-unit, never double-counted:
          1. The PRIMARY (Apple TV) wins whenever it is effectively active. We
             call `_effective_bundle_id` exactly ONCE (it applies idle-grace and
             is the authoritative idle-grace carry-over for the Apple TV). If it
             returns a bundle, the Apple TV owns the room — even mid-pause within
             idle-grace. This keeps per-app attribution and children's-content
             priority, and means the Apple-TV idle-grace memory is NEVER polluted
             with a secondary's synthetic bundle (we don't feed xbox.console in).
          2. Otherwise the FIRST active secondary device claims the room with its
             synthetic bundle (xbox_presence home ⇒ "xbox.console" → gaming).
          3. Otherwise the room is idle ⇒ (None, False) closes the open event.

        Because (1) and (2) are mutually exclusive, the single open event holds
        EITHER an Apple TV app OR a secondary bundle at any instant — overlap
        collapses to one, so used_seconds_today counts the room exactly once even
        when both devices are physically on.

        `from_secondary` lets `_sync_open_event` skip the pyatv-staleness gate,
        which is meaningless for a device_tracker and would otherwise refuse to
        open the Xbox event whenever the Apple TV is off (>= stale_session_minutes
        with no push) — the exact scenario this merge exists to track.
        """
        primary = self.hass.states.get(self._profile.apple_tv_entity_id)
        p_media, p_app = self._extract_activity_signal(primary)
        effective = self._effective_bundle_id(p_media, p_app, now)
        if effective is not None:
            return (effective, False)  # Apple TV (incl. idle-grace) wins
        for dev in (getattr(self._profile, "secondary_devices", None) or []):
            eid = dev.get("entity_id")
            if not eid:
                continue
            st = self.hass.states.get(eid)
            if st is None:
                continue
            if (
                dev.get("device_kind") == DEVICE_KIND_XBOX_PRESENCE
                and st.state == "home"
            ):
                return (dev.get("bundle_id") or XBOX_CONSOLE_BUNDLE_ID, True)
        # v0.21.0 — native TV is the LOWEST-priority claimant (apple_tv >
        # secondaries > native). Reached only when nothing tracked is active.
        # The excluded-source list (HDMI1=Apple TV, HDMI2/DVI=Xbox) is a second
        # guard on top of that precedence. `from_secondary=True` because the TV
        # entity — not the pyatv mirror — is the authority here, so the pyatv
        # staleness gate in `_sync_open_event` must be bypassed (a TV that has
        # been "on" for hours is NOT a frozen-push symptom).
        native_tv_eid = self._native_tv_entity_id()
        if native_tv_eid:
            tv_st = self.hass.states.get(native_tv_eid)
            tv_state = tv_st.state if tv_st is not None else None
            source = tv_st.attributes.get("source") if tv_st is not None else None
            if native_tv_is_active(
                track_native_tv=True,
                tv_configured=True,
                tv_state=tv_state,
                source=source,
                excluded_sources=getattr(
                    self._profile, "native_tv_excluded_sources", None
                ),
            ):
                return (NATIVE_TV_BUNDLE_ID, True)
        return (None, False)  # room idle — close as usual

    async def _seed_from_current_state(self) -> None:
        now = dt_util.utcnow()
        # Back-compat (mirrors the tick): a single-device profile whose primary
        # entity is missing at seed time (e.g. pyatv-not-loaded-yet race on
        # restart) does NOTHING — no resolver call, no side effect, no
        # force-close (the v0.17.2 stale logic owns that). A merged profile
        # (with secondaries) always resolves so an in-progress Xbox session is
        # picked up on restart even when the Apple TV entity is absent.
        primary_state = self.hass.states.get(self._profile.apple_tv_entity_id)
        if (
            primary_state is None
            and not self._secondary_entity_ids()
            and not self._native_tv_entity_id()  # v0.21.0 — native TV seeds too
        ):
            return
        # v0.20.0 — resolve the whole room (primary + secondaries), not just the
        # primary, so an in-progress Xbox session is picked up on restart too.
        effective, from_secondary = self._resolve_room_activity(now)
        await self._sync_open_event(
            effective=effective,
            from_secondary=from_secondary,
            now=now,
        )

    @callback
    def _handle_state_change(self, event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if new is None:
            return
        # v0.20.0 — ANY watched entity (primary OR a secondary) changing triggers
        # a full room re-resolution. We deliberately ignore which entity fired:
        # trusting the changed entity alone let a primary Apple-TV state event
        # close an open Xbox event (the union must win, not the last messenger).
        now = dt_util.utcnow()
        effective, from_secondary = self._resolve_room_activity(now)
        self.hass.async_create_task(
            self._sync_open_event(
                effective=effective,
                from_secondary=from_secondary,
                now=now,
            )
        )

    @callback
    def _handle_midnight_check(self, now: datetime) -> None:
        local = dt_util.as_local(now)
        # Reset extension pool once per local midnight (00:00 — 00:01 window).
        # The DST-safe deduplication uses `_last_midnight_reset_date` so we
        # only fire once per local calendar day.
        if local.hour == 0 and local.minute == 0:
            today = local.date()
            if self._last_midnight_reset_date != today:
                self._last_midnight_reset_date = today
                # v0.20.1 — scope to THIS profile (shared store): each
                # coordinator resets its own profile at midnight, so a
                # sibling's just-granted extension is never re-cleared.
                self._store.reset_daily_state(self._profile.id)
                self.hass.async_create_task(self._store.async_save())
                # v0.18.1 — TV-on counter rolls over with the rest of the
                # daily state. _tv_on_last_seen_at stays so a TV that is
                # currently on at midnight keeps counting into the new day.
                self._tv_on_seconds_today = 0.0

        # Expire pending requests that are past auto_expires_at. Runs every
        # minute, not just at midnight (added v0.10.0 — was never called
        # before).
        expired = self._store.expire_old_requests(now=now)
        if expired:
            for req in expired:
                self.hass.bus.async_fire(
                    "appletv_mgmt_request_decided",
                    {
                        "profile_id": req.profile_id,
                        "request_id": req.id,
                        "status": "expired",
                        "granted_minutes": 0,
                        "decided_by": "auto",
                    },
                )
            self.hass.async_create_task(self._store.async_save())

        # Prune decided requests older than retention once per local day.
        if local.hour == 0 and local.minute == 0:
            removed = self._store.prune_old_requests(now=now)
            if removed:
                _LOGGER.debug("Pruned %d old decided requests", removed)
                self.hass.async_create_task(self._store.async_save())

    @callback
    def _handle_reassert_tick(self, now: datetime) -> None:
        """Periodic reassert — heals drift between desired state and AdGuard,
        and (v0.16.0) watches for the kid re-arming the Apple TV under
        ENFORCING (the "social pressure" intervention)."""
        self.hass.async_create_task(self._enforcer.reassert())
        # v0.16.0 — fire-and-forget; watch_reactivation is a no-op when
        # we're not in ENFORCING or when should_act is BYPASS/OBSERVE.
        self.hass.async_create_task(self._enforcer.watch_reactivation())

    # ----- usage attribution -----

    def _entity_is_stale(self, now: datetime) -> bool:
        """v0.17.2 — return True iff the apple_tv_entity_id's
        `last_updated` is ≥ `stale_session_minutes` ago. Single source
        of truth for staleness used by both `_check_for_stale_session`
        (closes stuck events) and `_sync_open_event` (refuses to open
        new sessions against stale state).
        """
        threshold_min = int(getattr(self._profile, "stale_session_minutes", 5) or 0)
        if threshold_min <= 0:
            return False  # opt-out
        state = self.hass.states.get(self._profile.apple_tv_entity_id)
        if state is None or state.last_updated is None:
            return False
        age_s = (now - state.last_updated).total_seconds()
        return age_s >= threshold_min * 60

    async def _sync_open_event(
        self, *, effective: str | None, from_secondary: bool = False, now: datetime
    ) -> None:
        """Ensure the storage log reflects what the room is doing right now.

        v0.20.0 — takes the already-resolved `effective` bundle from
        `_resolve_room_activity` (which calls `_effective_bundle_id` exactly
        once). `from_secondary` is True when the room is owned by a secondary
        device (e.g. the Xbox); it bypasses the pyatv-staleness gate below,
        which is meaningless for a device_tracker.

        v0.17.2 — refuses to OPEN a new session when the (Apple TV) source
        entity is stale. Without this, the v0.17.1 close/reopen loop bit: the
        staleness check would close the stuck event, then this method
        immediately saw the same stale "playing/Disney+" state and
        opened a fresh event, then next tick closed it again, ad
        infinitum. CLOSING decisions are unchanged — when the room
        legitimately goes off/idle we still close the session.
        """
        open_event = self._store.open_event_for(self._profile.id)

        if effective is None:
            if open_event is not None:
                closed = self._store.close_open_event(self._profile.id, at=now)
                if closed is not None:
                    self._fire_app_ended(closed)
                await self._store.async_save()
            return

        # v0.17.2 — gate. Only matters when we'd be ABOUT to open a new
        # event (no open event OR app_id changed). Continuation of an
        # already-open event for the same bundle is fine — we're just
        # observing accrued time. Stitch path also bypassed because it
        # is a continuation of a recent SAME-bundle event, not a fresh
        # start against potentially-bogus state.
        about_to_open = (
            open_event is None or open_event.bundle_id != effective
        )
        # v0.20.0 — the staleness gate guards against a frozen pyatv push on the
        # Apple TV. It is meaningless for a secondary device (a device_tracker
        # that has been "home" for >stale_session_minutes is NOT stuck), and
        # applying it would refuse to ever open the Xbox event whenever the
        # Apple TV is off. Skip it when the room is owned by a secondary.
        if about_to_open and not from_secondary and self._entity_is_stale(now):
            _LOGGER.debug(
                "v0.17.2 — refusing to open %s session against stale "
                "%s entity (stale_session_minutes=%s)",
                effective,
                self._profile.apple_tv_entity_id,
                getattr(self._profile, "stale_session_minutes", 5),
            )
            return

        if open_event is None:
            # Stitch: if the previous closed event for this profile was the
            # same app and ended <= EVENT_STITCH_SECONDS ago, treat this as
            # a continuation (transient pyatv disconnect) and reopen that
            # event instead of starting a fresh session.
            #
            # v0.21.1 (FIX 4 completion) — NATIVE_TV events must NOT stitch.
            # They are TV-state-driven (not pyatv-push-driven), so there is no
            # "transient disconnect" to bridge. Critically, when the runaway
            # ceiling force-closes a stuck-'on' native event, a same-tick stitch
            # would reopen it with the ORIGINAL started_at (gap ≈ 0), neutralizing
            # the ceiling and re-closing+re-stitching every 30s tick (audit + save
            # spam). Skipping the stitch opens a FRESH event instead, so each
            # native segment is bounded by the ceiling and the loop stops.
            stitched = None
            if effective != NATIVE_TV_BUNDLE_ID:
                stitched = self._store.reopen_recent_event_if_match(
                    self._profile.id,
                    bundle_id=effective,
                    now=now,
                    max_gap_seconds=EVENT_STITCH_SECONDS,
                )
            if stitched is not None:
                _LOGGER.debug(
                    "Stitched event %s (%s) after transient gap",
                    stitched.id,
                    stitched.bundle_id,
                )
                await self._store.async_save()
                return
            opened = self._store.open_event(self._profile.id, effective, at=now)
            self._fire_app_started(opened)
            await self._store.async_save()
            return

        if open_event.bundle_id != effective:
            closed = self._store.close_open_event(self._profile.id, at=now)
            if closed is not None:
                self._fire_app_ended(closed)
            opened = self._store.open_event(self._profile.id, effective, at=now)
            self._fire_app_started(opened)
            await self._store.async_save()

    # ----- logbook event emission -----

    def _fire_app_started(self, event: UsageEvent) -> None:
        """Fire HA bus events for the Logbook panel + downstream consumers."""
        name = app_display_name(event.bundle_id)
        payload = {
            "profile_id": self._profile.id,
            "bundle_id": event.bundle_id,
            "display_name": name,
            "started_at": event.started_at.isoformat(),
        }
        # Native event for external consumers.
        self.hass.bus.async_fire(EVENT_APP_STARTED, payload)
        # logbook entry — appears in HA's built-in Logbook panel.
        self.hass.bus.async_fire(
            "logbook_entry",
            {
                "name": self._profile.display_name,
                "message": f"started {name}",
                "domain": DOMAIN,
                "entity_id": self._profile.apple_tv_entity_id,
            },
        )

    def _fire_app_ended(self, event: UsageEvent) -> None:
        # v0.18.0 — reset per-event DNS-corroboration state. The DNS cache
        # is keyed by event id so it self-invalidates, but the streak
        # counter and dedup map need explicit reset on close.
        self._consecutive_ambient_ticks = 0
        self._last_corrected_group.pop(event.id, None)
        # Diagnostic sensors reset to "no_open_event" / NONE until the next
        # session opens and the classifier runs again. Keeps the sensor
        # story tied to the open event, not stale verdicts from minutes ago.
        self._last_attribution_source = "no_open_event"
        self._last_dns_confidence = "NONE"
        name = app_display_name(event.bundle_id)
        duration_s = event.duration_seconds()
        minutes = round(duration_s / 60, 1)
        payload = {
            "profile_id": self._profile.id,
            "bundle_id": event.bundle_id,
            "display_name": name,
            "started_at": event.started_at.isoformat(),
            "ended_at": (event.ended_at.isoformat() if event.ended_at else None),
            "duration_seconds": duration_s,
            "duration_minutes": minutes,
        }
        self.hass.bus.async_fire(EVENT_APP_ENDED, payload)
        self.hass.bus.async_fire(
            "logbook_entry",
            {
                "name": self._profile.display_name,
                "message": f"finished {name} after {minutes} min",
                "domain": DOMAIN,
                "entity_id": self._profile.apple_tv_entity_id,
            },
        )

    async def _check_dns_corroboration(self, now: datetime) -> None:
        """v0.18.0 — DNS-corroborated attribution.

        Called from `_async_update_data` after `_check_for_stale_session`
        and before `_sync_open_event`. Queries AdGuard's recent DNS log
        for the configured `apple_tv_ip`, classifies the domains, and
        calls `decide_attribution` to decide whether to:
        - PRESERVE the open event as-is (no-op)
        - ANNOTATE_GROUP by appending a GroupSegment to the open event
          (truth-preserving correction — the bundle_id stays whatever
          pyatv last said, but the GROUP for sub-segments comes from DNS)
        - CLOSE_AT_LAST_UPDATED via the existing stale-session path
          (sustained AMBIENT — kid walked away)

        Behavior is gated by `profile.dns_corroboration_mode`:
        - `off` (default): this method is a no-op.
        - `monitor`: classification runs and proposals are written to the
          audit log, but UsageEvent.group_segments is NOT mutated. Use
          for ~1-2 weeks to verify classifications match reality.
        - `correct`: classification drives real corrections.

        AdGuard errors / timeouts fail open — the method silently returns
        and the rest of the tick proceeds as if the feature weren't
        configured. v0.17.x behavior is preserved exactly when the
        classifier yields no signal.
        """
        # v0.19.0 — pyatv-specific path. Non-apple_tv profiles (e.g. Xbox
        # via FRITZ presence) have no app-id signal in pyatv-the-DNS-log
        # is for and the classifier would be a no-op anyway. Skip cleanly
        # so the diagnostic sensors stay at "disabled"/"NONE".
        if getattr(self._profile, "device_kind", "apple_tv") != "apple_tv":
            self._last_attribution_source = "disabled"
            self._last_dns_confidence = "NONE"
            return
        mode = (getattr(self._profile, "dns_corroboration_mode", "off")
                or "off").lower()
        atv_ip = (getattr(self._profile, "apple_tv_ip", "") or "").strip()
        if mode == "off" or not atv_ip:
            # Diagnostic sensors distinguish "feature off" from "feature on
            # but classifier hasn't produced a verdict yet" so operators
            # know whether silence means the gate is disabled or just
            # hasn't fired.
            self._last_attribution_source = "disabled"
            self._last_dns_confidence = "NONE"
            return

        adguard = getattr(self._enforcer, "_adguard", None)
        if adguard is None:
            return  # no client wired up

        open_event = self._store.open_event_for(self._profile.id)
        if open_event is None:
            # No open event = nothing to attribute. Reset the ambient
            # streak so the counter doesn't fire spuriously when an event
            # opens later.
            self._consecutive_ambient_ticks = 0
            self._last_attribution_source = "no_open_event"
            self._last_dns_confidence = "NONE"
            return

        # Single-flight: the periodic tick and the state-change callback
        # could both reach here in the same wall-clock instant. Use a
        # per-coordinator lock to serialize the work. Lock is bound to
        # the running loop, so lazy-create.
        if self._dns_lock is None:
            import asyncio
            self._dns_lock = asyncio.Lock()

        async with self._dns_lock:
            # TTL cache — 25s is just under the 30s tick interval, so
            # consecutive ticks reuse the result while a real correction
            # window still re-queries promptly. Cache key includes the
            # open event id so close-then-open transparently invalidates.
            cache = self._dns_cache
            cache_ttl_s = 25
            classification = None
            if cache is not None:
                cached_event_id, cached_classification, cached_at = cache
                if (
                    cached_event_id == open_event.id
                    and (now - cached_at).total_seconds() < cache_ttl_s
                ):
                    classification = cached_classification

            if classification is None:
                # Bounded query — 2s timeout. Fail-open via the client.
                try:
                    rows = await adguard.query_recent_dns(
                        atv_ip,
                        limit=200,
                        timeout_s=2.0,
                        since_seconds=300,
                    )
                except Exception:  # noqa: BLE001 — defensive belt
                    _LOGGER.debug(
                        "v0.18.0 dns_corroboration: AdGuard query raised; "
                        "treating as no signal"
                    )
                    rows = []

                # Convert ISO strings to datetimes for the classifier.
                domains_with_times: list[tuple[str, datetime]] = []
                for domain, t in rows:
                    try:
                        domains_with_times.append((domain, datetime.fromisoformat(t)))
                    except (TypeError, ValueError):
                        continue

                classification = classify_dns_window(
                    domains_with_times,
                    now=now,
                    bundle_recency_s=DEFAULT_BUNDLE_RECENCY_S,
                    group_recency_s=DEFAULT_GROUP_RECENCY_S,
                )
                self._dns_cache = (open_event.id, classification, now)

            # Compose with pyatv state for decide_attribution.
            state = self.hass.states.get(self._profile.apple_tv_entity_id)
            pyatv_age_s: float | None = None
            if state is not None and state.last_updated is not None:
                pyatv_age_s = max(
                    0.0, (now - state.last_updated).total_seconds()
                )

            # Resolve current group: latest open segment overrides curated.
            curated_group = self._group_for(open_event.bundle_id) or "other"
            current_group = curated_group
            if open_event.group_segments:
                # Most recent open segment (no ended_at) IS the current group.
                for seg in reversed(open_event.group_segments):
                    if seg.ended_at is None:
                        current_group = seg.group
                        break

            decision = decide_attribution(
                pyatv_age_s=pyatv_age_s,
                dns=classification,
                open_event_bundle_id=open_event.bundle_id,
                open_event_current_group=current_group,
                consecutive_ambient_ticks=self._consecutive_ambient_ticks,
            )

            # Track AMBIENT streak across ticks (used by decide_attribution).
            confidence_name = getattr(
                classification.confidence, "name", str(classification.confidence)
            )
            if confidence_name == "AMBIENT_ONLY":
                self._consecutive_ambient_ticks += 1
            else:
                self._consecutive_ambient_ticks = 0
            # Diagnostic-sensor stash: record what the classifier reported
            # this tick so the dns_classifier_confidence sensor reflects
            # the live signal (PRESERVE branches below otherwise skip it).
            self._last_dns_confidence = confidence_name

            await self._apply_attribution_decision(
                decision=decision,
                open_event=open_event,
                current_group=current_group,
                now=now,
                mode=mode,
            )

    async def _apply_attribution_decision(
        self,
        *,
        decision,
        open_event,
        current_group: str,
        now: datetime,
        mode: str,
    ) -> None:
        """Act on the AttributionDecision produced by `decide_attribution`.

        - PRESERVE: log + return.
        - ANNOTATE_GROUP:
            * monitor mode: write `app_group_corrected_proposed` audit row,
              do NOT mutate the event.
            * correct mode: close any open GroupSegment at `now`, append a
              new GroupSegment(started_at=now, group=new_group). Save store.
              Write `app_group_corrected` audit row (deduped per event +
              new_group).
        - CLOSE_AT_LAST_UPDATED:
            * monitor mode: audit row, no mutation.
            * correct mode: delegate to the existing stale-session close
              logic (re-uses the audit / fire_app_ended chain).
        """
        # Diagnostic stash: every classifier-driven decision updates the
        # attribution_source sensor. PRESERVE includes the reason so a
        # parent eyeballing the sensor sees "preserve:pyatv_fresh" vs
        # "preserve:bundle_match" — both are PRESERVE but tell a very
        # different story about WHY the classifier didn't act.
        action_label = decision.action.value
        reason_short = (decision.reason.split("_")[0:3] or [""])
        # Compose a short human-readable label without leaking long reason
        # strings into the sensor state. Example: "preserve:pyatv_fresh",
        # "annotate_group:dns_bundle", "close_at_last_updated:sustained".
        reason_hint = "_".join(p for p in reason_short if p)[:32]
        source_label = (
            f"{action_label}:{reason_hint}" if reason_hint else action_label
        )
        # Avoid recorder churn — only flip the stash when the value
        # actually changes.
        if source_label != self._last_attribution_source:
            self._last_attribution_source = source_label

        if decision.action is AttributionAction.PRESERVE:
            _LOGGER.debug(
                "v0.18.0 dns_corroboration PRESERVE: %s confidence=%s reason=%s",
                open_event.bundle_id,
                decision.confidence,
                decision.reason,
            )
            return

        if decision.action is AttributionAction.ANNOTATE_GROUP:
            new_group = decision.new_group
            if not new_group:
                return
            # Dedup: don't re-emit the same correction on every tick.
            if self._last_corrected_group.get(open_event.id) == new_group:
                return
            self._last_corrected_group[open_event.id] = new_group

            if mode == "correct":
                # Close any currently-open segment at `now`.
                for seg in open_event.group_segments:
                    if seg.ended_at is None:
                        seg.ended_at = now
                # Append a new open segment with the corrected group.
                open_event.group_segments.append(
                    GroupSegment(
                        started_at=now,
                        ended_at=None,
                        group=new_group,
                        source="dns_classifier",
                        confidence=decision.confidence,
                    )
                )
                await self._store.async_save()

            await self._audit_attribution_correction(
                open_event=open_event,
                old_group=current_group,
                new_group=new_group,
                decision=decision,
                mode=mode,
            )
            return

        if decision.action is AttributionAction.CLOSE_AT_LAST_UPDATED:
            if mode == "monitor":
                # v0.18.0 follow-up — dedup close proposals the same way
                # ANNOTATE_GROUP is deduped. Without this, every 30s tick
                # would re-emit the same `app_attribution_close_proposed`
                # row for the entire duration of an AMBIENT-only window
                # (the live install observed 30+ rows in 20 min during
                # a real Disney+ session before the dedup landed).
                close_key = "__close__"
                if self._last_corrected_group.get(open_event.id) == close_key:
                    return
                self._last_corrected_group[open_event.id] = close_key
                await self._audit_attribution_correction(
                    open_event=open_event,
                    old_group=current_group,
                    new_group=None,
                    decision=decision,
                    mode=mode,
                    action="app_attribution_close_proposed",
                )
                return
            # correct mode — go through the v0.17.1 close path so we share
            # the same audit chain and last_updated semantics.
            await self._check_for_stale_session(now=now)

    async def _audit_attribution_correction(
        self,
        *,
        open_event,
        old_group: str,
        new_group: str | None,
        decision,
        mode: str,
        action: str | None = None,
    ) -> None:
        """Write the appropriate audit row for a DNS-corroboration event.
        Errors are swallowed so a broken audit path never crashes the tick."""
        if action is None:
            action = (
                "app_group_corrected" if mode == "correct"
                else "app_group_corrected_proposed"
            )
        detail = (
            f"event={open_event.id} bundle={open_event.bundle_id} "
            f"from={old_group} -> {new_group} "
            f"confidence={decision.confidence} reason={decision.reason}"
        )
        try:
            from .audit import record_admin_action
            record_admin_action(
                self.hass,
                profile_id=self._profile.id,
                action=action,
                detail=detail,
                actor="dns_classifier",
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("dns_corroboration audit row failed (non-fatal)")
        _LOGGER.info(
            "v0.18.0 dns_corroboration %s mode=%s: %s",
            action, mode, detail,
        )

    async def _check_for_stale_session(self, now: datetime) -> None:
        """v0.17.3 — staleness detection with Samsung-liveness gate.

        Background (v0.17.1): pyatv's companion protocol drops silently.
        HA's apple_tv mirror stays at its last cached state ("playing
        Disney+") indefinitely; the open UsageEvent never closes and
        phantom usage accrues — the owner's kid's overnight Disney+
        session tracked 12.9 hours on 2026-05-31. v0.17.1 fixed it by
        closing the open event at `last_updated` whenever the entity
        was push-quiet for `>= stale_session_minutes`.

        Under-count regression (v0.17.3, observed live 2026-06-09): on
        tvOS 26 Apple TV 4K, pyatv pushes very rarely during steady
        playback (~once per 5+ min — 312s gaps observed). The v0.17.1
        check fires every push-quiet window and closes back at the
        open-time `last_updated`, recording 0.0 min for the chunk
        even though the kid was watching. Cross-checked vs. Samsung
        TV history: 2 of 3 zero-duration sessions today were genuine
        under-counts (Samsung stayed `on` throughout).

        Fix: consult an out-of-band liveness signal (the Samsung TV
        state, the same `tv_entity_id` used by the v0.4.0 TV-shutdown
        feature). The decision logic is in
        `media_attribution.decide_stale_action` — a pure function
        unit-tested without HA. This method is a thin adapter that
        reads the relevant entities, clamps clock skew, and acts on
        the result.

        Set `stale_session_minutes = 0` to disable the check entirely.
        """
        # v0.19.0 — pyatv-specific failure mode. Xbox device_tracker rolls
        # presence every ~10s via FRITZ!Box; it can't go "stuck playing".
        # Skip the check for non-apple_tv profiles.
        if getattr(self._profile, "device_kind", "apple_tv") != "apple_tv":
            return
        threshold_min = int(getattr(self._profile, "stale_session_minutes", 5) or 0)
        open_event = self._store.open_event_for(self._profile.id)
        # v0.20.0 — if the open event belongs to a SECONDARY device (e.g. the
        # Xbox), the Apple-TV staleness machinery must not touch it. A pyatv
        # freeze that leaves the Apple TV stuck at "playing" is irrelevant when
        # the room is actually the Xbox — closing the gaming event here would
        # silently drop live Xbox minutes. (Computed inline from self._profile
        # so this hot path has no dependency on sibling helpers.)
        secondary_bundles = {
            (d.get("bundle_id") or XBOX_CONSOLE_BUNDLE_ID)
            for d in (getattr(self._profile, "secondary_devices", None) or [])
            if d.get("entity_id")
        }
        if open_event is not None and open_event.bundle_id in secondary_bundles:
            return
        # v0.21.0 — a native TV event is owned by the TV entity, not the pyatv
        # mirror; the Apple-TV staleness gate (pyatv freeze) must not close it —
        # the TV state is authoritative and closes the event on its own when the
        # display goes off / flips to an excluded source.
        # v0.21.1 (FIX 4) — but it is NOT fully exempt from the runaway ceiling.
        # A Samsung stuck at "on" (some TVs miss a power-off) would otherwise
        # over-count linear_tv forever. Reuse the SAME STALE_RUNAWAY_CEILING_S
        # the Apple TV path uses: force-close the native event once it has been
        # open past the ceiling, then let the TV fast path own everything below
        # it. No new timer — this is the existing absolute-max guard applied to
        # the native bundle.
        if open_event is not None and open_event.bundle_id == NATIVE_TV_BUNDLE_ID:
            native_age_s = max(0.0, (now - open_event.started_at).total_seconds())
            if native_age_s >= STALE_RUNAWAY_CEILING_S:
                closed = self._store.close_open_event(self._profile.id, at=now)
                if closed is not None:
                    self._fire_app_ended(closed)
                    _LOGGER.warning(
                        "v0.21.1 native-TV runaway: open event %s (bundle=%s) "
                        "exceeded %ds runaway ceiling (open_for=%.0fs) — "
                        "force-closed at %s (TV likely stuck 'on', missed a "
                        "power-off).",
                        closed.id, closed.bundle_id, STALE_RUNAWAY_CEILING_S,
                        native_age_s, now.isoformat(),
                    )
                    try:
                        from .audit import record_admin_action
                        record_admin_action(
                            self.hass,
                            profile_id=self._profile.id,
                            action="app_stale_closed",
                            detail=(
                                f"{closed.bundle_id} native-TV runaway-ceiling "
                                f"fired (open_for={int(native_age_s)}s >= "
                                f"{STALE_RUNAWAY_CEILING_S}s); closed at {now.isoformat()}"
                            ),
                            actor="system",
                        )
                    except Exception:  # noqa: BLE001 — audit must never crash the tick
                        _LOGGER.debug("native-TV runaway audit row failed (non-fatal)")
                    await self._store.async_save()
            return
        state = self.hass.states.get(self._profile.apple_tv_entity_id)

        apple_tv_state = state.state if state is not None else None
        last_updated = state.last_updated if state is not None else None
        # Clamp clock skew: NTP can momentarily put last_updated ahead of
        # `now` right after HA restart. A negative age would otherwise
        # never pass the >= threshold gate, but treating it as 0 is the
        # honest reading (entity is fresh).
        age_s: float | None = None
        if last_updated is not None:
            age_s = max(0.0, (now - last_updated).total_seconds())

        open_event_age_s: float | None = None
        if open_event is not None:
            open_event_age_s = max(
                0.0, (now - open_event.started_at).total_seconds()
            )

        tv_entity_id = getattr(self._profile, "tv_entity_id", None) or None
        tv_entity_configured = bool(tv_entity_id)
        tv_state: str | None = None
        if tv_entity_configured:
            tv_st_obj = self.hass.states.get(tv_entity_id)
            tv_state = tv_st_obj.state if tv_st_obj is not None else None

        action = decide_stale_action(
            apple_tv_state=apple_tv_state,
            apple_tv_last_updated_age_s=age_s,
            stale_threshold_s=threshold_min * 60,
            has_open_event=open_event is not None,
            open_event_age_s=open_event_age_s,
            tv_entity_configured=tv_entity_configured,
            tv_state=tv_state,
        )

        if action is StaleAction.NOOP:
            return

        if action is StaleAction.KEEP_OPEN:
            # Stale-but-live: open_event MUST be set (gate rule 2 excluded
            # the None case). The event accrues to `now` naturally via
            # the storage layer's open-event handling.
            assert open_event is not None  # for type-checkers; gate-enforced
            _LOGGER.debug(
                "v0.17.3 stale-but-live: apple_tv=%s age=%.0fs tv=%s — "
                "keep accruing for open event %s (bundle=%s, open_for=%.0fs)",
                apple_tv_state,
                age_s,
                tv_state,
                open_event.id,
                open_event.bundle_id,
                open_event_age_s,
            )
            return

        # CLOSE_AT_LAST_UPDATED or CLOSE_RUNAWAY: both close at
        # last_updated. The runaway path differs only in the audit
        # reason — closing at `now` would charge time accrued after
        # pyatv went dark, which we deliberately avoid in both paths.
        # Gate rules 4/5 ensure last_updated is set at this point.
        assert last_updated is not None  # gate-enforced
        closed = self._store.close_open_event(self._profile.id, at=last_updated)
        if closed is None:
            return
        self._fire_app_ended(closed)
        if action is StaleAction.CLOSE_RUNAWAY:
            _LOGGER.warning(
                "v0.17.3 stale-runaway: open event %s (bundle=%s) exceeded "
                "%ds runaway ceiling (open_for=%.0fs, apple_tv=%s, tv=%s) — "
                "force-closed at last_updated %s",
                closed.id,
                closed.bundle_id,
                STALE_RUNAWAY_CEILING_S,
                open_event_age_s,
                apple_tv_state,
                tv_state,
                last_updated.isoformat(),
            )
        else:
            _LOGGER.warning(
                "v0.17.3 stale-close: %s entity %s last_updated=%s (%.0fs "
                "ago >= threshold %ds, tv_state=%s) — closed open event %s "
                "(bundle=%s) at the stale timestamp.",
                self._profile.id,
                self._profile.apple_tv_entity_id,
                last_updated.isoformat(),
                age_s,
                threshold_min * 60,
                tv_state,
                closed.id,
                closed.bundle_id,
            )
        # Audit row so the parent dashboard sees the cleanup.
        try:
            from .audit import record_admin_action
            if action is StaleAction.CLOSE_RUNAWAY:
                detail = (
                    f"{closed.bundle_id} runaway-ceiling fired "
                    f"(open_for={int(open_event_age_s or 0)}s >= "
                    f"{STALE_RUNAWAY_CEILING_S}s); tv_state={tv_state}; "
                    f"closed at last_updated {last_updated.isoformat()}"
                )
            else:
                detail = (
                    f"{closed.bundle_id} entity stale {int(age_s or 0)}s; "
                    f"tv_state={tv_state}; closed at last_updated "
                    f"{last_updated.isoformat()}"
                )
            record_admin_action(
                self.hass,
                profile_id=self._profile.id,
                action="app_stale_closed",
                detail=detail,
                actor="system",
            )
        except Exception:  # noqa: BLE001 — audit must never crash the tick
            _LOGGER.debug("app_stale_closed audit row failed (non-fatal)")
        await self._store.async_save()

    async def _check_pyatv_reload(self, now: datetime) -> None:
        """v0.18.0 — In-integration proactive pyatv reload.

        Heals the symptom where pyatv's push channel dies silently and
        the apple_tv media_player entity sits at `idle`/`on` for hours
        (last_updated frozen on the last push). The stale-session path
        does NOT engage in this case (no open event to close OR entity
        not in playing/paused/buffering), so without this self-heal
        usage attribution stays broken until the user manually reloads.

        The decision is in `media_attribution.decide_pyatv_reload` — a
        pure function unit-tested without HA. This method is a thin
        adapter that reads the relevant entities, queries AdGuard's
        recent DNS log for the Apple TV's IP, clamps clock skew, calls
        the pure function, and (on True) fires
        `hass.config_entries.async_reload(apple_tv_entry_id)`.

        Disabled when `profile.apple_tv_entry_id` is empty (default for
        old persisted profiles). The setup path in __init__ resolves the
        entry id from the HA entity registry on every restart, so the
        feature self-arms once the registry has materialized.

        Fail-CLOSED on every gate / on any exception: the reload is
        disruptive (briefly interrupts pyatv mid-reconnect) so we err
        on the side of NOT firing.
        """
        # v0.19.0 — Apple TV / pyatv only. Xbox profiles can't have a
        # pyatv config entry to reload.
        if getattr(self._profile, "device_kind", "apple_tv") != "apple_tv":
            return
        atv_entry_id = (
            getattr(self._profile, "apple_tv_entry_id", "") or ""
        ).strip()
        if not atv_entry_id:
            return

        # --- gather inputs ---
        atv_st = self.hass.states.get(self._profile.apple_tv_entity_id)
        apple_tv_state = atv_st.state if atv_st is not None else None
        pyatv_quiet_s: float | None = None
        if atv_st is not None and atv_st.last_updated is not None:
            pyatv_quiet_s = max(
                0.0, (now - atv_st.last_updated).total_seconds()
            )

        tv_entity_id = getattr(self._profile, "tv_entity_id", None) or None
        samsung_state: str | None = None
        if tv_entity_id:
            tv_st_obj = self.hass.states.get(tv_entity_id)
            samsung_state = tv_st_obj.state if tv_st_obj is not None else None

        # DNS-recency corroborator. Bounded query (1s timeout, 50 row cap).
        # Any error -> 0 hits = fail-CLOSED (no reload).
        dns_recent_hits = 0
        adguard = getattr(self._enforcer, "_adguard", None)
        atv_ip = (getattr(self._profile, "apple_tv_ip", "") or "").strip()
        if adguard is not None and atv_ip:
            try:
                rows = await adguard.query_recent_dns(
                    atv_ip,
                    limit=50,
                    timeout_s=1.0,
                    since_seconds=DNS_RECENT_WINDOW_S,
                )
                dns_recent_hits = len(rows)
            except Exception:  # noqa: BLE001 — fail-closed
                _LOGGER.debug(
                    "v0.18.0 pyatv-reload: AdGuard recent-dns query raised; "
                    "treating as 0 hits (fail-closed, no reload)"
                )
                dns_recent_hits = 0

        last_reload_age_s: float | None = None
        if self._last_pyatv_reload_at is not None:
            last_reload_age_s = max(
                0.0, (now - self._last_pyatv_reload_at).total_seconds()
            )

        should_reload = decide_pyatv_reload(
            PyatvReloadInputs(
                apple_tv_state=apple_tv_state,
                pyatv_quiet_s=pyatv_quiet_s,
                samsung_state=samsung_state,
                dns_recent_hits=dns_recent_hits,
                last_reload_age_s=last_reload_age_s,
            )
        )

        if not should_reload:
            return

        # Set the rate-limit timestamp BEFORE awaiting async_reload. If the
        # reload itself fails (raises) we still want to wait the full
        # rate-limit window before trying again rather than retry-storming
        # a broken reload path.
        self._last_pyatv_reload_at = now

        _LOGGER.warning(
            "v0.18.0 pyatv proactive reload: apple_tv=%s quiet=%.0fs "
            "samsung=%s dns_hits=%d — reloading entry %s",
            apple_tv_state,
            pyatv_quiet_s or 0.0,
            samsung_state,
            dns_recent_hits,
            atv_entry_id,
        )

        # Audit row so the parent dashboard sees the self-heal action.
        try:
            from .audit import record_admin_action
            detail = (
                f"apple_tv={apple_tv_state} "
                f"quiet={int(pyatv_quiet_s or 0)}s "
                f"samsung={samsung_state} "
                f"dns_hits={dns_recent_hits} "
                f"target_entry={atv_entry_id}"
            )
            record_admin_action(
                self.hass,
                profile_id=self._profile.id,
                action="pyatv_reload_triggered",
                detail=detail,
                actor="system",
            )
        except Exception:  # noqa: BLE001 — audit must never crash the tick
            _LOGGER.debug(
                "pyatv_reload_triggered audit row failed (non-fatal)"
            )

        # Fire the reload. On success, the OTHER integration's entry is
        # torn down + rebuilt — our own coordinator instance is untouched.
        # Wrap in try/except so a failed reload (e.g. integration crash)
        # never propagates up and crashes our tick.
        try:
            await self.hass.config_entries.async_reload(atv_entry_id)
        except Exception as err:  # noqa: BLE001 — non-fatal
            _LOGGER.warning(
                "v0.18.0 pyatv proactive reload failed (non-fatal): %s",
                err,
            )

    def _effective_bundle_id(
        self, media_state: str | None, app_id: str | None, now: datetime
    ) -> str | None:
        """Thin wrapper around `media_attribution.resolve_effective_bundle_id`.

        The pure logic lives in `media_attribution.py` so it can be unit
        tested without HA imports (see test_media_attribution.py for the
        full matrix). This method exists only to update our instance state
        from the result.
        """
        result = resolve_effective_bundle_id(
            media_state=media_state,
            app_id=app_id,
            now=now,
            last_known_bundle_id=self._last_known_bundle_id,
            last_known_seen_at=self._last_known_seen_at,
            idle_grace_minutes=self._profile.idle_grace_minutes,
        )
        self._last_known_bundle_id = result.last_known_bundle_id
        self._last_known_seen_at = result.last_known_seen_at
        return result.bundle_id

    # ----- coordinator tick -----

    async def _async_update_data(self) -> dict:
        now = dt_util.utcnow()
        # v0.17.1 — close stuck open sessions FIRST so the rest of the
        # tick computes against accurate usage. Catches pyatv silent
        # disconnect: HA's entity stays at the last cached "playing"
        # state for hours, never triggering _sync_open_event, and the
        # open UsageEvent accrues phantom time. The check is a no-op
        # when the entity is recent or stale_session_minutes is 0.
        await self._check_for_stale_session(now=now)
        # v0.18.0 — proactive pyatv reload (in-integration self-heal).
        # Catches the failure mode the stale-session path can't: pyatv
        # push-quiet while apple_tv is `idle`/`on` (no open event to
        # close). When all gates pass, calls async_reload on the Apple
        # TV core integration's entry to restart pyatv. No-op when
        # apple_tv_entry_id is empty. Fail-closed on every gate.
        await self._check_pyatv_reload(now=now)
        # v0.18.0 — DNS-corroborated attribution. Queries AdGuard for the
        # Apple TV's recent DNS and corrects bundle/group attribution when
        # pyatv is push-silent. No-op when feature is off or no IP set.
        # Fail-open: AdGuard errors collapse to v0.17.3 behavior.
        await self._check_dns_corroboration(now=now)
        # v0.18.1 — TV-on accumulator (observability only). Reads the
        # configured tv_entity_id and accrues seconds while it's in an
        # on-ish state. Pure local-state update — no I/O, no enforcement,
        # no event mutation. Surfaced as samsung_tv_on_minutes_today.
        self._update_tv_on_counter(now=now)
        # v0.21.1 (FIX 2b) — one-time native-TV excluded-source misconfig check.
        # Self-guards after the first non-empty source_list read; cheap no-op
        # otherwise. Surfaces a mismatch that would silently mis-book native TV.
        self._maybe_warn_native_source_list_mismatch()
        # Recompute "effective" from current state every tick so an open event
        # that's gone stale (e.g. device turned off without a state event) gets
        # closed eventually. v0.20.0 — resolve the whole room (primary +
        # secondaries) into ONE effective bundle so EITHER device accrues to the
        # single shared budget (Apple-TV-wins, never double-counted).
        primary_state = self.hass.states.get(self._profile.apple_tv_entity_id)
        if (
            primary_state is None
            and not self._secondary_entity_ids()
            and not self._native_tv_entity_id()  # v0.21.0 — native TV resolves too
        ):
            # Back-compat: a single-device profile whose primary entity is
            # missing attributes nothing this tick (pre-v0.20.0 behavior — no
            # resolver call, no side effect). A merged profile (with
            # secondaries) always resolves so the Xbox is tracked even when the
            # Apple TV entity is absent.
            _LOGGER.debug(
                "tracking entity %s missing — cannot attribute usage",
                self._profile.apple_tv_entity_id,
            )
        else:
            effective, from_secondary = self._resolve_room_activity(now)
            await self._sync_open_event(
                effective=effective,
                from_secondary=from_secondary,
                now=now,
            )

        used_s = self._store.used_seconds_today(self._profile.id, now=now)
        extension_s = self._store.extension_minutes_today(self._profile.id) * 60

        # Resolve TODAY'S effective values via the per-weekday schedule
        # (v0.11.0). When no per-weekday overrides are set, these return
        # exactly the base Profile values, preserving prior behavior.
        local_today = dt_util.as_local(now).date()
        today_budget_min = effective_daily_budget(
            base_min=self._profile.daily_budget_min,
            weekday_overrides=self._profile.weekday_budgets_min or {},
            on=local_today,
        )
        today_group_budgets = effective_group_budgets(
            base_groups=self._profile.group_budgets or {},
            weekday_group_overrides=self._profile.weekday_group_budgets_min or {},
            on=local_today,
        )
        today_quiet_windows_string = effective_quiet_windows_string(
            base_string=self._profile.quiet_windows or "",
            weekday_overrides=self._profile.weekday_quiet_windows or {},
            on=local_today,
        )

        budget_s = today_budget_min * 60 + extension_s
        remaining_s = max(0, budget_s - used_s)

        open_event = self._store.open_event_for(self._profile.id)
        current_bundle_id = open_event.bundle_id if open_event else None

        # Categorize the current app (CURATED first, then cached iTunes,
        # then None) and trigger an async iTunes lookup if it's truly new.
        current_group = self._group_for(current_bundle_id)
        if (
            current_bundle_id
            and current_bundle_id != "unknown"
            and current_group is None
            and current_bundle_id not in self._itunes_lookups_attempted
        ):
            self.hass.async_create_task(
                self._async_resolve_group(current_bundle_id)
            )

        # Per-group totals for today.
        group_totals_s = self._store.group_totals_today(
            self._profile.id,
            bundle_to_group=lambda bid: self._group_for(bid) or GROUP_OTHER,
            now=now,
        )
        # Apply TODAY'S effective per-group budgets.
        group_budgets_s: dict[str, int] = {
            g: int(mins) * 60 for g, mins in today_group_budgets.items()
        }
        current_group_used_s = (
            group_totals_s.get(current_group, 0) if current_group else 0
        )
        current_group_budget_s = (
            group_budgets_s.get(current_group) if current_group else None
        )
        # v0.19.1 — per-group extension grants (seconds). The enforcer adds
        # each group's tagged extension to that group's budget so a parent's
        # "+30 min" lifts the binding GROUP, not just the daily pool.
        group_extensions_s: dict[str, int] = {
            g: int(mins) * 60
            for g, mins in self._store.group_extensions_today(
                self._profile.id
            ).items()
        }

        # Adult-mode override (auto-expires inside the store).
        adult_active = self._store.is_adult_mode_active(self._profile.id, now=now)

        await self._enforcer.evaluate(
            used_seconds=used_s - extension_s,
            now=now,
            today_budget_min=today_budget_min,
            today_quiet_windows_string=today_quiet_windows_string,
            current_group=current_group,
            current_group_used_seconds=current_group_used_s,
            current_group_budget_seconds=current_group_budget_s,
            # v0.16.5 — full per-group totals + budgets so the enforcer
            # can pin enforcement when ANY group is exhausted today,
            # even during periods where current_group is None (Apple TV
            # idle / standby). See enforcer.evaluate for the rationale.
            group_totals_seconds=group_totals_s,
            group_budgets_seconds=group_budgets_s,
            group_extensions_seconds=group_extensions_s,
            adult_mode_active=adult_active,
        )

        # v0.12.2: surface the current session's start + duration in the
        # snapshot so the panel can show "Netflix — since 10:13 (1.5 min)"
        # without re-deriving from the event log.
        if open_event:
            current_session_started_at = open_event.started_at.isoformat()
            current_session_duration_s = int(
                (now - open_event.started_at).total_seconds()
            )
        else:
            current_session_started_at = None
            current_session_duration_s = 0

        snapshot = {
            "used_seconds_today": used_s,
            "remaining_seconds_today": remaining_s,
            # v0.16.3 — the binding constraint's remaining seconds (group
            # OR daily). Audit voice-substitution reads this so warn
            # announcements show "5 min of movies" instead of "145 min"
            # when a group budget is the binding constraint.
            "effective_remaining_seconds": self._enforcer.effective_remaining_seconds,
            "budget_seconds_today": budget_s,
            "extension_minutes_today": self._store.extension_minutes_today(self._profile.id),
            "current_bundle_id": current_bundle_id,
            "current_group": current_group,
            "current_session_started_at": current_session_started_at,
            "current_session_duration_s": current_session_duration_s,
            "enforcement_state": self._enforcer.state,
            "enforce_reason": self._enforcer.enforce_reason,
            "is_blocked": self._enforcer.is_blocked,
            # v0.15.0 (spec §3.4.3) — True when the last _enter_enforcing
            # couldn't fully take effect (AdGuard 5xx OR every turn_off
            # path failed). The effective_state sensor uses this to
            # surface `enforcing_failed` instead of `enforcing` so the
            # parent isn't lied to. Cleared on _exit_enforcing.
            "enforcement_failed": self._enforcer._last_enforcement_failed,
            "active_quiet_window": self._enforcer.active_quiet_label,
            "adult_mode_active": adult_active,
            "adult_mode_until": (
                self._store.adult_mode_until(self._profile.id).isoformat()
                if self._store.adult_mode_until(self._profile.id)
                else None
            ),
            "group_totals_seconds": group_totals_s,
            "group_budgets_minutes": dict(today_group_budgets),
            "today_budget_min": today_budget_min,
            "today_weekday": weekday_key(local_today),
            # v0.18.0 diagnostic sensors — surface the DNS-corroboration
            # story for the parent dashboard.
            "attribution_source": self._last_attribution_source,
            "dns_classifier_confidence": self._last_dns_confidence,
            "attribution_gap_minutes_today": self._compute_gap_minutes_today(now),
            # v0.18.1 — TV-on minutes today (observability only).
            "tv_on_seconds_today": self._tv_on_seconds_today,
        }
        self.hass.bus.async_fire(
            EVENT_USAGE_UPDATED,
            {"profile_id": self._profile.id, **snapshot},
        )
        return snapshot

    # v0.18.1 — TV-on accumulator (observability only). States we treat as
    # "the screen is showing something" — strictly, what the configured
    # display reports while in active use. `off`/`standby`/`unavailable`/
    # `unknown`/`idle` are NOT counted. `playing`/`paused`/`buffering` are
    # included so the counter works on Samsung integrations that report
    # media states directly instead of the generic `on`.
    _TV_ON_STATES = frozenset({"on", "playing", "paused", "buffering"})
    # Sanity cap on a single-tick delta so a clock jump (DST, sleep+wake
    # of the HA host, etc.) can't dump hours into the counter. The normal
    # tick interval is ~30s; 10 min covers reasonable jitter and slow
    # hosts without being a silent over-count.
    _TV_ON_MAX_TICK_DELTA_S = 600.0

    def _update_tv_on_counter(self, now: datetime) -> None:
        """v0.18.1 — accrue seconds for the TV-on diagnostic sensor.

        Cheap per-tick state read. Short-circuits when no `tv_entity_id`
        is configured (counter stays at 0). On state transitions the
        accumulator credits the gap between the previous on-tick and
        now — so a TV that switched off mid-window still gets credit
        for time up to the last tick that saw it on.
        """
        tv_entity_id = getattr(self._profile, "tv_entity_id", None) or None
        if not tv_entity_id:
            self._tv_on_last_seen_at = None
            return
        st = self.hass.states.get(tv_entity_id)
        tv_state = st.state if st is not None else None
        if tv_state in self._TV_ON_STATES:
            if self._tv_on_last_seen_at is not None:
                delta = (now - self._tv_on_last_seen_at).total_seconds()
                if 0 < delta <= self._TV_ON_MAX_TICK_DELTA_S:
                    self._tv_on_seconds_today += delta
            self._tv_on_last_seen_at = now
        else:
            self._tv_on_last_seen_at = None

    def _compute_gap_minutes_today(self, now: datetime) -> float:
        """v0.18.0 — sum minutes spent in DNS-driven group corrections today.

        "Attribution gap" minutes are time the v0.18.0 DNS corroborator
        REWROTE the group attribution for — either by appending a
        `GroupSegment(source='dns_classifier', ...)` (the bundle stayed
        Disney+ but the minutes were reclassified) or by force-closing
        an event under the AMBIENT-ambient path (which leaves an open
        last-known-good segment behind whose group differs from the
        bundle's curated group). Both are "visible-gap" symptoms of
        pyatv push-silence that the parent dashboard should be able to
        glance at.

        Iterates raw events for the profile (NOT `events_today`, which
        drops `group_segments` during clipping) and tallies the seconds
        each dns_classifier-sourced segment contributed within today's
        local-midnight-to-now window. Returns minutes rounded to 1
        decimal. Pre-v0.18.0 events have no segments and contribute 0,
        as do legacy `stale_closed` rows — exactly the right behavior
        (those were honest pyatv-silence closes, not DNS corrections).
        """
        local_now = dt_util.as_local(now)
        day_start_local = local_now.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day_start_utc = dt_util.as_utc(day_start_local)
        total_s = 0
        for event in self._store.events_for_profile(self._profile.id):
            if not event.group_segments:
                continue
            evt_end = event.ended_at or now
            if evt_end <= day_start_utc:
                continue  # entirely in the past
            for seg in event.group_segments:
                if seg.source != "dns_classifier":
                    continue
                seg_end = seg.ended_at or evt_end
                seg_start = max(seg.started_at, day_start_utc)
                seg_end_clipped = min(seg_end, now)
                if seg_end_clipped <= seg_start:
                    continue
                total_s += int((seg_end_clipped - seg_start).total_seconds())
        return round(total_s / 60.0, 1)

    def _group_for(self, bundle_id: str | None) -> str | None:
        """Resolve a bundle_id to a group via CURATED + the store's iTunes cache.

        Returns `None` if completely unknown (caller may trigger a lookup).
        """
        if not bundle_id:
            return None
        return categorize(
            bundle_id, cache=self._store.app_categories()
        )

    async def _async_resolve_group(self, bundle_id: str) -> None:
        """Look up `bundle_id` via Apple's iTunes Search API and cache the result."""
        self._itunes_lookups_attempted.add(bundle_id)
        session = async_get_clientsession(self.hass)
        group = await lookup_itunes(session, bundle_id)
        if group is None:
            _LOGGER.info(
                "iTunes lookup for %s returned no result — treating as 'other'",
                bundle_id,
            )
            group = GROUP_OTHER
        self._store.cache_category(bundle_id, group)
        await self._store.async_save()
