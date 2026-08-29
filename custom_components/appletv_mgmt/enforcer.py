"""Enforcement controller.

Wires the pure state machine (`state.compute_next_state`) to its side effects:
AdGuard client-blocking via `adguard.AdGuardClient`, plus calling HA's
`media_player.turn_off` service to sleep the Apple TV (which also triggers
HDMI-CEC TV-off when the chain is configured).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from homeassistant.const import SERVICE_TURN_OFF
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .adguard import AdGuardClient, AdGuardError
from .categorize import GROUP_LINEAR_TV
from .const import (
    EVENT_ENFORCEMENT_CHANGED,
    STATE_ENFORCING,
    STATE_GRACE,
    STATE_OK,
    STATE_WARNING,
)
from .quiet import find_active_window, parse_windows
from .state import StateDecision, compute_next_state
from .storage import AppleTVMgmtStore, Profile

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "AdGuardClient",
    "AdGuardError",
    "EnforcementController",
    "StateDecision",
    "compute_next_state",
]


class EnforcementController:
    def __init__(
        self,
        hass: HomeAssistant,
        adguard: AdGuardClient,
        profile: Profile,
        store: "AppleTVMgmtStore | None" = None,  # noqa: F821 (fwd ref ok)
    ) -> None:
        self._hass = hass
        self._adguard = adguard
        self._profile = profile
        # v0.14.3 — store gives us a live read of `is_adult_mode_active`
        # so _enter_enforcing can hard-skip side effects when the parent
        # has explicitly granted bypass time, regardless of which code
        # path tried to force enforcement.
        self._store = store
        self._state: str = STATE_OK
        self._grace_started_at: datetime | None = None
        self._is_blocked: bool = False
        self._lock = asyncio.Lock()
        # Pre-parse quiet windows once at startup; bad input fails loudly here.
        try:
            self._quiet_windows = parse_windows(profile.quiet_windows)
        except ValueError as err:
            _LOGGER.error(
                "Profile %s has invalid quiet_windows %r: %s — ignoring",
                profile.id,
                profile.quiet_windows,
                err,
            )
            self._quiet_windows = []
        self._active_quiet_label: str | None = None
        # Reason for the current state (one of: "daily_limit",
        # "group:<name>", "quiet:<label>", "adult_mode", or None when in OK).
        self._enforce_reason: str | None = None
        # v0.16.3 — the BINDING constraint's remaining seconds (group OR
        # daily, whichever is smaller; 0 when a quiet window is active).
        # Set in every evaluate() so the audit voice-substitution layer
        # can show "5 min of movies" instead of raw daily "145 min" when
        # a group budget is what actually triggered the warn. Without
        # this, warn voice and warn audit row were operating on daily
        # remaining while the state machine was already counting down
        # the group budget — producing a "Noch 145 Minuten" announcement
        # 5 minutes before the kid was actually cut off. Live-reported
        # 2026-05-29 (Living Room profile, group "movies", Netflix).
        self._effective_remaining_s: int = 0
        # v0.21.3 — is the linear_tv (native TV) group itself exhausted today?
        # Recomputed every evaluation while the group data is in hand, then
        # read by `_native_tv_should_enforce()`, which must NOT rely on the
        # binding-reason string alone (see the FIX note there).
        self._native_group_exhausted: bool = False
        # `warn_thresholds_min` is a list for future multi-threshold support,
        # but the state machine currently uses only the largest value as
        # the single "approaching budget" trigger. Surface this honestly
        # in the log on init so users with [5,2,0] understand they get
        # one warning at 5 min, not three. v0.10.0 QA P1 #9.
        if profile.warn_thresholds_min and len(profile.warn_thresholds_min) > 1:
            _LOGGER.info(
                "Profile %s configured %d warn_thresholds_min %r; only the "
                "largest (%d min) is currently used. Multi-threshold "
                "notifications are a future feature.",
                profile.id,
                len(profile.warn_thresholds_min),
                profile.warn_thresholds_min,
                max(profile.warn_thresholds_min),
            )
        # Cache key for per-day quiet windows (v0.11.0). Tracks the last
        # string we parsed so back-to-back ticks on the same day skip
        # the parse work.
        self._last_quiet_windows_string: str = profile.quiet_windows or ""
        # v0.15.0 (spec §3.4.3) — surfaces whether the last enforcement
        # attempt's side effects fully succeeded. Set in _enter_enforcing
        # after AdGuard + turn_off attempts; cleared in _exit_enforcing.
        # Coordinator reads this and surfaces it as
        # `coordinator.data["enforcement_failed"]` for the effective_state
        # sensor's `enforcing_failed` row.
        self._last_enforcement_failed: bool = False
        # v0.16.1 — timer-based countdown (replaces v0.16.0 tick-based logic).
        # When the state machine transitions into GRACE we schedule a
        # one-shot timer for (grace_seconds - 30s) to fire the countdown
        # voice at exactly 30s before enforce. The tick-based check in
        # v0.16.0 missed the window on group-budget transitions (overall
        # remaining stays huge while group remaining hits 0 → state goes
        # OK→GRACE→ENFORCING with no time in WARNING, no tick lands in
        # the 35s window).
        self._countdown_handle: CALLBACK_TYPE | None = None
        # v0.16.0 legacy flag — kept for back-compat with existing tests
        # that drive _maybe_fire_countdown directly. The real production
        # logic uses _countdown_handle (the HA timer handle, above).
        self._countdown_fired: bool = False
        # v0.16.0 — reactivation tracking. `_reactivation_count` is the
        # number of inactive→active Apple TV transitions observed under
        # ENFORCING since we entered ENFORCING; `_was_apple_tv_active_last_tick`
        # is the edge-detector input. Both reset in `_exit_enforcing`.
        # v0.16.1: `_was_apple_tv_active_last_tick` is now seeded
        # correctly in `_enter_enforcing` (was always False before, which
        # made the first watch_reactivation tick after enforce_start
        # trip the False→True edge and fire a phantom "reactivation #1").
        self._reactivation_count: int = 0
        self._was_apple_tv_active_last_tick: bool = False

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_blocked(self) -> bool:
        return self._is_blocked

    @property
    def active_quiet_label(self) -> str | None:
        """Label of the quiet window currently in effect, or None."""
        return self._active_quiet_label

    @property
    def enforce_reason(self) -> str | None:
        """Why the integration is currently enforcing (or None when in OK)."""
        return self._enforce_reason

    @property
    def effective_remaining_seconds(self) -> int:
        """Seconds remaining on the binding constraint (group OR daily).

        Used by the audit layer to substitute `{minutes}` in voice
        templates so warn announcements show what the kid actually has
        left of the binding budget (e.g. "5 min of movies") rather than
        raw daily remaining (e.g. "145 min"). v0.16.3.
        """
        return self._effective_remaining_s

    async def evaluate(
        self,
        *,
        used_seconds: int,
        now: datetime | None = None,
        today_budget_min: int | None = None,
        today_quiet_windows_string: str | None = None,
        current_group: str | None = None,
        current_group_used_seconds: int = 0,
        current_group_budget_seconds: int | None = None,
        group_totals_seconds: dict[str, int] | None = None,
        group_budgets_seconds: dict[str, int] | None = None,
        group_extensions_seconds: dict[str, int] | None = None,
        adult_mode_active: bool = False,
        adult_mode_reason: str | None = None,
    ) -> None:
        """Called by the coordinator on each tick.

        v0.6.0 widens the contract: in addition to the overall daily budget,
        the caller may also pass the current app's group + that group's
        usage/budget. The state machine sees `min(remaining_overall,
        remaining_group)` and trips on whichever runs out first.

        v0.11.0 adds `today_budget_min` + `today_quiet_windows_string`
        so the coordinator can apply per-weekday overrides without the
        enforcer needing to know about the weekday model. When omitted,
        the base profile values are used (back-compat).

        `adult_mode_active=True` short-circuits everything to OK — the
        override switch is the highest-priority signal.
        """
        now = now or dt_util.utcnow()
        # v0.17.0 F-I — snapshot the binding reason BEFORE any of the
        # mutations below overwrite it, so a relax-to-OK transition can
        # still attribute the cleared cycle (`quiet:Bedtime`,
        # `group:movies`, `daily_limit`) on the event payload.
        prev_reason_for_emit = self._enforce_reason

        # Per-day budget — fall back to profile base for back-compat.
        budget_min = (
            today_budget_min
            if today_budget_min is not None
            else self._profile.daily_budget_min
        )
        budget_s = budget_min * 60
        # Only the LARGEST threshold is used for the WARNING transition.
        # The other entries are silently ignored — the multi-threshold
        # config shape pre-dates the single-threshold state machine.
        # QA review v0.10: documented but not yet collapsed to a single
        # int to avoid breaking existing config entries.
        warn_s = (
            max(self._profile.warn_thresholds_min) * 60
            if self._profile.warn_thresholds_min
            else 0
        )

        # Per-day quiet windows: when supplied, re-parse for THIS day.
        # Coordinator supplies the resolved string from schedule helpers;
        # we cache the last parsed string so back-to-back same-day ticks
        # don't re-parse unnecessarily.
        if today_quiet_windows_string is not None and today_quiet_windows_string != self._last_quiet_windows_string:
            try:
                self._quiet_windows = parse_windows(today_quiet_windows_string)
                self._last_quiet_windows_string = today_quiet_windows_string
            except ValueError as err:
                _LOGGER.error(
                    "Per-day quiet_windows %r is invalid: %s — using empty list",
                    today_quiet_windows_string,
                    err,
                )
                self._quiet_windows = []
                self._last_quiet_windows_string = today_quiet_windows_string

        # v0.17.0 F-H (Opus BA audit P2, broadened): paused mode +
        # already-ENFORCING-in-monitor should short-circuit early so
        # the state machine doesn't emit re-noisy WARN→GRACE→ENFORCING
        # audit rows on every tick. Pre-v0.17.0 paused mode walked
        # the full state machine and (post v0.16.5 group-exhaustion
        # pin) monitor mode in ENFORCING re-emitted `enforce_start
        # (monitor only)` every tick because the pin held but
        # `_apply` was called with a "transition into ENFORCING" on
        # each evaluate (no, actually `_apply` is idempotent — only
        # the audit row fires once; but evaluate() still computes
        # everything every tick. The audit-spam case is the
        # binding-reason mutation under monitor-mode for already-
        # ENFORCING. Per the adversarial reviewer.).
        #
        # The check is conditional on the policy decision, not the
        # mode string directly, so a future "paused-like" mode picks
        # up the short-circuit automatically.
        from .policy import should_act
        policy_decision = should_act(
            mode=getattr(self._profile, "mode",
                         "enforced" if getattr(self._profile, "enforcement_enabled", True)
                         else "monitor_only"),
            adult_mode_until=(
                self._store.adult_mode_until(self._profile.id)
                if self._store else None
            ),
            now=now,
        )
        if policy_decision.kind == "BYPASS" and not adult_mode_active:
            # Paused mode (BYPASS without adult-mode being on) — pin
            # to OK silently. Adult mode is handled by the dedicated
            # branch below because it has its own counter-preserve
            # semantics + enforce_reason quirk.
            self._active_quiet_label = None
            self._enforce_reason = None
            self._effective_remaining_s = budget_s - used_seconds
            await self._apply(
                StateDecision(STATE_OK, None), prev_reason=prev_reason_for_emit
            )
            return

        # Adult mode bypasses ALL enforcement, including quiet windows.
        # Wife wants to watch a movie → fine; tracker still records usage
        # so the kids' counter isn't affected the next day.
        if adult_mode_active:
            self._active_quiet_label = None
            # enforce_reason is None when in OK; adult-mode is surfaced
            # via `adult_mode_active` instead (QA P1 #14: panel was
            # rendering "OK — adult_mode" which is contradictory).
            self._enforce_reason = None
            # v0.16.3 — keep effective remaining in sync; no constraint
            # is binding under adult mode so report raw daily remaining.
            self._effective_remaining_s = budget_s - used_seconds
            # v0.17.0 — signal an adult-mode exit so `_exit_enforcing`
            # preserves the reactivation counter. The state machine still
            # returns OK (kid sees no enforcement), but if budget is still
            # exhausted when adult mode ends the cycle resumes with the
            # counter intact — the kid doesn't get a fresh
            # "reactivation #1 friendly voice" handed back every time.
            await self._apply(
                StateDecision(STATE_OK, None),
                exit_reason="adult_mode",
                prev_reason=prev_reason_for_emit,
            )
            return

        effective_used = used_seconds
        # v0.16.3 — track the BINDING constraint explicitly. Previously
        # `reason` was only set when fully exhausted, which left WARN
        # audit rows with reason=None and the warn voice substituting
        # daily remaining (e.g. "Noch 145 Minuten") when the actual
        # binding constraint was a group budget with much less left
        # (e.g. 5 min of movies). Live-reported 2026-05-29.
        binding_reason: str = "daily_limit"
        binding_remaining_s: int = budget_s - used_seconds

        # v0.19.1 — fold per-group extension grants into the group budgets so
        # a parent's "+30 min" lifts the BINDING GROUP budget, not just the
        # daily pool. Without this an extension was a no-op exactly when a
        # group sub-limit was the binding constraint: live-reported
        # 2026-06-19, the parent granted +90 min against a 30 min movies cap
        # and the nagging continued (warn → grace → "30 s left") until adult
        # mode was used. `budget_s` upstream already includes the full daily
        # extension; here we add each group's TAGGED extension to its own
        # budget. Anti-defeat: an extension is tagged to ONE group at grant
        # time (the binding/active group), so switching apps cannot move the
        # granted credit to a different group.
        gx = group_extensions_seconds or {}
        eff_group_budgets_seconds: dict[str, int] | None = None
        if group_budgets_seconds is not None:
            # IMPORTANT: only groups with a POSITIVE base budget receive the
            # extension. A budget of 0 means "unlimited" by convention (see
            # the `> 0` / `<= 0` guards below and in the coordinator). Adding
            # an extension to a 0-budget group would convert "unlimited" into
            # "limited to the extension amount" — turning a parent's "+time"
            # grant into a DENIAL of previously-unlimited screen time.
            # (Caught in adversarial review 2026-06-19; the active-group
            # auto-tagging in audit.resolve_extension_target_group can route
            # a grant to an unlimited group, so this guard is load-bearing.)
            eff_group_budgets_seconds = {
                g: (b + gx.get(g, 0) if b > 0 else b)
                for g, b in group_budgets_seconds.items()
            }
        if (
            current_group is not None
            and current_group_budget_seconds is not None
            and current_group_budget_seconds > 0
        ):
            current_group_budget_seconds = (
                current_group_budget_seconds + gx.get(current_group, 0)
            )

        # v0.17.0 F-D (Opus BA audit P1 — pre-exhaustion defeat vector):
        # When the kid is in WARN/GRACE because of a group constraint
        # and closes the app for one tick, `current_group` clears. The
        # daily binding has hours left → state would relax to OK → the
        # next time the kid reopens the app the WARN/GRACE clock starts
        # FRESH (full warn_threshold + full grace_seconds), gaining
        # ~75 s of free time per close-reopen cycle.
        #
        # Latch: when the kid is currently NOT in any app but the
        # previous binding was a group AND that group is NOT yet fully
        # exhausted (v0.16.5 pin handles full exhaustion), promote
        # current_group to the latched group so the group-binding block
        # below picks it up. The latch "follows" the kid through brief
        # standby tics; it clears naturally when the kid changes to a
        # different group (current_group != None) or when midnight
        # rollover resets group_totals.
        if (
            current_group is None
            and self._state in (STATE_WARNING, STATE_GRACE)
            and self._enforce_reason
            and self._enforce_reason.startswith("group:")
            and group_totals_seconds is not None
            and group_budgets_seconds is not None
        ):
            latched_group = self._enforce_reason[len("group:"):]
            latched_used = group_totals_seconds.get(latched_group, 0)
            # v0.19.1 — read the EXTENSION-ADJUSTED budget so a latched group
            # the parent just extended isn't mis-treated as exhausted.
            latched_budget = (eff_group_budgets_seconds or {}).get(latched_group)
            if (
                latched_budget is not None
                and latched_budget > 0
                # Skip when group is fully exhausted — v0.16.5 pin
                # below handles that case and is the stronger signal.
                and latched_used < latched_budget
            ):
                current_group = latched_group
                current_group_used_seconds = latched_used
                current_group_budget_seconds = latched_budget

        # Per-group constraint — if the kid is currently using an app in
        # a group with its own budget, also evaluate that.
        if (
            current_group is not None
            and current_group_budget_seconds is not None
            and current_group_budget_seconds > 0
        ):
            overall_remaining = budget_s - effective_used
            group_remaining = current_group_budget_seconds - current_group_used_seconds
            if group_remaining < overall_remaining:
                # The group is the binding constraint. Translate to the
                # state machine's vocabulary by scaling the group budget
                # against effective_used.
                effective_used = max(
                    effective_used, budget_s - group_remaining
                )
                binding_reason = f"group:{current_group}"
                binding_remaining_s = group_remaining

        # v0.16.5 — group-exhaustion pin. When any group's daily total is
        # at-or-over its budget, keep enforcement pinned during periods
        # where the kid is NOT actively inside a different non-exhausted
        # budgeted group. Without this, when the kid stops watching after
        # exhausting (current_group → None) the daily binding — which
        # usually has hours left — relaxes state back to OK, _exit_enforcing
        # fires, `_reactivation_count` resets to 0. The kid then restarts
        # the Apple TV and reopens the app: state cycles through a fresh
        # WARN → GRACE → ENFORCING, and the `watch_reactivation` edge
        # detector never sees inactive→active WHILE state=ENFORCING, so
        # the reactivation voices never fire and the parent push never
        # lands. Live-reported 2026-05-30 (Living Room, movies group,
        # Netflix). The "switch groups to gain time" path stays intact:
        # if the kid moves to a different group that still has budget,
        # we DON'T pin (its own binding takes over and state can relax).
        if group_totals_seconds and group_budgets_seconds:
            in_safe_group = (
                current_group is not None
                and current_group_budget_seconds is not None
                and current_group_budget_seconds > 0
                and current_group_used_seconds < current_group_budget_seconds
            )
            if not in_safe_group:
                # v0.21.3 (FIX) — evaluate the CURRENT group first.
                # `group_totals_seconds` is an unordered dict, so the plain
                # scan below used to pin whichever exhausted group happened
                # to be iterated first. When the kid sat in an exhausted
                # current_group while a sibling was ALSO exhausted, the
                # binding reason could name the sibling — e.g. "group:other"
                # reported while the kid was watching an exhausted
                # "linear_tv". That misleading reason then suppressed the
                # native-TV kill in `_native_tv_should_enforce()`, leaving
                # Live TV running past its cap indefinitely (live-reported
                # 2026-08-17: linear_tv 158min/30, other 204min/60, reason
                # "group:other", TV never powered off). Ordering by
                # "is not the current group" is a stable, total key, so the
                # current group wins whenever it is itself exhausted.
                for g_name, g_used in sorted(
                    group_totals_seconds.items(),
                    key=lambda kv: kv[0] != current_group,
                ):
                    # v0.19.1 — compare against the extension-adjusted budget
                    # so a group the parent extended isn't pinned as exhausted.
                    g_budget = (eff_group_budgets_seconds or {}).get(g_name)
                    if g_budget is None or g_budget <= 0:
                        continue
                    if g_used >= g_budget:
                        # This group is exhausted today → pin enforcement.
                        effective_used = max(effective_used, budget_s)
                        binding_reason = f"group:{g_name}"
                        binding_remaining_s = 0
                        break

        # v0.21.3 — snapshot whether native TV's OWN cap is spent, while the
        # group data is still in scope. `_native_tv_should_enforce()` reads
        # this instead of trusting the single binding-reason string.
        _nat_budget = (eff_group_budgets_seconds or {}).get(GROUP_LINEAR_TV)
        _nat_used = (group_totals_seconds or {}).get(GROUP_LINEAR_TV, 0)
        self._native_group_exhausted = bool(
            _nat_budget is not None
            and _nat_budget > 0
            and _nat_used >= _nat_budget
        )

        # Quiet windows force enforcement regardless of any budget.
        active_window = find_active_window(self._quiet_windows, dt_util.as_local(now))
        if active_window is not None:
            effective_used = max(effective_used, budget_s)
            self._active_quiet_label = active_window.label or active_window.format()
            binding_reason = f"quiet:{self._active_quiet_label}"
            binding_remaining_s = 0
        else:
            self._active_quiet_label = None

        # v0.16.3 — expose the binding-constraint remaining unconditionally
        # so the audit voice-substitution layer can always read it. The
        # state machine still receives `effective_used` (scaled) so its
        # transition logic is unchanged.
        self._effective_remaining_s = binding_remaining_s

        # v0.16.3 — surface the binding reason from WARN onwards (was only
        # set at exhaustion). Below WARN we stay None so OK ticks don't
        # carry a misleading reason on the EVENT_ENFORCEMENT_CHANGED bus.
        if binding_remaining_s <= warn_s:
            self._enforce_reason = binding_reason
        else:
            self._enforce_reason = None

        decision = compute_next_state(
            current_state=self._state,
            used_seconds=effective_used,
            budget_seconds=budget_s,
            warn_threshold_seconds=warn_s,
            grace_seconds=self._profile.grace_seconds,
            grace_started_at=self._grace_started_at,
            now=now,
        )
        await self._apply(decision, prev_reason=prev_reason_for_emit)

        # v0.16.0 — countdown voice. After the state decision is applied,
        # check whether we're in the final 30s wind-down (WARNING with
        # very little remaining, OR GRACE) and fire the static cue once.
        # v0.16.1 NOTE: this evaluate-tick check is now disabled. The
        # countdown is scheduled by `_schedule_countdown_for_grace()`
        # which runs ONCE on the state transition into GRACE (in
        # `_apply`), and fires a one-shot HA timer at the exact moment
        # (grace_seconds - 30s) afterwards. Tick-based detection missed
        # the window on group-budget transitions (state went straight
        # OK→GRACE→ENFORCING with no time in WARNING, no tick fell in
        # the 35s remaining_seconds window). The legacy code below is
        # kept as a no-op stub so the call sites in tests still resolve;
        # the actual work happens in the scheduler.
        # (legacy tick-based call retained for the test fixtures that
        # still drive `_maybe_fire_countdown` directly with mocked
        # remaining_s values; in production the method is a no-op.)

    def seed_from_runtime_state(self) -> None:
        """v0.17.0 F-E — restore the persisted enforce-cycle state.

        Called by the coordinator at startup BEFORE `seed_from_adguard`.
        Takes precedence over the AdGuard-derived guess (which was the
        only state-recovery path pre-v0.17.0; broken for the
        `enable_adguard_block=False` default since v0.15.5).

        Sanity check (adversarial-required): if the persisted
        `grace_started_at` is older than 2× `grace_seconds`, the GRACE
        window is unrecoverable — log a warning and drop straight to
        ENFORCING (preserves the "kid was past their budget when we
        restarted" intent without trying to compute a negative grace
        delta against now).
        """
        if self._store is None:
            return
        runtime = self._store.get_runtime_state(self._profile.id)
        if runtime is None:
            return  # fresh boot or no prior persistence — use constructor defaults

        self._state = runtime.state
        self._grace_started_at = runtime.grace_started_at
        self._enforce_reason = runtime.enforce_reason
        self._reactivation_count = int(runtime.reactivation_count)
        self._was_apple_tv_active_last_tick = bool(
            runtime.was_apple_tv_active_last_tick
        )
        self._last_enforcement_failed = bool(runtime.last_enforcement_failed)

        # Sanity check on GRACE: if the window has been "running" for
        # more than 2× grace_seconds, the restart spanned an interval
        # where ENFORCING should already have fired. Don't try to
        # resume a phantom grace — push straight to ENFORCING.
        if (
            self._state == STATE_GRACE
            and self._grace_started_at is not None
        ):
            now = dt_util.utcnow()
            age = (now - self._grace_started_at).total_seconds()
            stale_threshold = 2 * int(self._profile.grace_seconds)
            if age >= stale_threshold:
                _LOGGER.warning(
                    "v0.17.0 F-E — restored GRACE for %s is %ds old (≥ %ds "
                    "= 2×grace_seconds). Treating as expired and dropping "
                    "to ENFORCING.",
                    self._profile.id, int(age), stale_threshold,
                )
                self._state = STATE_ENFORCING
                # Leave _grace_started_at as-is so the audit trail still
                # shows when the cycle started; _apply will treat it as
                # historical.

        _LOGGER.info(
            "v0.17.0 F-E — restored runtime state for %s: state=%s "
            "reason=%r grace_started_at=%s reactivation_count=%d",
            self._profile.id, self._state, self._enforce_reason,
            self._grace_started_at, self._reactivation_count,
        )

    def _persist_runtime_state(self) -> None:
        """v0.17.0 F-E — snapshot the current enforce-cycle state to the
        store. Called after every transition in `_apply` and after every
        reactivation increment / counter reset. The actual disk write
        happens on the next `async_save()` (debounced by HA).
        """
        if self._store is None:
            return
        from .storage import RuntimeState
        self._store.set_runtime_state(
            self._profile.id,
            RuntimeState(
                state=self._state,
                grace_started_at=self._grace_started_at,
                enforce_reason=self._enforce_reason,
                reactivation_count=self._reactivation_count,
                was_apple_tv_active_last_tick=self._was_apple_tv_active_last_tick,
                last_enforcement_failed=self._last_enforcement_failed,
                updated_at=dt_util.utcnow(),
            ),
        )

    async def seed_from_adguard(self) -> None:
        """Read the live AdGuard block state and seed `_is_blocked`.

        Called once at coordinator startup so that an HA restart while
        ENFORCING doesn't leave the state machine thinking the kid is
        unblocked when AdGuard still has them blocked (or vice versa).

        Added in v0.10.0 (QA P0 #3).
        v0.15.5 — skip when AdGuard blocking is disabled (the supplementary
        DNS layer is opt-in; nothing to seed if we never call it).
        v0.17.0 — runs AFTER seed_from_runtime_state. The runtime state
        is the authoritative recovery source; AdGuard is now a secondary
        signal that only adjusts `_is_blocked` (the in-memory flag),
        never `_state`.
        """
        if not getattr(self._profile, "enable_adguard_block", False):
            _LOGGER.debug(
                "seed_from_adguard skipped — enable_adguard_block=False"
            )
            return
        try:
            client = await self._adguard.get_client(
                self._profile.adguard_client_name
            )
        except Exception as err:  # noqa: BLE001 -- seed must never crash setup_entry
            # AdGuard may be unreachable at HA startup (proxy addon still
            # booting, AdGuard restarting, DNS lookup transient). The
            # reassert tick will heal once AdGuard is reachable.
            _LOGGER.warning(
                "Cannot seed enforcer state from AdGuard (%s): %s — assuming unblocked, will reassert on next tick",
                self._profile.adguard_client_name,
                err,
            )
            return
        blocked_services = client.get("blocked_services") or []
        # AdGuard's representation: a non-empty blocked_services list with
        # use_global_blocked_services=False means we're enforcing.
        use_global = client.get("use_global_blocked_services", True)
        self._is_blocked = bool(blocked_services) and not use_global
        if self._is_blocked:
            # If AdGuard says blocked but we don't know why, assume
            # daily_limit — the state machine will correct on the first
            # evaluate() with real usage numbers.
            #
            # v0.17.0 F-E — only overwrite `_state` / `_enforce_reason`
            # when seed_from_runtime_state didn't already restore a
            # non-OK state. The runtime store is the authoritative
            # recovery source; AdGuard's flag was the only path
            # pre-v0.17.0 (broken for AdGuard-disabled deployments)
            # and now serves as a secondary check.
            if self._state == STATE_OK:
                self._state = STATE_ENFORCING
                self._enforce_reason = "daily_limit"
            _LOGGER.info(
                "Seeded enforcer state from AdGuard: ENFORCING (client %s is blocked)",
                self._profile.adguard_client_name,
            )

    async def reassert(self) -> None:
        """Idempotent: re-apply the current desired state to AdGuard +
        re-attempt Apple TV turn_off if it's still active under ENFORCING.

        Called periodically by the coordinator (every tick). Two jobs:

        1. **AdGuard drift heal** (v0.10.0): if state machine says
           ENFORCING but AdGuard isn't blocked (or vice-versa), re-apply.

        2. **Apple TV watchdog** (v0.15.4): if we're in ENFORCING AND
           the Apple TV entity is in an ACTIVE state (playing / on / etc.
           — anything in ACTIVE_MEDIA_STATES), retry just the pyatv
           `turn_off`. This catches the chronic v0.14.x pattern where
           pyatv's Companion protocol drops silently mid-call → Apple TV
           stays on → kid could resume watching after Samsung TV comes
           back up. We do NOT re-fire the full `_enter_enforcing` chain
           (would cause audit-row spam, voice spam, and re-block AdGuard
           pointlessly — that's the v0.15.2 fix). Only the pyatv hammer.
           When Apple TV is idle/off, watchdog is a no-op.
        """
        async with self._lock:
            want_blocked = self._state == STATE_ENFORCING

            # v0.17.0 F-F (Sonnet BA audit P1): compute the policy
            # decision ONCE at the top and gate BOTH jobs on it. Pre-
            # v0.17.0 only Job 2 (watchdog) had a `decision.kind != "ACT"`
            # bail; Job 1 (drift heal) would still call _enter_enforcing
            # every reassert tick in monitor mode, producing "drift
            # detected" log spam + spurious enforce attempts. The repro
            # is narrow (monitor + enable_adguard_block=True; v0.15.7
            # made _is_blocked=True for AdGuard-disabled enforced, so
            # that combo doesn't drift) but real for anyone running an
            # external AdGuard while calibrating in monitor mode.
            decision = self._should_act_now()
            if decision.kind != "ACT":
                return  # don't fight a bypass — neither AdGuard nor pyatv

            # Job 1: AdGuard drift
            if want_blocked != self._is_blocked:
                _LOGGER.warning(
                    "Drift detected: state=%s but AdGuard is_blocked=%s — re-applying",
                    self._state, self._is_blocked,
                )
                if want_blocked:
                    await self._enter_enforcing()
                else:
                    await self._exit_enforcing()
                return  # _enter_enforcing already handled pyatv turn_off

            # Job 2: pyatv watchdog (only when in ENFORCING and the kid
            # is ACTUALLY WATCHING SOMETHING on the Apple TV).
            if not want_blocked:
                return  # not enforcing, nothing to watchdog

            # Job 3 (v0.20.0): secondary-switch drift heal — the anti-defeat
            # watchdog for folded-in devices. `_is_blocked` only tracks the
            # AdGuard/state flag, so a kid flipping a secondary's FRITZ switch
            # back ON mid-block produces NO drift for Job 1 to catch. Re-assert
            # any secondary switch that reads "on" while we're ENFORCING. Runs
            # BEFORE Job 2's Apple-TV-specific early returns so it fires even
            # when the Apple TV is off (the common Xbox-only case). No-op for
            # single-device profiles. Mirrors the AdGuard drift-heal for the
            # primary so the Xbox can't be un-blocked by toggling the switch.
            for dev in (getattr(self._profile, "secondary_devices", None) or []):
                sw = (dev.get("enforcement_switch_entity_id") or "").strip()
                if not sw:
                    continue
                sw_state = self._hass.states.get(sw)
                if sw_state is not None and sw_state.state == "on":
                    _LOGGER.warning(
                        "v0.20.0 secondary drift — %s switch %s is ON under "
                        "ENFORCING; re-asserting OFF",
                        dev.get("entity_id"), sw,
                    )
                    try:
                        await self._hass.services.async_call(
                            "switch", "turn_off", {"entity_id": sw}, blocking=True
                        )
                    except Exception as err:  # noqa: BLE001 — non-fatal
                        _LOGGER.error(
                            "secondary switch re-assert turn_off(%s) failed: %s",
                            sw, err,
                        )

            # Job 4 (v0.21.0): native-TV anti-defeat. While ENFORCING, if the
            # kid turns the TV back on with a native (non-excluded) source, turn
            # it off again. Runs BEFORE the pyatv-specific early-return below so
            # it fires even when the Apple TV is off (the native-TV common case).
            # A source of HDMI1 (Apple TV) / HDMI2/DVI (Xbox) reads as NOT native
            # here, so this never fights a legitimately-tracked device. No-op
            # when track_native_tv is off.
            # v0.21.1 (FIX 1) — same reason gate as `_enter_enforcing`: when the
            # room enforces for a SIBLING group (e.g. "group:movies") while an
            # unlimited native TV is on, the watchdog must NOT re-kill the TV.
            if self._native_tv_should_enforce():
                tv_eid = (
                    getattr(self._profile, "tv_entity_id", None) or ""
                ).strip()
                _LOGGER.debug(
                    "v0.21.0 native-TV watchdog — TV %s back on with a native "
                    "source under ENFORCING; re-asserting off",
                    tv_eid,
                )
                await self._call_turn_off_with_verify(
                    tv_eid, label="Native TV (watchdog)"
                )

            # v0.19.0 — pyatv-specific. Xbox profiles have no pyatv path
            # to retry, and apple_tv_entity_id is a device_tracker rather
            # than a media_player — calling media_player.turn_off on it
            # would just fail. Skip the watchdog entirely.
            if getattr(self._profile, "device_kind", "apple_tv") != "apple_tv":
                return
            # v0.17.4 fix: only retry turn_off while the device is actively
            # consuming media (playing/paused/buffering) — NOT for `idle`/
            # `on` (home screen / screensaver / just powered on). The
            # previous check used INACTIVE_MEDIA_STATES which doesn't
            # include `idle`, so a kid who walked away with the TV on
            # the Apple TV home screen produced one ERROR log + one
            # `enforce_turn_off_failed` audit row every 60s indefinitely
            # (184 rows / 6h observed on live install once the v0.17.3
            # under-count fix made the budget actually bite). The intent
            # of the watchdog has always been "kid won't stop streaming";
            # idle home-screen navigation isn't streaming.
            from .media_attribution import ACTIVELY_PLAYING_STATES
            apple_state = self._hass.states.get(self._profile.apple_tv_entity_id)
            if apple_state is None:
                return
            if apple_state.state not in ACTIVELY_PLAYING_STATES:
                return  # kid isn't actively watching — watchdog quiet
            # Apple TV is still actively playing under ENFORCING — pyatv
            # probably dropped. Retry the single pyatv path (don't touch
            # Samsung again, already done in `_enter_enforcing`).
            _LOGGER.debug(
                "v0.15.4 watchdog — Apple TV still %s under ENFORCING, "
                "retrying pyatv turn_off",
                apple_state.state,
            )
            await self._call_turn_off_with_verify(
                self._profile.apple_tv_entity_id, label="Apple TV (watchdog)"
            )

            # Note: we don't need to re-check the decision after the
            # turn_off retry — the lock has been held throughout and the
            # gate is consistent.

    async def watch_reactivation(self) -> None:
        """v0.16.0 — detect kid re-arming the Apple TV under ENFORCING.

        Called from the coordinator every tick (alongside `reassert`).
        Edge-detects inactive→active on the Apple TV entity. The first
        re-on in a cycle gets the friendly voice; the 2nd+ gets the
        stern voice AND a parent push notification.

        Why this matters (the owner's live test, 2026-05-28): kid hit budget,
        enforce_start fired, both TVs went off for ~70s. Kid physically
        turned Samsung back on + restarted the Apple TV → watching
        resumed. The integration kept retrying pyatv (the v0.15.4
        watchdog) but had no social-pressure intervention. This closes
        that gap.

        Counter state (`_reactivation_count`,
        `_was_apple_tv_active_last_tick`) lives on the controller and
        resets in `_exit_enforcing` so each ENFORCING cycle starts at
        "first re-on" for the friendly tone.

        v0.16.2 — acquires `self._lock` to serialize with `_enter_enforcing`
        + `_exit_enforcing`. BA audit 2026-05-28 (Sonnet + Opus convergent)
        identified a race: the coordinator's tick fires this method
        WITHOUT the lock, so it could read `_was_apple_tv_active_last_tick`
        while `_enter_enforcing` was still in its first await (AdGuard call
        ~9s window), before the seed line executed → phantom "reactivation #1"
        at enforce_start. Confirmed by audit row at 10:52:32.540 in the
        live test. The lock serialization closes the window.
        """
        async with self._lock:
            await self._watch_reactivation_locked()

    async def _watch_reactivation_locked(self) -> None:
        """Body of watch_reactivation, MUST be called with self._lock held."""
        # Only watch under ENFORCING — if we're back in OK/WARNING/GRACE
        # the kid is allowed to be watching. Reset the edge-detector
        # input so we don't carry a stale True across cycles.
        if self._state != STATE_ENFORCING:
            self._was_apple_tv_active_last_tick = False
            return

        # v0.17.0 F-C (Opus BA audit P1): under BYPASS (adult mode /
        # paused) the integration is supposed to be muted — no audit
        # rows, no voice, no push. Under OBSERVE (monitor_only) we
        # STILL detect edges + record the audit row so a parent
        # calibrating thresholds in monitor mode can see "the kid
        # would have tried to defeat the block #N times" on the
        # dashboard. Voice + parent push remain gated to ACT in
        # `_handle_reactivation` so monitor mode stays audibly silent
        # for the kid. Pre-v0.17.0 this was a bare `decision.kind !=
        # "ACT"` early-return which silenced the entire pipeline,
        # including the audit row.
        decision = self._should_act_now()
        if decision.kind == "BYPASS":
            self._was_apple_tv_active_last_tick = False
            return

        # Lazy import to mirror the watchdog pattern (avoid circular).
        from .media_attribution import INACTIVE_MEDIA_STATES
        apple_state = self._hass.states.get(self._profile.apple_tv_entity_id)
        if apple_state is None or apple_state.state in INACTIVE_MEDIA_STATES:
            # Either we can't read the state OR Apple TV is inactive —
            # update the edge-detector flag and bail. Counts the
            # "missing" tick as inactive so the next active tick is a
            # genuine transition.
            self._was_apple_tv_active_last_tick = False
            return

        # Apple TV is in an active state. If last tick was also active
        # there's no transition — nothing to do.
        if self._was_apple_tv_active_last_tick:
            return

        # Edge: inactive → active detected.
        self._reactivation_count += 1
        _LOGGER.warning(
            "v0.16.0 re-on detected on %s (cycle count #%d) — kid "
            "appears to have restarted the Apple TV under ENFORCING",
            self._profile.apple_tv_entity_id, self._reactivation_count,
        )
        self._was_apple_tv_active_last_tick = True
        # v0.17.0 F-E — persist the counter increment so HA restarts
        # don't hand the kid a free "reactivation #1" by losing the
        # counter mid-cycle.
        self._persist_runtime_state()
        await self._handle_reactivation(decision)

    async def _handle_reactivation(self, decision=None) -> None:
        """Fire the friendly/stern voice + (for 2nd+) the parent push.

        Audit row is recorded unconditionally — the parent should be
        able to count attempts on the dashboard even if voice/push are
        silent (e.g. notify_media_player_entity_id not set, or
        monitor_only mode).

        v0.17.0 F-C — `decision` is the policy decision from
        `_watch_reactivation_locked`. Under OBSERVE (monitor_only) the
        audit row still records but voice + parent push are skipped
        so the kid can't hear the heads-up during calibration mode.
        Default `None` keeps the legacy call sites (tests calling
        `_handle_reactivation` directly) working — they're assumed ACT.
        """
        # Treat missing decision as ACT for back-compat with direct
        # test calls.
        kind = decision.kind if decision is not None else "ACT"
        count = self._reactivation_count
        friendly = getattr(self._profile, "reactivation_message_friendly", "") or ""
        stern = getattr(self._profile, "reactivation_message_stern", "") or ""

        # Lazy import to mirror _maybe_announce_enforce.
        from .voice_notifier import should_speak, speak as _speak
        from .audit import record_admin_action

        # Voice + parent push are ACT-only. Under OBSERVE we still
        # record the count + audit row below.
        speak_voice = kind == "ACT"

        # Choose the message: 1st = friendly, 2nd+ = stern. Both opt-in
        # (empty = silent), so the empty-string default is preserved.
        spoke = False
        if speak_voice and count == 1 and friendly and should_speak(self._profile, friendly):
            result = await _speak(self._hass, self._profile, template=friendly)
            spoke = result.get("status") == "spoken"
            if spoke:
                try:
                    record_admin_action(
                        self._hass,
                        profile_id=self._profile.id,
                        action="voice_announcement",
                        reason="reactivation_friendly",
                        detail=result.get("message"),
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("reactivation audit (voice) failed (non-fatal)")
        elif speak_voice and count >= 2 and stern and should_speak(self._profile, stern):
            result = await _speak(self._hass, self._profile, template=stern)
            spoke = result.get("status") == "spoken"
            if spoke:
                try:
                    record_admin_action(
                        self._hass,
                        profile_id=self._profile.id,
                        action="voice_announcement",
                        reason="reactivation_stern",
                        detail=result.get("message"),
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("reactivation audit (voice) failed (non-fatal)")

        # Always record the reactivation event itself (separate from the
        # voice audit) so the dashboard can tally attempts even when the
        # voice was silenced by missing config.
        try:
            record_admin_action(
                self._hass,
                profile_id=self._profile.id,
                action="reactivation",
                reason=None,
                detail=f"#{count}",
                actor="system",
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("reactivation audit (event) failed (non-fatal)")

        # v0.16.0 — 2nd+ re-on triggers the parent push (Phase D
        # delivery; the call site stays here so the count threshold
        # lives in one place).
        # v0.17.0 F-C — only push under ACT. In monitor_only the parent
        # has explicitly asked to calibrate without interventions; the
        # audit row above is the calibration signal.
        if count >= 2 and kind == "ACT":
            await self._notify_parent()

    async def _notify_parent(self) -> None:
        """v0.16.0 — send a push notification to the parent.

        Fires the HA `notify.<target>` service with a short message
        describing the defeat attempt. Target service is per-profile
        (`notify_parent_target`); empty falls back to `notify.notify`
        which fans out to all configured notification services.

        Wrapped in try/except — a missing notification service must
        NOT crash the reactivation handler (would block subsequent
        re-on events from being detected this cycle).

        Audit: action=parent_notified, detail=`#N via notify.<target>`,
        actor=system. Recorded BEFORE the await on the service call so
        the row lands even if notify raises.
        """
        target = (getattr(self._profile, "notify_parent_target", "") or "").strip()
        service = target or "notify"
        count = self._reactivation_count

        # Lazy import — keeps the enforcer's module-load surface tight.
        from .audit import record_admin_action

        message = (
            f"Kid restarted Apple TV {count}x during ENFORCING. "
            f"Currently {self._profile.apple_tv_entity_id} is active."
        )
        title = f"{self._profile.display_name}: TV defeat"
        try:
            await self._hass.services.async_call(
                "notify",
                service,
                {"title": title, "message": message},
                blocking=False,
            )
            spoken = True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Parent notification via notify.%s failed (non-fatal): %s",
                service, err,
            )
            spoken = False

        try:
            record_admin_action(
                self._hass,
                profile_id=self._profile.id,
                action="parent_notified",
                reason=None,
                detail=(
                    f"#{count} via notify.{service}"
                    + ("" if spoken else " (failed)")
                ),
                actor="system",
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("parent_notified audit row failed (non-fatal)")

    def _should_act_now(self):
        """Compute the current policy decision off live state.
        Helper used by the watchdog so it doesn't punch through adult_mode
        or a manual mode=paused/monitor_only override that was set mid-cycle.
        """
        from .policy import should_act
        until = (
            self._store.adult_mode_until(self._profile.id)
            if self._store is not None
            else None
        )
        mode = getattr(
            self._profile,
            "mode",
            "enforced" if getattr(self._profile, "enforcement_enabled", True) else "monitor_only",
        )
        return should_act(mode=mode, adult_mode_until=until, now=dt_util.utcnow())

    async def force_block(self) -> None:
        """Service-call entrypoint — block immediately, bypassing the state machine.

        v0.17.0 F-N (Sonnet BA audit P2, the owner Q2 decision: silent no-op
        + warning log): when current policy is BYPASS (adult mode or
        paused), force_block must NOT silently leave `_state =
        STATE_ENFORCING` + `_is_blocked = False`. That false state then
        causes the reassert watchdog to spam "drift detected" every 60s
        until the next coordinator tick re-evaluates and clears it.

        Pre-v0.17.0 behaviour: state was set, `_enter_enforcing` was
        called, the adult-mode hard guard at the top of `_enter_enforcing`
        prevented actual blocking but `_state` already read ENFORCING.

        Post-v0.17.0: log a warning + return without touching state.
        The caller's expectation ("kid is now blocked") is violated, but
        loudly (in the HA log) rather than silently (in the broken
        drift-detected loop). API.md documents this behaviour.
        """
        decision = self._should_act_now()
        if decision.kind != "ACT":
            _LOGGER.warning(
                "force_block service called but policy decision is %s "
                "(reason=%s) — refusing to alter state. The parent has "
                "explicitly bypassed enforcement (adult mode / paused). "
                "Disable that override first, then call force_block.",
                decision.kind, decision.reason,
            )
            return
        async with self._lock:
            prev = self._state
            self._state = STATE_ENFORCING
            self._enforce_reason = self._enforce_reason or "force_block"
            self._grace_started_at = None
            await self._enter_enforcing()
            self._emit_state_event(prev_state=prev)

    async def unblock(self) -> None:
        """Service-call entrypoint — clear enforcement and reset state."""
        async with self._lock:
            prev = self._state
            await self._exit_enforcing()
            self._state = STATE_OK
            self._enforce_reason = None
            self._grace_started_at = None
            self._emit_state_event(prev_state=prev)

    # ----- internals -----

    async def _apply(
        self,
        decision: StateDecision,
        *,
        exit_reason: str | None = None,
        prev_reason: str | None = None,
    ) -> None:
        """Apply a state decision; fire side-effect entry/exit hooks.

        v0.17.0 — `exit_reason` is passed through to `_exit_enforcing`
        so the caller (e.g. the adult-mode branch in `evaluate()`) can
        signal a TEMPORARY exit. Adult-mode exits preserve the
        reactivation counter so the kid can't earn a free "reactivation
        #1" by waiting for adult-mode to end.

        v0.17.0 F-I — `prev_reason` carries the binding reason from
        BEFORE this tick's `evaluate()` mutation. When the caller
        (evaluate) hasn't already overwritten `_enforce_reason`,
        falling back to the live value is fine; but `evaluate()`
        always writes `_enforce_reason` before calling `_apply()`,
        so `prev_reason` is what the EVENT_ENFORCEMENT_CHANGED payload
        needs to carry the original attribution on relax transitions
        (e.g. ENFORCING→OK keeping the `quiet:Bedtime` label, or
        WARN→OK keeping the `group:movies` label for the F-L
        `warn_cleared` audit row).
        """
        async with self._lock:
            prev = self._state
            self._state = decision.state
            self._grace_started_at = decision.grace_started_at

            # v0.15.2 — only fire _enter_enforcing on the TRANSITION INTO
            # ENFORCING, not every tick we stay enforcing. The coordinator
            # already calls reassert() on each tick for AdGuard drift
            # (coordinator.py:200), so the "are we blocked?" half is
            # handled. Re-firing _enter_enforcing every 30s when the
            # budget stays at 0 was producing one enforce_turn_off_failed
            # audit row per tick + (pre-v0.15.1) one voice_announcement
            # lie per tick. Live-observed 2026-05-25.
            if decision.state == STATE_ENFORCING and prev != STATE_ENFORCING:
                await self._enter_enforcing()
            elif prev == STATE_ENFORCING and decision.state != STATE_ENFORCING:
                await self._exit_enforcing(reason=exit_reason)

            # v0.16.1 — schedule the countdown voice on entry into GRACE.
            # GRACE is the universal pre-enforce state regardless of
            # which budget triggered (overall / group / quiet window).
            # Schedule once; cancel on any exit from GRACE so a sudden
            # budget bump (PATCH /limits) or adult mode toggle silences
            # the upcoming voice cue.
            if decision.state == STATE_GRACE and prev != STATE_GRACE:
                self._schedule_countdown_for_grace()
            elif prev == STATE_GRACE and decision.state != STATE_GRACE:
                self._cancel_countdown()

            # v0.16.0 legacy — reset latch on return to OK (kept for
            # back-compat with the v0.16.0 test suite; v0.16.1 production
            # logic uses _countdown_handle instead).
            if decision.state == STATE_OK and prev != STATE_OK:
                self._countdown_fired = False

            if prev != decision.state:
                self._emit_state_event(prev_state=prev, prev_reason=prev_reason)
                # v0.17.0 F-E — snapshot the runtime state on every
                # state transition. async_save() debounces the disk
                # write so the cost is one batched write per transition
                # batch (typical: 1 transition per coordinator tick).
                self._persist_runtime_state()

    def _native_tv_active(self) -> bool:
        """v0.21.0 — is the configured TV currently showing *native* content
        (on, with a non-excluded source) under this profile's track_native_tv?

        Live-state mirror of the coordinator's native-TV detection so the
        enforce path AND the reassert anti-defeat watchdog agree on one rule.
        Source-based: the excluded-source list (HDMI1=Apple TV, HDMI2/DVI=Xbox)
        is what separates native TV from the tracked devices, so an Apple-TV /
        Xbox input never reads as native here. Reads `tv_entity_id` — the same
        field the tv-on accumulator uses — NOT `tv_shutdown_target`.
        """
        from .media_attribution import native_tv_is_active
        if not getattr(self._profile, "track_native_tv", False):
            return False
        tv_eid = (getattr(self._profile, "tv_entity_id", None) or "").strip()
        if not tv_eid:
            return False
        st = self._hass.states.get(tv_eid)
        tv_state = st.state if st is not None else None
        source = st.attributes.get("source") if st is not None else None
        return native_tv_is_active(
            track_native_tv=True,
            tv_configured=True,
            tv_state=tv_state,
            source=source,
            excluded_sources=getattr(
                self._profile, "native_tv_excluded_sources", None
            ),
        )

    def _native_tv_should_enforce(self) -> bool:
        """v0.21.1 (FIX 1) — should the native-TV kill fire *given why the room
        is enforcing*?

        `_native_tv_active()` is the pure live-state mirror (display on + a
        non-excluded source); it says nothing about WHY the room is currently
        under ENFORCING. Native TV is constrained ONLY by room-wide limits
        (daily budget / quiet window / sleep — the non-"group:" reasons) or by
        its OWN category cap ("group:linear_tv"). When the room is enforcing
        because a SIBLING group is exhausted (e.g. reason "group:movies"), an
        unlimited native TV must keep tracking, NOT get powered off — otherwise
        a movies-exhausted budget would shut off a parent watching the tuner.

        Gate: native is active AND the binding reason is not a *sibling* group.
          - reason None / "daily_limit" / "quiet:*" / "sleep:*"  → enforce.
          - reason "group:linear_tv" (native's own cap)          → enforce.
          - reason "group:<other>" (movies / gaming / …)         → DO NOT.

        v0.21.3 (FIX) — the sibling-group exemption is now conditional on
        native TV still HAVING budget. The original gate trusted the reason
        string alone, so when linear_tv was exhausted at the same time as a
        sibling, and the binding picker named the sibling, Live TV was spared
        despite being over its own cap. Room-wide reasons and an exhausted
        linear_tv now both enforce; only a *genuinely unrelated* sibling
        exhaustion (with Live TV still in credit) spares the TV.
        """
        if not self._native_tv_active():
            return False
        reason = self._enforce_reason or ""
        is_sibling_group = (
            reason.startswith("group:")
            and reason != f"group:{GROUP_LINEAR_TV}"
        )
        if is_sibling_group and not self._native_group_exhausted:
            # Unrelated category ran out; unlimited/in-credit Live TV keeps
            # tracking rather than being blacked out.
            return False
        return True

    async def _enter_enforcing_native_tv(self) -> None:
        """v0.21.0 — enforce native TV by turning the TV off unconditionally.

        AdGuard + Apple-TV-sleep + secondary switches are all irrelevant when
        the room activity is native TV (nothing tracked is on), so this path
        does ONE thing: media_player.turn_off on the configured TV entity,
        verified. The tv_shutdown switch is deliberately bypassed — for native
        TV the display is the device, so it's always a valid kill target.
        """
        tv_eid = (getattr(self._profile, "tv_entity_id", None) or "").strip()
        _LOGGER.info(
            "v0.21.0 native-TV enforcement — media_player.turn_off(%s) "
            "(unconditional; tv_shutdown switch bypassed, AdGuard + Apple-TV "
            "sleep skipped)",
            tv_eid,
        )
        tv_off_ok = await self._call_turn_off_with_verify(
            tv_eid, label="Native TV"
        )
        # State flag: mark blocked so reassert's drift-heal (state=ENFORCING vs
        # _is_blocked) doesn't re-fire _enter_enforcing every tick. The native
        # anti-defeat watchdog in reassert handles the "kid turns TV back on"
        # case instead.
        self._is_blocked = True
        self._last_enforcement_failed = not tv_off_ok
        if tv_off_ok:
            await self._maybe_announce_enforce()
        else:
            _LOGGER.warning(
                "v0.21.0 native-TV turn_off did not verify — skipping enforce "
                "voice to avoid speaking a lie."
            )

    async def _enter_enforcing(self) -> None:
        # v0.16.1 — seed `_was_apple_tv_active_last_tick` to the CURRENT
        # Apple TV state, so the first watch_reactivation tick after
        # enforce_start sees no transition (correct: the kid was already
        # watching when we transitioned to ENFORCING — that's not a
        # defeat). Pre-v0.16.1 this was always initialized False at
        # construction time, so the first tick under ENFORCING would
        # see False→True and fire a phantom "reactivation #1" right at
        # enforce_start. Live-observed 2026-05-28.
        from .media_attribution import INACTIVE_MEDIA_STATES
        apple_state = self._hass.states.get(self._profile.apple_tv_entity_id)
        if apple_state is not None:
            self._was_apple_tv_active_last_tick = (
                apple_state.state not in INACTIVE_MEDIA_STATES
            )
        else:
            self._was_apple_tv_active_last_tick = False

        # v0.14.3 — HARD GUARD: when adult mode is active, never touch
        # AdGuard or sleep any media_player. Defensive against ANY path
        # that calls into _enter_enforcing (the normal state machine
        # already bypasses to OK when adult mode is active, but
        # force_block + reassert + a future bug could all land here).
        # The audit-log entry for enforce_start still gets recorded
        # via the event listener; we just suppress the side effects.
        if self._store is not None and self._store.is_adult_mode_active(
            self._profile.id
        ):
            _LOGGER.info(
                "ADULT MODE ACTIVE — skipping all enforcement side effects "
                "(would have: AdGuard block + media_player.turn_off on %s). "
                "State machine still records the attempt; the audit row "
                "is suffixed with '(adult mode active)'.",
                self._profile.apple_tv_entity_id,
            )
            self._is_blocked = False
            # Bypass is intentional, not a failure.
            self._last_enforcement_failed = False
            return

        # v0.14.0 — monitor mode. When enforcement_enabled is False the
        # state machine still computes the transition + the audit log
        # still records it + voice announcements still fire, but no
        # AdGuard call and no Apple TV sleep. Useful for setup phase
        # ("observe before committing limits") and for selective
        # "let it slide tonight" without changing the budget.
        if not getattr(self._profile, "enforcement_enabled", True):
            _LOGGER.info(
                "MONITOR MODE — would have blocked AdGuard client %r + "
                "slept media_player %s (reason: %s)",
                self._profile.adguard_client_name,
                self._profile.apple_tv_entity_id,
                self._enforce_reason,
            )
            # Reflect what AdGuard actually is (still unblocked).
            self._is_blocked = False
            # Monitor mode is intentional bypass, not a failure.
            self._last_enforcement_failed = False
            return

        # v0.21.0 — native TV enforcement. When the room is watching native TV
        # (the TV is on with a non-excluded source — tuner / SCART / smart-TV
        # app), the TV IS the device: turn it OFF unconditionally. This is NOT
        # gated on the tv_shutdown switch (that switch means "ALSO kill the TV
        # when enforcing the Apple TV"; here the TV is the primary target).
        # AdGuard + Apple-TV-sleep are irrelevant for native TV and skipped.
        # v0.21.1 (FIX 1) — gate on `_native_tv_should_enforce()` (not the raw
        # `_native_tv_active()`): a sibling group's exhaustion (reason
        # "group:movies") must NOT power off an unlimited native TV. When the
        # gate is False but native is active, control falls through to the
        # normal enforcement path below (AdGuard / Apple-TV sleep on the idle
        # Apple TV — harmless), leaving the TV on.
        if self._native_tv_should_enforce():
            await self._enter_enforcing_native_tv()
            return

        # v0.15.5 — AdGuard blocking is now opt-in (default False).
        # When disabled, the call is skipped entirely AND `adguard_ok` is
        # treated as True for the failure-flag math below (we don't count
        # "we chose not to call AdGuard" as a failure). The Apple TV +
        # Samsung TV turn_off paths are the user-visible enforcement;
        # AdGuard is purely supplementary DNS hygiene.
        #
        # v0.14.2 — also catch generic transport errors (ClientConnectorError
        # etc.) that the AdGuard client may surface as raw aiohttp exceptions
        # when the proxy addon is unreachable. Without this, the whole
        # service call 500s and the user sees a generic HA error.
        # v0.19.0 — device_kind dispatch for the network-block primitive.
        # apple_tv profiles → AdGuard block (v0.15.x path, unchanged).
        # xbox_presence profiles → switch.turn_off on enforcement_switch_entity_id
        # (typically switch.xboxone_internet_access from the FRITZ!Box
        # integration). Same _is_blocked + adguard_ok semantics so the
        # downstream failure-flag math is identical for both kinds.
        device_kind = getattr(self._profile, "device_kind", "apple_tv")
        if device_kind == "xbox_presence":
            switch_eid = (
                getattr(self._profile, "enforcement_switch_entity_id", None) or ""
            ).strip() if getattr(
                self._profile, "enforcement_switch_entity_id", None
            ) else ""
            if switch_eid:
                adguard_ok = False
                try:
                    await self._hass.services.async_call(
                        "switch",
                        "turn_off",
                        {"entity_id": switch_eid},
                        blocking=True,
                    )
                    self._is_blocked = True
                    adguard_ok = True
                    _LOGGER.info(
                        "v0.19.0 Xbox enforcement — switch.turn_off(%s)", switch_eid,
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error(
                        "Xbox switch.turn_off(%s) failed (continuing with TV-off "
                        "fallback if configured): %s", switch_eid, err,
                    )
            else:
                # No enforcement switch configured — the network-block primitive
                # is a no-op for this profile. TV-shutdown fallback may still
                # apply. Match the "AdGuard disabled" success semantics so we
                # don't reload-storm via reassert().
                adguard_ok = True
                self._is_blocked = True
                _LOGGER.debug(
                    "Xbox enforcement no-op — enforcement_switch_entity_id not set",
                )
        else:
            adguard_enabled = getattr(self._profile, "enable_adguard_block", False)
            if adguard_enabled:
                adguard_ok = False
                try:
                    await self._adguard.set_blocked(self._profile.adguard_client_name, True)
                    self._is_blocked = True
                    adguard_ok = True
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error(
                        "AdGuard block failed (continuing with TV-off path): %s", err
                    )
            else:
                # v0.15.5: AdGuard disabled → skip the call; treat as "succeeded"
                # for the failure-flag math (we chose not to call → not a failure).
                # v0.15.7 FIX: ALSO set `_is_blocked = True` as a state-machine
                # flag. Without this, `reassert()` sees drift every tick
                # (state=ENFORCING vs _is_blocked=False) → re-fires _enter_enforcing
                # → re-fires turn_off + voice every 30s. Live-observed 2026-05-26.
                adguard_ok = True
                self._is_blocked = True
                _LOGGER.debug(
                    "AdGuard blocking disabled (enable_adguard_block=False) — "
                    "skipping API call; setting _is_blocked=True as state flag"
                )
        # v0.15.4 — PARALLEL turn_off. Fire both pyatv (Apple TV via
        # MRP/Companion) and the Samsung TV (via its own integration)
        # concurrently with asyncio.gather. Was sequential before, which
        # meant Samsung waited up to ~25s for pyatv to fail-and-retry
        # before the user-visible screen went dark — way too long when
        # pyatv companion-protocol drops (the chronic v0.14.x issue).
        #
        # Both calls fully verify state-transitioned-to-inactive via
        # `_call_turn_off_with_verify` (v0.15.1 pre-check skips
        # already-off targets so the voice doesn't lie).
        tv_target = getattr(self._profile, "tv_shutdown_target", None) or (
            self._profile.tv_entity_id if self._profile.tv_shutdown_enabled else None
        )
        # v0.21.1 (FIX 1 completion) — we only reach here with the native gate
        # False. If native TV is nonetheless ACTIVE, the reason must be a sibling
        # group's exhaustion (e.g. "group:movies") while the kid is on the TV's
        # own tuner/app — a cap that does NOT apply to Live TV. The generic
        # tv_shutdown target IS that same Samsung TV, so firing turn_off here
        # would black out legitimate Live TV — the exact harm the native gate was
        # added to prevent. Suppress the TV-target turn_off (the Apple-TV/AdGuard
        # path below still runs, harmless while the Apple TV is idle). Room-wide
        # reasons and the linear_tv cap take the native branch above and never
        # reach here.
        native_tv_spared = False
        if tv_target and self._native_tv_active():
            _LOGGER.info(
                "v0.21.1 native TV active under a non-native enforce reason (%s) "
                "— suppressing tv_shutdown turn_off(%s) so Live TV isn't blacked out",
                self._enforce_reason, tv_target,
            )
            tv_target = None
            native_tv_spared = True
        # v0.19.0 — for Xbox profiles, `apple_tv_entity_id` is a
        # device_tracker.* (not a media_player), so calling
        # media_player.turn_off on it would fail. The Xbox network-block
        # primitive (switch.turn_off above) is the equivalent kill — we
        # only fire the TV-target turn_off here, and treat the missing
        # pyatv path as neutrally successful for the failure-flag math.
        is_xbox = getattr(self._profile, "device_kind", "apple_tv") == "xbox_presence"
        if is_xbox:
            if tv_target:
                _LOGGER.info(
                    "v0.19.0 Xbox firing TV-target turn_off — %s", tv_target,
                )
                tv_target_ok = await self._call_turn_off_with_verify(
                    tv_target, label="TV"
                )
            else:
                tv_target_ok = True
            turn_off_ok = True  # primary kill was switch.turn_off above
        elif tv_target:
            _LOGGER.info(
                "v0.15.4 firing parallel turn_off — Apple TV (%s) + TV target (%s)",
                self._profile.apple_tv_entity_id, tv_target,
            )
            apple_tv_task = self._call_turn_off_with_verify(
                self._profile.apple_tv_entity_id, label="Apple TV"
            )
            tv_target_task = self._call_turn_off_with_verify(tv_target, label="TV")
            turn_off_ok, tv_target_ok = await asyncio.gather(
                apple_tv_task, tv_target_task
            )
        else:
            # No TV target configured — just pyatv.
            turn_off_ok = await self._call_turn_off_with_verify(
                self._profile.apple_tv_entity_id, label="Apple TV"
            )
            tv_target_ok = True  # neutral when no target
        any_turn_off_succeeded = turn_off_ok or (
            tv_target is not None and tv_target_ok
        )

        # v0.20.0 — ONE system: fan the block out to every SECONDARY device
        # (e.g. the Xbox's FRITZ internet switch) in the SAME enforce
        # transition that blocked the Apple TV + Samsung TV. Empty (no-op) for
        # single-device profiles. Reached only after the adult-mode / monitor-
        # mode early-returns above, so a bypass never flips the Xbox switch.
        # A secondary erroring is log-only (see the helper). The return tells us
        # whether the secondary block actually landed, which counts as a
        # successful kill for the failure-flag math below.
        secondary_block_ok = await self._flip_secondary_switches(on=False)

        # v0.15.0 (spec §3.4.3) — surface enforcement failure so the
        # effective_state sensor can render `enforcing_failed`. Failure
        # = AdGuard fell over OR nothing got killed (apple_tv turn_off, the
        # tv_target turn_off, AND every secondary switch all failed).
        # v0.20.0 — `secondary_block_ok` is folded in so the headline merge
        # scenario (kid on the Xbox, Apple TV + Samsung already off → both
        # primary turn_offs report "failed" because the targets are already
        # off, but the Xbox switch WAS cut) reads as enforcing, not
        # enforcing_failed. False (no secondaries) leaves the prior behavior
        # for single-device profiles untouched.
        # v0.21.1 — `native_tv_spared` (we intentionally suppressed the tv_shutdown
        # turn_off because native TV is legitimately on under a sibling-group
        # reason) counts as a SUCCESSFUL intentional no-op, not a failure. Without
        # it, a native profile with no secondary + AdGuard off would render a
        # sustained false `enforcing_failed` on the effective_state sensor while
        # the kid watches unlimited Live TV — nothing failed; we chose not to act.
        self._last_enforcement_failed = not (
            adguard_ok
            and (any_turn_off_succeeded or secondary_block_ok or native_tv_spared)
        )

        # v0.15.8 — voice gate tightened: only fire if the Apple TV
        # ITSELF verified as inactive. Samsung TV is the secondary screen-
        # kill, not the content source — if pyatv failed (Apple TV still
        # playing) but Samsung TV's turn_off "succeeded" (whether briefly
        # or because the kid turned it back on), the kid is still
        # watching Netflix on the Apple TV. The "Bildschirmzeit ist
        # vorbei" voice would lie. Live-observed 2026-05-28: the owner's
        # test showed enforce_message firing while Netflix kept playing
        # (Apple TV usage kept incrementing through the supposed-off
        # transition).
        #
        # Prior gate (v0.14.5): `if any_turn_off_succeeded:` — counted
        # Samsung as enough. Now: only `turn_off_ok` (Apple TV's verified
        # state transition). Samsung success is still relevant for the
        # `_last_enforcement_failed` flag (partial enforcement is better
        # than none), but doesn't justify the audible claim that the
        # Apple TV was shut down.
        if turn_off_ok:
            await self._maybe_announce_enforce()
        else:
            _LOGGER.warning(
                "No turn_off path succeeded — skipping enforce_message "
                "announcement to avoid speaking a lie. Audit log already "
                "carries the enforce_turn_off_failed row."
            )

    # v0.16.0 — countdown voice. Threshold is in seconds; we fire when
    # the running remaining time crosses the configured window. 35s
    # gives the 30s tick + a 5s tolerance against tick jitter (a tick at
    # 28s would otherwise miss the trigger if the next tick is at 0s).
    _COUNTDOWN_THRESHOLD_S: int = 35

    def _countdown_voice_allowed(self, decision) -> bool:
        """v0.16.4 — mirror of audit._voice_allowed_for(profile, "countdown").

        Previously the countdown gate was a bare `decision.kind != "ACT"`
        check, which silenced the cue in monitor_only mode even when
        the parent had opted into warn_in_monitor_mode. The warn voice
        respects the opt-in; the countdown is the same kind of heads-up
        cue and now follows the same policy:

          - ACT      → speak
          - OBSERVE  → speak only if profile.warn_in_monitor_mode
          - BYPASS   → silent (adult mode / paused)
        """
        from .policy import voice_allowed
        if not voice_allowed(decision, "countdown"):
            return False
        if decision.kind == "OBSERVE":
            return bool(getattr(self._profile, "warn_in_monitor_mode", False))
        return True

    async def _maybe_fire_countdown(self, *, remaining_s: int) -> None:
        """v0.16.0 — speak the 30s pre-enforce cue once per state-cycle.

        Gates:
          - profile.countdown_message non-empty (opt-in)
          - 0 < remaining_s <= 35 (we're inside the final wind-down)
          - state in WARNING/GRACE (not OK — too early; not ENFORCING —
            too late, the cue is supposed to land BEFORE the lights go
            out)
          - voice gate allows countdown for the current should_act
            decision (BYPASS silences always; OBSERVE silences unless
            profile.warn_in_monitor_mode is set; ACT always allows)
          - latch (`_countdown_fired`) hasn't been raised this cycle

        The latch is reset in `_apply` on transition back to OK and in
        `_exit_enforcing`.

        Audit row: action=`voice_announcement` with reason=`countdown`,
        consistent with the existing enforce/extension voice rows so
        the panel can render them with the same 🔊 icon.
        """
        if self._countdown_fired:
            return
        if remaining_s <= 0 or remaining_s > self._COUNTDOWN_THRESHOLD_S:
            return
        # Only fire during the wind-down zone; OK is too early, ENFORCING
        # is too late (the lights are already going off / off).
        if self._state not in (STATE_WARNING, STATE_GRACE):
            return
        message = getattr(self._profile, "countdown_message", "") or ""
        if not message:
            return

        # v0.16.4 — gate consults the same policy the warn voice uses,
        # so a parent in monitor_only mode with warn_in_monitor_mode=True
        # hears the full heads-up sequence (warn + countdown) for
        # threshold calibration. Pre-v0.16.4 this was a bare
        # `decision.kind != "ACT"` and silently skipped the cue.
        decision = self._should_act_now()
        if not self._countdown_voice_allowed(decision):
            return

        # Lazy import for parity with _maybe_announce_enforce.
        from .voice_notifier import should_speak, speak as _speak

        if not should_speak(self._profile, message):
            return

        # Mark the latch BEFORE the await so concurrent re-entry can't
        # double-fire. (No other thread can fire it — the enforcer lock
        # is held by evaluate's caller chain — but defensive.)
        self._countdown_fired = True
        result = await _speak(self._hass, self._profile, template=message)
        if result.get("status") == "spoken":
            try:
                # Lazy import for the same reason as voice_notifier above.
                from .audit import record_admin_action
                record_admin_action(
                    self._hass,
                    profile_id=self._profile.id,
                    action="voice_announcement",
                    reason="countdown",
                    detail=result.get("message"),
                )
            except Exception:  # noqa: BLE001
                _LOGGER.debug("countdown audit row failed (non-fatal)")

    # v0.16.1 — timer-based countdown helpers (replace the v0.16.0
    # tick-based check above for the production path).
    # `_schedule_countdown_for_grace` runs once on state-machine entry
    # into GRACE. The HA timer fires (grace_seconds - 30s) later, which
    # is the moment we want the cue: 30s before enforce. The pre-existing
    # tick-based method is kept as a no-op stub for test back-compat.

    def _schedule_countdown_for_grace(self) -> None:
        """Schedule the countdown voice for 30s before enforce.

        Called on GRACE entry from `_apply`. Cancels any existing
        scheduled countdown first. Computes the delay as
        `grace_seconds - 30`; if grace_seconds <= 30 (user has
        configured a very short grace), fires almost immediately
        (max(0, …) clamp).

        No-op when `countdown_message` is empty (opt-in).
        """
        # Cancel any prior schedule (defensive — shouldn't happen
        # because GRACE-exit cancels, but kept for safety).
        self._cancel_countdown()
        message = getattr(self._profile, "countdown_message", "") or ""
        if not message:
            return  # opt-in feature; no message → no schedule
        delay_s = max(0, int(self._profile.grace_seconds) - 30)
        _LOGGER.debug(
            "Scheduling countdown voice for %ss from now (grace_seconds=%s)",
            delay_s, self._profile.grace_seconds,
        )

        # async_call_later requires a coroutine-returning callable;
        # the closure passes self so the timer can re-check current
        # state when it fires (a lot can change in 30s).
        async def _on_fire(_now):
            await self._fire_countdown_now()

        self._countdown_handle = async_call_later(self._hass, delay_s, _on_fire)

    def _cancel_countdown(self) -> None:
        """Cancel a pending countdown timer if one is scheduled."""
        if self._countdown_handle is not None:
            try:
                self._countdown_handle()
            except Exception:  # noqa: BLE001 — defensive: handle may be expired
                pass
            self._countdown_handle = None

    async def _fire_countdown_now(self) -> None:
        """HA-timer callback fired (grace_seconds - 30s) after GRACE entry.

        Re-checks gates at fire time so the voice doesn't fire under:
        - adult mode / paused (decision BYPASS)
        - monitor_only without warn_in_monitor_mode opt-in (decision
          OBSERVE without the flag)
        - state already advanced past GRACE (e.g. extension grant
          dropped state back to OK in the 30s window — the cancel in
          _apply should have caught this, but defense in depth)
        - missing media_player target or empty template

        v0.16.4 — switched from `decision.kind != "ACT"` to the shared
        `_countdown_voice_allowed` helper so the cue fires in
        monitor_only mode when the parent opts in via
        warn_in_monitor_mode (matches the warn voice). Live 2026-05-30
        13:13 grace window proved the old gate silently dropped the
        countdown row.
        """
        self._countdown_handle = None  # consumed
        if self._state != STATE_GRACE:
            _LOGGER.debug(
                "Countdown timer fired but state is now %r — skipping",
                self._state,
            )
            return
        decision = self._should_act_now()
        if not self._countdown_voice_allowed(decision):
            _LOGGER.debug(
                "Countdown timer fired but voice gate denied for decision=%s "
                "(warn_in_monitor_mode=%s) — skipping",
                decision.kind,
                bool(getattr(self._profile, "warn_in_monitor_mode", False)),
            )
            return
        message = getattr(self._profile, "countdown_message", "") or ""
        if not message:
            return
        # v0.19.2 — don't fire the "30 seconds left" cue to an empty room.
        # Same gate as the warn voice: suppress when the primary device is
        # inactive AND the TV is off (live bug 2026-06-19 — adult-mode expiry
        # walked the cycle with devices off).
        from .media_attribution import someone_could_be_watching
        _primary = self._hass.states.get(self._profile.apple_tv_entity_id)
        _tv_id = getattr(self._profile, "tv_entity_id", None) or None
        _tv = self._hass.states.get(_tv_id) if _tv_id else None
        if not someone_could_be_watching(
            getattr(self._profile, "device_kind", "apple_tv"),
            _primary.state if _primary is not None else None,
            _tv.state if _tv is not None else None,
            secondary_active=self._any_secondary_active(),
        ):
            _LOGGER.debug(
                "Countdown suppressed — nobody watching (primary=%s tv=%s)",
                _primary.state if _primary is not None else None,
                _tv.state if _tv is not None else None,
            )
            return
        from .voice_notifier import should_speak, speak as _speak
        if not should_speak(self._profile, message):
            return
        result = await _speak(self._hass, self._profile, template=message)
        if result.get("status") == "spoken":
            try:
                from .audit import record_admin_action
                record_admin_action(
                    self._hass,
                    profile_id=self._profile.id,
                    action="voice_announcement",
                    reason="countdown",
                    detail=result.get("message"),
                )
            except Exception:  # noqa: BLE001
                _LOGGER.debug("countdown audit row failed (non-fatal)")

    async def _maybe_announce_enforce(self) -> None:
        """v0.14.5 — speak the enforce_message + audit-log it.

        Called from _enter_enforcing AFTER at least one turn_off has
        verified successful. If the profile has no message configured
        or no media_player target, this is a no-op.
        """
        message = getattr(self._profile, "enforce_message", "") or ""
        if not message:
            return
        # Lazy import to avoid circular: voice_notifier imports
        # nothing from this module, but enforcer importing it at module
        # load is fine — we keep the lazy import to mirror the
        # established pattern in _call_turn_off_with_verify.
        from .voice_notifier import should_speak, speak as _speak
        from .audit import record_admin_action

        if not should_speak(self._profile, message):
            return
        # remaining_today_min is 0 at the moment of enforcement; pass
        # 0 so any {minutes} placeholder renders cleanly.
        result = await _speak(
            self._hass, self._profile, template=message, minutes=0
        )
        if result.get("status") == "spoken":
            record_admin_action(
                self._hass,
                profile_id=self._profile.id,
                action="voice_announcement",
                reason="enforce_start",
                detail=result.get("message"),
            )

    async def _call_turn_off_with_verify(
        self, entity_id: str, *, label: str
    ) -> bool:
        """Call media_player.turn_off and verify the entity actually went off.

        v0.14.2 — was silently no-op'ing when pyatv's companion-protocol
        connection dropped (the channel that handles power commands).
        Now we await the service call with a timeout, then poll for the
        entity to reach an inactive state. If it doesn't, retry once,
        then record an audit row + log loudly so the failure is visible
        on the Dashboard's Recent Activity feed instead of being a
        silent compliance failure.

        Returns True if the entity reached an inactive state, False
        otherwise (caller can decide whether to escalate further).
        """
        # Lazy import to avoid a circular dependency at module load.
        from .audit import record_admin_action
        from .media_attribution import INACTIVE_MEDIA_STATES

        # v0.15.1 — pre-check: if the entity is ALREADY inactive, skip
        # the turn_off call entirely and return False. Otherwise we
        # silently "succeed" by turning off something that wasn't on,
        # which makes any_turn_off_succeeded True and fires the
        # enforce_message voice — a lie (live-observed 2026-05-25 when
        # Samsung TV was already off but kids were watching on a
        # different output; voice fired every ~45s as the enforcer
        # tick re-evaluated).
        pre_state = self._hass.states.get(entity_id)
        if pre_state is not None and pre_state.state in INACTIVE_MEDIA_STATES:
            _LOGGER.info(
                "media_player.turn_off (%s %s) skipped — already %s; "
                "no action counted as success (would otherwise lie via voice)",
                label, entity_id, pre_state.state,
            )
            return False

        async def _attempt() -> bool:
            try:
                await asyncio.wait_for(
                    self._hass.services.async_call(
                        "media_player",
                        SERVICE_TURN_OFF,
                        {"entity_id": entity_id},
                        blocking=True,
                    ),
                    timeout=8.0,
                )
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "media_player.turn_off (%s %s) timed out after 8s",
                    label,
                    entity_id,
                )
                return False
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "media_player.turn_off (%s %s) raised: %s",
                    label,
                    entity_id,
                    err,
                )
                return False

            # Poll the state for up to 4s. We accept any state that
            # media_attribution treats as inactive.
            for _ in range(8):  # 4s in 500ms slices
                await asyncio.sleep(0.5)
                state_obj = self._hass.states.get(entity_id)
                if state_obj is None or state_obj.state in INACTIVE_MEDIA_STATES:
                    return True
            return False

        if await _attempt():
            return True
        # Retry once — pyatv often heals if you give it a beat.
        _LOGGER.info(
            "Retrying media_player.turn_off (%s %s) — first attempt didn't take",
            label,
            entity_id,
        )
        await asyncio.sleep(1.0)
        if await _attempt():
            return True

        # Both attempts failed. Record a visible audit row so the parent
        # sees it on the Dashboard.
        #
        # v0.17.4: log severity + audit emission are label-aware. The
        # `(watchdog)` retry path can hammer this every 60s while a kid
        # is actively streaming over an unreachable pyatv — recording
        # one ERROR + one audit row per tick spams the log and the
        # dashboard with redundant rows that say the same thing (184
        # rows / 6h observed on live install). The FIRST enforce
        # attempt (no `(watchdog)` in the label) still logs ERROR +
        # audit; subsequent watchdog retries downgrade to WARNING and
        # skip the audit row (the first row already conveys the failure;
        # what matters operationally is that pyatv is unreachable, not
        # how many times we retried).
        is_watchdog_retry = "(watchdog)" in (label or "")
        if is_watchdog_retry:
            _LOGGER.warning(
                "media_player.turn_off (%s %s) still failing — pyatv "
                "unreachable. AdGuard DNS block remains in place; the "
                "stream cannot continue without it, but the device is "
                "not powering off. (Suppressing per-retry audit rows.)",
                label,
                entity_id,
            )
            return False
        _LOGGER.error(
            "media_player.turn_off (%s %s) FAILED after retry — entity "
            "still active. AdGuard DNS block still in place but DOES NOT "
            "kill in-flight streams; configure tv_shutdown_enabled with a "
            "TV entity that doesn't depend on pyatv (e.g. Samsung Smart TV "
            "integration) for reliable enforcement.",
            label,
            entity_id,
        )
        try:
            record_admin_action(
                self._hass,
                profile_id=self._profile.id,
                action="enforce_turn_off_failed",
                reason=self._enforce_reason,
                detail=(
                    f"{label} ({entity_id}) didn't go off after 2 attempts. "
                    "Stream likely still playing — enable tv_shutdown fallback."
                ),
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("audit record_admin_action failed (non-fatal)")
        return False

    async def _exit_enforcing(self, *, reason: str | None = None) -> None:
        """Leave ENFORCING and reset cycle state.

        v0.17.0 — `reason` lets the caller signal *why* we're leaving so
        we can decide whether to reset the reactivation counter. Adult
        mode is a temporary pause (parent watches a movie, then the kid
        resumes), so resetting `_reactivation_count = 0` would let the
        kid re-arm one free "reactivation #1" every adult-mode cycle —
        bypassing the stern voice + parent push that requires count ≥ 2.
        Sonnet BA audit 2026-05-30 F1.

        Accepted reasons:
        - `"adult_mode"`: preserve counters (cycle resumes after expiry)
        - `None` / any other value (extension, budget reset, drift heal,
          midnight rollover, manual force-release): reset counters; the
          cycle is genuinely over.

        v0.17.0 F-G (Sonnet BA audit P1) — skip the AdGuard
        `set_blocked(False)` call when we never actually blocked. The
        signal: `_is_blocked == False` AND we're NOT in ACT. In monitor
        mode `_enter_enforcing` returns early with `_is_blocked = False`,
        so a later exit (e.g. extension granted) would otherwise
        spuriously call AdGuard. The defensive "user toggled
        enforcement_enabled OFF while we were blocked" path stays
        intact because that case has `_is_blocked == True`.
        """
        # v0.17.0 F-G — compute the policy decision so we can tell
        # "monitor mode never blocked us" from "we WERE blocked, user
        # toggled off, clean up". The defensive path (line below)
        # preserves cleanup when `_is_blocked=True`.
        decision_kind = self._should_act_now().kind

        # v0.20.0 — capture the blocked state BEFORE the AdGuard/state-flag
        # mutations below, so the secondary-switch unblock only fires when a
        # real block was in effect — mirrors the existing xbox branch's
        # `if switch_eid and self._is_blocked` guard and avoids spurious
        # switch.turn_on calls on monitor-mode exits.
        was_blocked = self._is_blocked

        # v0.20.0 — restore every SECONDARY device's internet (switch.turn_on)
        # HERE, BEFORE the device_kind dispatch, so it runs for BOTH the
        # apple_tv path AND the xbox_presence path (whose branch early-returns
        # below). Without this an xbox-PRIMARY profile that also had secondaries
        # would block them on enforce and never restore them. Empty (no-op) for
        # single-device profiles.
        if was_blocked:
            await self._flip_secondary_switches(on=True)

        # In monitor mode we never blocked, so nothing to unblock — but
        # be defensive: try once to unblock AdGuard in case the user
        # toggled enforcement_enabled OFF while we were blocked. Cheap.
        if not getattr(self._profile, "enforcement_enabled", True):
            self._is_blocked = False
        # v0.19.0 — device_kind dispatch on the unblock primitive.
        # Xbox profiles: flip enforcement_switch back ON if it was off.
        # Apple TV profiles: existing AdGuard unblock path (unchanged below).
        device_kind = getattr(self._profile, "device_kind", "apple_tv")
        if device_kind == "xbox_presence":
            switch_eid = (
                getattr(self._profile, "enforcement_switch_entity_id", None) or ""
            ).strip() if getattr(
                self._profile, "enforcement_switch_entity_id", None
            ) else ""
            if switch_eid and self._is_blocked:
                try:
                    await self._hass.services.async_call(
                        "switch",
                        "turn_on",
                        {"entity_id": switch_eid},
                        blocking=True,
                    )
                    _LOGGER.info(
                        "v0.19.0 Xbox unblock — switch.turn_on(%s)", switch_eid,
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error(
                        "Xbox switch.turn_on(%s) failed: %s", switch_eid, err,
                    )
            self._is_blocked = False
            return  # done — no AdGuard or pyatv state to manage for Xbox
        # v0.15.5/v0.15.7 — `_is_blocked` is the state-machine flag
        # for "we're nominally blocking". Clear it unconditionally on
        # exit so reassert() doesn't think there's drift. Only call
        # the real AdGuard unblock if AdGuard is enabled (or was
        # previously and is now toggled off mid-session).
        adguard_enabled = getattr(self._profile, "enable_adguard_block", False)
        # v0.17.0 F-G — skip the AdGuard call when we never set the
        # block in the first place. `_is_blocked == True` means a real
        # block happened (either by us this cycle or persisted across
        # restart via `seed_from_adguard`); in that case ALWAYS call
        # AdGuard so the cleanup-on-toggle path keeps working.
        skip_adguard = (
            adguard_enabled
            and not self._is_blocked
            and decision_kind != "ACT"
        )
        if adguard_enabled and not skip_adguard:
            try:
                await self._adguard.set_blocked(self._profile.adguard_client_name, False)
            except Exception as err:  # noqa: BLE001 — see _enter_enforcing
                _LOGGER.error("AdGuard unblock failed: %s", err)
        elif skip_adguard:
            _LOGGER.debug(
                "v0.17.0 F-G — skipping AdGuard set_blocked(False) "
                "(_is_blocked=False + decision=%s; no real block to undo)",
                decision_kind,
            )
        self._is_blocked = False
        # v0.15.0 (spec §3.4.3) — leaving ENFORCING clears the failure
        # flag regardless of unblock outcome (we're no longer trying to
        # enforce). The effective_state sensor will fall back to a
        # non-`enforcing_failed` value on the next tick.
        self._last_enforcement_failed = False
        # v0.16.0 — leaving ENFORCING resets the countdown latch and
        # the reactivation cycle counters so the NEXT enforcement cycle
        # starts fresh ("first re-on of THIS cycle" semantics).
        # v0.16.1: also cancel any pending countdown timer (defensive —
        # _apply already cancels on GRACE-exit, but if the state went
        # GRACE→ENFORCING and then directly out of ENFORCING the timer
        # may still be pending).
        self._countdown_fired = False
        self._cancel_countdown()
        # v0.17.0 — preserve the reactivation counter across adult mode.
        # Without this guard, a parent toggling adult mode mid-enforce
        # cycle hands the kid a free "reactivation #1" every cycle.
        if reason != "adult_mode":
            self._reactivation_count = 0
            self._was_apple_tv_active_last_tick = False

    def _any_secondary_active(self) -> bool:
        """v0.20.0 — True if any folded-in secondary device is active right now
        (e.g. the Xbox device_tracker reads 'home'). Used to gate the heads-up
        voices so Xbox-only play still warns even when the Apple TV is off and
        the Samsung reports off/standby (the Xbox may drive a different input)."""
        for dev in (getattr(self._profile, "secondary_devices", None) or []):
            eid = dev.get("entity_id")
            if not eid:
                continue
            st = self._hass.states.get(eid)
            if st is None:
                continue
            if dev.get("device_kind") == "xbox_presence" and st.state == "home":
                return True
        return False

    async def _flip_secondary_switches(self, *, on: bool) -> bool:
        """v0.20.0 — fan the enforce/unblock out to every secondary device's
        network switch. `on=False` blocks (switch.turn_off → cuts e.g. Xbox
        internet); `on=True` restores it.

        Returns True iff at least one secondary switch was toggled successfully
        (False when there are no secondary switches or all calls errored). The
        caller folds the on=False return into the enforcement-success math so an
        Xbox-only block (Apple TV + Samsung already off) is NOT misreported as
        `enforcing_failed` just because the already-off primary turn_off
        "failed".

        Errors are log-only and NEVER raise: a secondary switch falling over
        must not crash the tick. A partial block (TV off, Xbox switch errored)
        self-heals via reassert's Job 3.
        """
        service = "turn_on" if on else "turn_off"
        any_ok = False
        for dev in (getattr(self._profile, "secondary_devices", None) or []):
            sw = (dev.get("enforcement_switch_entity_id") or "").strip()
            if not sw:
                continue
            try:
                await self._hass.services.async_call(
                    "switch", service, {"entity_id": sw}, blocking=True
                )
                any_ok = True
                _LOGGER.info(
                    "v0.20.0 secondary %s — switch.%s(%s)",
                    dev.get("entity_id"), service, sw,
                )
            except Exception as err:  # noqa: BLE001 — non-fatal, see docstring
                _LOGGER.error(
                    "secondary switch.%s(%s) failed (non-fatal): %s",
                    service, sw, err,
                )
        return any_ok

    def _emit_state_event(
        self,
        *,
        prev_state: str | None = None,
        prev_reason: str | None = None,
    ) -> None:
        """Fire EVENT_ENFORCEMENT_CHANGED.

        v0.12.0 — payload now includes `prev_state` + `reason` so the
        action-log recorder can persist a meaningful audit entry without
        having to keep its own state-machine mirror.

        v0.17.0 F-I (Sonnet BA audit P2, generalized per adversarial
        review) — `prev_reason` carries the binding reason as it was
        BEFORE this tick's `evaluate()` mutation. The current
        `_enforce_reason` is post-mutation, which is None on relax-to-OK
        transitions; without `prev_reason` the `enforce_end`,
        `warn_cleared`, and `grace_cleared` audit rows would lose the
        attribution ("released" with no reason). Sonnet F5 originally
        flagged this for the quiet-window case; the adversarial reviewer
        generalized it — `_enforce_reason` is overwritten by EVERY
        binding mutation in `evaluate()` (group binding, latch, pin,
        quiet window), so the snapshot pattern needs to live at the
        emit site, not at one specific binding path.
        """
        self._hass.bus.async_fire(
            EVENT_ENFORCEMENT_CHANGED,
            {
                "profile_id": self._profile.id,
                "state": self._state,
                "prev_state": prev_state,
                "is_blocked": self._is_blocked,
                "reason": self._enforce_reason,
                # v0.17.0 — prev_reason defaults to the CURRENT reason
                # when the caller doesn't supply one; back-compat with
                # transitions that don't relax (WARN→GRACE, OK→WARN,
                # etc.) where the reason is the same on both sides.
                "prev_reason": prev_reason if prev_reason is not None else self._enforce_reason,
            },
        )
