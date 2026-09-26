"""System-action audit recorder.

Subscribes to the integration's own bus events and persists a short,
human-readable entry in the store's `_actions` log. The panel addon's
Dashboard reads this back via `GET /profiles/{id}/actions` so the user
can see what the integration actually did (yesterday's 09:15 morning
enforcement is no longer invisible at 18:00).

Single listener per config entry, registered from `async_setup_entry`,
torn down on `async_unload_entry`. Decoupled from the enforcer — the
enforcer just fires `EVENT_ENFORCEMENT_CHANGED` with enough payload
for us to record an entry without inspecting controller internals.

Bypass-coalescing happens at the storage layer (see `record_action`),
not here.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    EVENT_APP_ENDED,
    EVENT_APP_STARTED,
    EVENT_ENFORCEMENT_CHANGED,
    EVENT_LIMITS_UPDATED,
    EVENT_REQUEST_DECIDED,
    STATE_ENFORCING,
    STATE_GRACE,
    STATE_OK,
    STATE_WARNING,
)
from .storage import AppleTVMgmtStore
from .voice_notifier import format_message, should_speak, speak

_LOGGER = logging.getLogger(__name__)


def _human_reason(reason: str | None) -> str | None:
    """Shorter strings for the panel UI."""
    if not reason:
        return None
    if reason.startswith("group:"):
        return f"{reason.split(':', 1)[1]} budget"
    if reason.startswith("quiet:"):
        return f"quiet window ({reason.split(':', 1)[1]})"
    return {
        "daily_limit": "daily budget reached",
        "force_block": "forced by service call",
        "adult_mode": "adult mode active",
    }.get(reason, reason)


def register_action_recorder(
    hass: HomeAssistant, *, profile_id: str
) -> Callable[[], None]:
    """Wire up bus listeners that record actions for this profile.

    Returns an unsubscribe callable. The recorder is profile-scoped so
    when one entry unloads, we don't tear down the other entries' loggers.
    """

    def _store() -> AppleTVMgmtStore | None:
        bundle = hass.data.get(DOMAIN, {}).get(profile_id)
        return bundle["store"] if bundle else None

    def _current_bundle_id() -> str | None:
        bundle = hass.data.get(DOMAIN, {}).get(profile_id)
        if not bundle:
            return None
        snap = (bundle.get("coordinator").data or {}) if bundle.get("coordinator") else {}
        return snap.get("current_bundle_id")

    def _profile() -> "Profile | None":  # noqa: F821 — forward ref
        bundle = hass.data.get(DOMAIN, {}).get(profile_id)
        return bundle["profile"] if bundle else None

    def _decision(profile):
        """v0.15.0 — compute the policy.should_act decision once, off the
        live mode + adult_mode_until. Single source of truth that replaces
        the per-site duplicated checks of v0.14.x.
        """
        from .policy import should_act, voice_allowed  # local import: avoid early
        from homeassistant.util import dt as dt_util
        store = _store()
        until = store.adult_mode_until(profile_id) if store is not None else None
        mode = getattr(profile, "mode",
                       "enforced" if getattr(profile, "enforcement_enabled", True)
                       else "monitor_only")
        return should_act(mode=mode, adult_mode_until=until, now=dt_util.utcnow())

    def _enforcement_suppressed(profile) -> bool:
        """Back-compat shim retained while the audit detail texts below
        reference it. Returns True when no real enforcement would happen.
        Internally consults the unified `should_act` decision."""
        return _decision(profile).kind != "ACT"

    def _suppression_label(profile) -> str | None:
        """Human-friendly label matching spec §3.3 user terminology.
        Returns None when there's nothing to label (ACT path)."""
        d = _decision(profile)
        if d.reason == "adult_mode":
            return "adult mode active"
        if d.reason == "paused":
            return "paused"
        if d.reason == "monitor_only":
            return "monitor only"
        return None

    def _voice_allowed_for(profile, trigger: str) -> bool:
        """Spec §3.7 voice gate, with the warn_in_monitor_mode caller-side
        check applied here so callers don't need to duplicate it."""
        from .policy import voice_allowed  # local import — same reason as above
        d = _decision(profile)
        if not voice_allowed(d, trigger):
            return False
        # Monitor-mode heads-up voices (warning + countdown) are gated
        # further by the per-profile flag. Per PO D2, default is False
        # (silent monitor mode); user opts in via PATCH
        # warn_in_monitor_mode=true for the calibration use case — same
        # flag covers both warnings and the countdown cue (v0.16.4 fix
        # for the 13:13 grace-window incident).
        if trigger in ("warning", "countdown") and d.kind == "OBSERVE":
            return bool(getattr(profile, "warn_in_monitor_mode", False))
        return True

    def _someone_watching(profile) -> bool:
        """v0.19.2 — is anyone plausibly in front of the screen right now?

        Suppresses heads-up voices (warn) when both the primary device is
        inactive AND the TV is off — don't nag an empty room. See
        media_attribution.someone_could_be_watching for the rationale (live
        bug 2026-06-19: phantom voices after adult-mode expiry, devices off)."""
        from .media_attribution import someone_could_be_watching

        primary = hass.states.get(profile.apple_tv_entity_id)
        tv_id = getattr(profile, "tv_entity_id", None) or None
        tv = hass.states.get(tv_id) if tv_id else None
        # v0.20.0 — a folded-in secondary (e.g. the Xbox) being active counts as
        # "someone watching" so Xbox-only play still warns with the Apple TV off.
        secondary_active = False
        for dev in (getattr(profile, "secondary_devices", None) or []):
            eid = dev.get("entity_id")
            if not eid:
                continue
            st = hass.states.get(eid)
            if st is not None and dev.get("device_kind") == "xbox_presence" \
                    and st.state == "home":
                secondary_active = True
                break
        return someone_could_be_watching(
            getattr(profile, "device_kind", "apple_tv"),
            primary.state if primary is not None else None,
            tv.state if tv is not None else None,
            secondary_active=secondary_active,
        )

    def _remaining_min() -> int | None:
        bundle = hass.data.get(DOMAIN, {}).get(profile_id)
        if not bundle:
            return None
        # v0.16.3 — prefer LIVE values from the enforcer, not the
        # coordinator snapshot. The snapshot lags by one tick: the
        # coordinator only assigns `self.data = snapshot` AFTER
        # `_async_update_data` returns, but the warn voice fires from
        # WITHIN `evaluate()` (via bus event → `_on_enforcement_changed`
        # → `_maybe_speak`). Reading `coordinator.data` there sees the
        # PREVIOUS tick — for the live-reported 2026-05-29 bug that
        # was the OK tick's raw daily remaining ("Noch 68 Minuten")
        # even though group movies had just tripped the WARN at 2 min
        # remaining. Reading from the enforcer's property avoids the
        # lag entirely.
        enforcer = bundle.get("enforcer")
        if enforcer is not None and hasattr(enforcer, "effective_remaining_seconds"):
            rem_s = enforcer.effective_remaining_seconds
            return max(0, int(round(int(rem_s) / 60)))
        # Fallback: coordinator snapshot (used in tests + when the
        # enforcer isn't on the bundle yet, e.g. during setup).
        coord = bundle.get("coordinator")
        if coord is None:
            return None
        snap = coord.data or {}
        rem_s = snap.get("effective_remaining_seconds")
        if rem_s is None:
            rem_s = snap.get("remaining_seconds_today")
        if rem_s is None:
            return None
        return max(0, int(round(int(rem_s) / 60)))

    async def _maybe_speak(
        template: str, *, audit_action: str, minutes: int | None = None
    ) -> None:
        """Speak the template if profile has voice config + template set,
        and record a voice_announcement audit entry on success.

        If `minutes` is explicitly passed it's used as-is (e.g. the
        amount granted by an extension); otherwise we use the current
        remaining_today_min (good for warnings)."""
        profile = _profile()
        store = _store()
        if profile is None or store is None:
            return
        if not should_speak(profile, template):
            return
        if minutes is None:
            minutes = _remaining_min()
        result = await speak(
            hass,
            profile,
            template=template,
            minutes=minutes,
            app=_current_bundle_id(),
        )
        if result.get("status") == "spoken":
            store.record_action(
                profile_id=profile_id,
                action="voice_announcement",
                reason=audit_action,
                detail=result.get("message"),
                bundle_id=_current_bundle_id(),
            )
            await store.async_save()

    async def _persist() -> None:
        store = _store()
        if store is not None:
            await store.async_save()

    @callback
    def _on_enforcement_changed(event: Event) -> None:
        data = event.data
        if data.get("profile_id") != profile_id:
            return
        store = _store()
        if store is None:
            return
        state = data.get("state")
        prev = data.get("prev_state")
        reason = data.get("reason")
        # v0.17.0 F-I — prev_reason carries the binding reason from
        # BEFORE this tick's evaluate() mutation. Falls back to `reason`
        # for back-compat with events that don't supply it (force_block,
        # unblock, tests).
        prev_reason = data.get("prev_reason") or reason
        bundle_id = _current_bundle_id()

        if state == STATE_ENFORCING:
            # v0.15.0 — surface the should_act bypass/observe reason in
            # the audit detail so the parent sees at a glance whether
            # the integration would have blocked. Replaces the per-mode
            # branching from v0.14.x. Wording matches the user-facing
            # terminology in spec §3.3.
            profile = _profile()
            human = _human_reason(reason) or ""
            label = _suppression_label(profile) if profile is not None else None
            if label:
                detail = f"{human} ({label} — not blocked)"
            else:
                detail = human
            entry = store.record_action(
                profile_id=profile_id,
                action="enforce_start",
                reason=reason,
                detail=detail or None,
                bundle_id=bundle_id,
                actor="system",
            )
            if entry.action == "bypass_attempt":
                _LOGGER.warning(
                    "Bypass detected: profile=%s %d attempts in coalesce window",
                    profile_id,
                    entry.count,
                )
            # v0.14.5: the enforce-message voice MOVED OUT of this
            # listener — it now fires from EnforcementController._enter_
            # enforcing AFTER at least one turn_off has verified success.
            # That eliminates the "the announcement said the TV would
            # be shut down but Turn-off FAILED was logged the same
            # second" lie. The audit-row above is still recorded so the
            # state transition stays visible; only the spoken message
            # is now gated on actual effect.
        elif prev == STATE_ENFORCING and state == STATE_OK:
            # v0.17.0 F-P (Sonnet F4) — the unreachable STATE_WARNING
            # arm has been removed. `state.compute_next_state` only
            # transitions ENFORCING → OK when `remaining > 0`, never
            # ENFORCING → WARNING. The pre-fix `state in (STATE_OK,
            # STATE_WARNING)` was dead code that misled future engineers.
            # v0.17.0 F-I — use prev_reason so quiet:Bedtime + similar
            # attributions survive the relax-to-OK transition.
            store.record_action(
                profile_id=profile_id,
                action="enforce_end",
                reason=prev_reason,
                detail=_human_reason(prev_reason) or "released",
                bundle_id=bundle_id,
                actor="system",
            )
        elif prev == STATE_GRACE and state == STATE_OK:
            # v0.17.0 F-L (Opus BA audit P2) — grace cleared without
            # transitioning to ENFORCING. The kid stopped watching mid-
            # grace OR an extension landed in time OR a quiet window
            # expired. Pre-v0.17.0 the timeline read "grace started"
            # then nothing — confusing. Now: a `grace_cleared` row
            # closes the cycle on the dashboard with the original
            # binding attribution.
            store.record_action(
                profile_id=profile_id,
                action="grace_cleared",
                reason=prev_reason,
                detail=_human_reason(prev_reason) or "grace cleared",
                bundle_id=bundle_id,
                actor="system",
            )
        elif prev == STATE_WARNING and state == STATE_OK:
            # v0.17.0 F-L — warning cleared. Less visible than grace_cleared
            # (no preceding heads-up voice happens that the parent already
            # heard), but worth recording so close-then-reopen-then-close
            # patterns show on the timeline. The F-D latch (Cohort 3)
            # specifically PREVENTS this transition when a group binding
            # was active — when this row DOES fire it means a legitimate
            # clear (daily-binding ticked back, extension granted, etc.).
            store.record_action(
                profile_id=profile_id,
                action="warn_cleared",
                reason=prev_reason,
                detail=_human_reason(prev_reason) or "warning cleared",
                bundle_id=bundle_id,
                actor="system",
            )
        elif state == STATE_GRACE and prev != STATE_GRACE:
            # v0.16.2 — audit GRACE entry. Was previously invisible:
            # dashboard showed OK then ENFORCING with nothing between,
            # making the grace period look skipped (Sonnet BA audit
            # 2026-05-28 Finding 2). Helps the parent see the
            # heads-up window actually ran.
            store.record_action(
                profile_id=profile_id,
                action="grace_start",
                reason=reason,
                detail=_human_reason(reason) or "grace period started",
                bundle_id=bundle_id,
                actor="system",
            )
        elif state == STATE_WARNING and prev != STATE_WARNING:
            store.record_action(
                profile_id=profile_id,
                action="warn",
                reason=reason,
                detail=_human_reason(reason),
                bundle_id=bundle_id,
                actor="system",
            )
            # v0.13.0: speak the warning message as the heads-up.
            # v0.15.0: unified through policy.voice_allowed (spec §3.7).
            # Default for monitor mode is silent (PO D2); user opts in
            # via warn_in_monitor_mode=True for the calibration case.
            # Adult mode / paused mode never warn (the bypass would
            # make the warning misleading).
            profile = _profile()
            if (
                profile is not None
                and profile.warning_message
                and _voice_allowed_for(profile, "warning")
                # v0.19.2 — don't warn an empty room (device idle + TV off).
                and _someone_watching(profile)
            ):
                hass.async_create_task(
                    _maybe_speak(profile.warning_message, audit_action="warn")
                )
        else:
            return
        hass.async_create_task(_persist())

    @callback
    def _on_request_decided(event: Event) -> None:
        data = event.data
        if data.get("profile_id") != profile_id:
            return
        store = _store()
        if store is None:
            return
        status = data.get("status") or "decided"
        granted = data.get("granted_minutes")
        by = data.get("decided_by") or "?"
        detail = f"{status} by {by}"
        if granted:
            detail += f" ({granted}m)"
        store.record_action(
            profile_id=profile_id,
            action="decision",
            reason=status,
            detail=detail,
            actor=data.get("actor"),
        )
        # v0.14.1 — speak the extension_message when a request is
        # actually approved with positive minutes. Denials / expiries
        # don't announce (the kid hears silence).
        if status == "approved" and isinstance(granted, int) and granted > 0:
            profile = _profile()
            if profile is not None and profile.extension_message:
                hass.async_create_task(
                    _maybe_speak(
                        profile.extension_message,
                        audit_action="extension_approved",
                        minutes=granted,
                    )
                )
        hass.async_create_task(_persist())

    @callback
    def _on_app_started(event: Event) -> None:
        data = event.data
        if data.get("profile_id") != profile_id:
            return
        store = _store()
        if store is None:
            return
        bundle_id = data.get("bundle_id")
        name = data.get("display_name") or bundle_id or "?"
        store.record_action(
            profile_id=profile_id,
            action="app_started",
            reason=None,
            detail=f"{name} opened",
            bundle_id=bundle_id,
            actor="system",
        )
        hass.async_create_task(_persist())

    @callback
    def _on_app_ended(event: Event) -> None:
        data = event.data
        if data.get("profile_id") != profile_id:
            return
        store = _store()
        if store is None:
            return
        bundle_id = data.get("bundle_id")
        name = data.get("display_name") or bundle_id or "?"
        mins = data.get("duration_minutes")
        if mins is not None:
            detail = f"{name} closed ({mins} min)"
        else:
            detail = f"{name} closed"
        store.record_action(
            profile_id=profile_id,
            action="app_ended",
            reason=None,
            detail=detail,
            bundle_id=bundle_id,
            actor="system",
        )
        hass.async_create_task(_persist())

    @callback
    def _on_limits_updated(event: Event) -> None:
        data = event.data
        if data.get("profile_id") != profile_id:
            return
        store = _store()
        if store is None:
            return
        # v0.15.0 — `api.py` fires the event with `updated_fields`; older
        # spec mentions `changed`. Accept either to allow downstream
        # consumers / future emitters to use either key. Without this
        # fallback, the audit row always showed "limits updated" instead
        # of the actual field list (pre-existing bug).
        changed = data.get("updated_fields") or data.get("changed") or []
        detail = ", ".join(changed)[:120] if changed else "limits updated"
        store.record_action(
            profile_id=profile_id,
            action="limits_changed",
            reason=None,
            detail=detail,
            actor=data.get("actor"),
        )
        hass.async_create_task(_persist())

    unsubs = [
        hass.bus.async_listen(EVENT_ENFORCEMENT_CHANGED, _on_enforcement_changed),
        hass.bus.async_listen(EVENT_REQUEST_DECIDED, _on_request_decided),
        hass.bus.async_listen(EVENT_LIMITS_UPDATED, _on_limits_updated),
        hass.bus.async_listen(EVENT_APP_STARTED, _on_app_started),
        hass.bus.async_listen(EVENT_APP_ENDED, _on_app_ended),
    ]

    def _unsubscribe() -> None:
        for u in unsubs:
            try:
                u()
            except Exception:  # noqa: BLE001
                pass

    return _unsubscribe


def resolve_extension_target_group(
    hass: HomeAssistant,
    profile_id: str,
    explicit_group: str | None = None,
    *,
    fallback_most_used: bool = False,
) -> str | None:
    """v0.19.1 — decide which group a manual extension should be tagged to.

    When a parent hits "+30 min", the intent is almost always "give 30 more
    minutes of whatever is being blocked right now" — not "+30 to a daily
    pool that has hours free." So we tag the grant to the binding/active
    group so the enforcer lifts that GROUP budget too (see
    enforcer.evaluate group_extensions). Resolution order:

      1. `explicit_group` if the caller passed one (panel/automation can
         target a specific group; "" / "daily" / None → daily-only).
      2. The currently BINDING group — when the profile is in WARN/GRACE/
         ENFORCING because of a `group:<name>` constraint (read off the
         live enforcer). This is the exact group the kid is being nagged
         about, which is what the parent is reacting to.
      3. The currently ACTIVE group — the group of the app on screen right
         now (coordinator snapshot `current_group`).
      4. (v0.20.4, `fallback_most_used` only) today's MOST-USED capped group
         — so a direct "+X" during a tvOS-26 freeze gap OR just after the app
         closed still relieves whatever was being watched, instead of silently
         landing daily-only and leaving the binding category still nagging.
         Reported live 2026-07-05: +120 granted but only +60 reached the movies
         cap because the rest was granted during freeze gaps → daily-only.
      5. None → daily-only.

    `fallback_most_used` is opt-in so DIRECT parent grants (panel "+X" buttons,
    the `grant_extension` service) get the smart fallback, while the
    request-approval path (notify._resolve_request_group) keeps deferring to
    the *requested* app's group instead.

    Returns a lowercase group name or None.
    """
    if explicit_group is not None:
        g = str(explicit_group).strip().lower()
        # "daily" is an explicit opt-out sentinel → daily-only pool.
        return None if g in ("", "daily") else g
    bundle = hass.data.get(DOMAIN, {}).get(profile_id)
    if not bundle:
        return None
    coord = bundle.get("coordinator")
    enforcer = getattr(coord, "_enforcer", None)
    reason = getattr(enforcer, "enforce_reason", None)
    if reason and reason.startswith("group:"):
        return reason[len("group:"):] or None
    data = getattr(coord, "data", None) or {}
    cur = data.get("current_group")
    if cur:
        return cur
    if fallback_most_used:
        # Most-used group today, restricted to POSITIVE-budget (capped) groups
        # — a 0-budget group is "unlimited" and can never be the binding
        # constraint, so tagging it would waste the grant.
        totals = data.get("group_totals_seconds") or {}
        budgets = data.get("group_budgets_minutes") or {}
        eligible = {
            grp: secs
            for grp, secs in totals.items()
            if secs > 0 and (budgets.get(grp) or 0) > 0
        }
        if eligible:
            return max(eligible, key=lambda grp: eligible[grp])
    return None


def record_extension_granted(
    hass: HomeAssistant,
    *,
    profile_id: str,
    minutes: int,
    new_total: int,
    actor: str | None = None,
    group: str | None = None,
) -> None:
    """v0.17.0 F-K (Opus BA audit P2) — shared extension-grant audit +
    voice. Called from BOTH the REST `POST /extension` endpoint AND
    the `appletv_mgmt.grant_extension` HA service. Pre-v0.17.0 the
    service silently mutated the store without firing the voice or
    recording the `extension` audit row, while REST did both — the owner's
    "kid presses the homework-done button" automation handed the kid
    minutes without the audible confirmation feedback they got from
    REST-driven grants.

    The voice fires only for positive minutes (negative grants take
    time AWAY — speaking the extension message there would be
    misleading; the kid hears silence which matches the REST behavior).

    `actor` is forwarded to the audit row so the source is tagged
    ("ha_service", "rest", "options_flow", etc.). Falls back to None
    for legacy callers.
    """
    bundle = hass.data.get(DOMAIN, {}).get(profile_id)
    if not bundle:
        return
    profile = bundle.get("profile")
    # v0.19.1 — surface the tagged group in the audit detail so the parent
    # can see "+30 min movies (today total 90)" vs a bare daily grant.
    group_suffix = f" {group}" if group else ""
    record_admin_action(
        hass,
        profile_id=profile_id,
        action="extension",
        reason=(f"group:{group}" if group else None),
        detail=(
            f"{'+' if minutes >= 0 else ''}{minutes} min{group_suffix} "
            f"(today total {new_total})"
        ),
        actor=actor,
    )
    if minutes <= 0 or profile is None:
        return
    template = getattr(profile, "extension_message", "") or ""
    if not template:
        return
    from .voice_notifier import should_speak, speak as _speak

    async def _announce_and_log() -> None:
        if not should_speak(profile, template):
            return
        result = await _speak(
            hass,
            profile,
            template=template,
            minutes=minutes,
        )
        if result.get("status") == "spoken":
            record_admin_action(
                hass,
                profile_id=profile_id,
                action="voice_announcement",
                reason="extension",
                detail=result.get("message"),
            )

    hass.async_create_task(_announce_and_log())


def record_admin_action(
    hass: HomeAssistant,
    *,
    profile_id: str,
    action: str,
    reason: str | None = None,
    detail: str | None = None,
    actor: str | None = None,
) -> None:
    """Synchronous helper for the REST views to drop in entries that
    aren't covered by a bus event (e.g. `adult_mode` POST/DELETE,
    `extension` POST). The store save is fire-and-forget via the bundle's
    coordinator refresh path.

    `actor` is the documented enum from spec §3.6 — pass it explicitly
    to attribute the audit row to its source (switch_entity, rest,
    options_flow, etc.). `None` for legacy callers.
    """
    bundle = hass.data.get(DOMAIN, {}).get(profile_id)
    if not bundle:
        return
    store: AppleTVMgmtStore = bundle["store"]
    snap = (bundle.get("coordinator").data or {}) if bundle.get("coordinator") else {}
    store.record_action(
        profile_id=profile_id,
        action=action,
        reason=reason,
        detail=detail,
        bundle_id=snap.get("current_bundle_id"),
        at=dt_util.utcnow(),
        actor=actor,
    )
    hass.async_create_task(store.async_save())
