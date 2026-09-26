"""Pure helper for deciding what bundle_id a tick should be attributed to.

Extracted from `coordinator._effective_bundle_id` in v0.12.1 so the rule
can be unit-tested in isolation — the coordinator imports HA which is
too heavy for the test environment.

Returns a tuple `(effective, new_last_known, new_last_known_at)` so the
caller (the coordinator) can update its instance state. The function
itself has no side effects.

The 05-23 bug that motivated this refactor:
    state == 'idle' + app_id is None + no recent last_known
    → previously returned the literal string 'unknown'
    → opened an event that ran for 19 hours
    → blew the daily budget on a day nobody was home
The fix is to return None for `idle`/`on` with no app context, so the
home screen / screensaver / AirPlay-waiting state isn't counted as
usage. Active media states (playing/paused/buffering) still return
'unknown' when an app is clearly running but pyatv can't read its
bundle id (e.g. most Apple TV games).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import NamedTuple

# These are the canonical sets — kept here too so this module is fully
# self-contained for testing. The coordinator imports the same sets
# from const for runtime use; they MUST stay aligned.
INACTIVE_MEDIA_STATES: frozenset[str | None] = frozenset(
    {"off", "standby", "unavailable", "unknown", None}
)
ACTIVE_MEDIA_STATES: frozenset[str] = frozenset(
    {"playing", "paused", "buffering", "on", "idle"}
)
# States where audio/video is actively being consumed by *some* app.
# When the app can't be identified (pyatv reports no app_id), we fall
# back to UNKNOWN_APP_BUNDLE_ID so the time is still counted. Games are
# the common case — pyatv's MediaRemote protocol doesn't expose game
# bundle ids.
#
# Also used by `enforcer.reassert`'s v0.15.4 watchdog as the "kid is
# actually watching something" predicate (NOT the broader
# `ACTIVE_MEDIA_STATES` which includes `idle`/`on` — home-screen
# navigation is not active consumption). v0.17.4 fix.
ACTIVELY_PLAYING_STATES: frozenset[str] = frozenset(
    {"playing", "paused", "buffering"}
)
# Backwards-compat alias (was the previous private name; the public
# rename in v0.17.4 keeps the watchdog import clean while not breaking
# any external code reading the old name).
_ACTIVELY_PLAYING_STATES = ACTIVELY_PLAYING_STATES
UNKNOWN_APP_BUNDLE_ID = "unknown"


def someone_could_be_watching(
    device_kind: str,
    primary_state: str | None,
    tv_state: str | None,
    secondary_active: bool = False,
) -> bool:
    """v0.19.2 — could a heads-up voice (warn / countdown) actually reach a
    viewer right now?

    Returns False when NOBODY can possibly be watching, so the integration
    doesn't nag an empty room. Live-reported 2026-06-19: adult mode expired
    while the movies group was exhausted, so the enforcer walked
    WARN→GRACE→countdown→ENFORCE and fired voices on the dining-room speaker
    even though the Apple TV had been `idle` and the Samsung TV `off` for an
    hour. (The enforce voice was already gated by the v0.15.8 success-of-
    turn_off check; warn + countdown were not.)

    Fire if EITHER signal says a viewer is plausibly present:
      - the primary device is actively consuming, OR
      - the display (Samsung TV) is on.
    Only suppress when BOTH say "off" — this is fail-safe toward firing, so a
    pyatv-stale `idle` (tvOS 26 bug) while the TV is genuinely on still warns.

    `device_kind`:
      - "apple_tv": active = primary_state in ACTIVELY_PLAYING_STATES
        (playing/paused/buffering — NOT idle/on/off/standby/unavailable).
      - "xbox_presence": active = primary_state == "home" (console on the LAN).
    `tv_state` is the configured TV entity's state ("on" = display on); pass
    None when no TV entity is configured.

    v0.20.0 — `secondary_active` is True when any device folded into this
    profile (e.g. a secondary Xbox) is currently active. For the unified
    Living Room profile, the kid may be on the Xbox with the Apple TV off and
    the Samsung reporting `off`/`standby` (Xbox drives a different input); the
    warn + countdown must still fire. Pass the OR of the secondaries' liveness.
    """
    if device_kind == "xbox_presence":
        primary_active = primary_state == "home"
    else:
        primary_active = primary_state in ACTIVELY_PLAYING_STATES
    tv_on = tv_state == "on"
    return primary_active or tv_on or secondary_active


def native_tv_is_active(
    *,
    track_native_tv: bool,
    tv_configured: bool,
    tv_state: str | None,
    source: str | None,
    excluded_sources: "list[str] | frozenset[str] | tuple[str, ...] | None",
) -> bool:
    """v0.21.0 — is the room currently watching *native* TV?

    Pure decision helper (no HA imports) so the resolver + enforce + reassert
    paths can share ONE rule and it stays unit-testable. Native TV means the
    display is on but the input is not one of the tracked devices — tuner /
    broadcast / SCART / a smart-TV app. Returns True iff ALL of:

      1. `track_native_tv` is enabled for the profile.
      2. a TV entity is configured (`tv_configured`).
      3. `tv_state == "on"` (the display is genuinely on — NOT `standby` /
         `idle` / `off` / `unavailable`).
      4. `source` is present (a missing/empty source ⇒ **fail closed**, no
         booking — we refuse to guess when the TV can't tell us the input).
      5. `source` is NOT in `excluded_sources` (those inputs belong to a
         tracked device, e.g. HDMI1=Apple TV, HDMI2/DVI=Xbox).

    The excluded-source list is what distinguishes native TV from the tracked
    devices, so the enforce/reassert anti-defeat paths can rely on this check
    alone; the coordinator additionally applies the apple_tv > secondary >
    native precedence before ever consulting it.
    """
    if not track_native_tv or not tv_configured:
        return False
    if tv_state != "on":
        return False
    if not source:  # missing / empty source ⇒ fail closed
        return False
    # v0.21.1 (FIX 2a) — compare case- and surrounding-whitespace-insensitively.
    # TVs report the same input inconsistently ("HDMI1" vs "hdmi1" vs " HDMI1 ";
    # CEC names like "Apple TV"); an exact/case-sensitive check would let the
    # Apple-TV input read as *native* during a pyatv-freeze window and mis-book.
    # Internal spacing ("HDMI 1" vs "HDMI1") is deliberately NOT normalized —
    # that mismatch is surfaced by the coordinator's source_list warning (2b).
    # v0.21.1 — str() coerces defensively: a media_player could report a non-str
    # truthy `source`; keep this a boolean predicate instead of raising into the
    # resolver/enforcer tick.
    src = str(source).strip().casefold()
    excluded = {str(s).strip().casefold() for s in (excluded_sources or ())}
    return src not in excluded


def excluded_sources_match_source_list(
    excluded: "list[str] | frozenset[str] | tuple[str, ...] | None",
    source_list: "list[str] | frozenset[str] | tuple[str, ...] | None",
) -> bool:
    """v0.21.1 (FIX 2b) — does at least ONE configured excluded source actually
    appear in the TV entity's reported `source_list`?

    Pure predicate (no HA imports) so the coordinator's one-time misconfig
    warning can be unit-tested in isolation. Comparison mirrors
    `native_tv_is_active`: casefold + strip both sides (surrounding whitespace
    only — internal spacing still counts as a mismatch, which is the whole
    point of the warning). Returns False when either side is empty.

    When this returns False for a configured (non-empty) excluded list, none of
    the parent's excluded inputs match what the TV reports — so a tracked
    device's input (e.g. the Apple TV's HDMI) could be mis-booked as native TV.
    """
    if not excluded or not source_list:
        return False
    norm_excluded = {s.strip().casefold() for s in excluded}
    norm_sources = {s.strip().casefold() for s in source_list}
    return not norm_excluded.isdisjoint(norm_sources)


# ---------------------------------------------------------------------------
# v0.17.3 — stale-session decision (pure function).
# ---------------------------------------------------------------------------
#
# The v0.17.1 fix added staleness detection on the apple_tv mirror to handle
# the overnight-Disney bug (pyatv push listener silently dies; the entity
# stays at the last cached `playing` state for hours, accruing phantom
# usage). It closes the open event at `last_updated` (best-guess true end
# time) whenever the entity is push-quiet for >= stale_session_minutes.
#
# v0.17.3: that fix UNDER-counts during legitimate playback on tvOS 26 4K,
# where pyatv pushes very rarely between updates (5+ minute gaps observed
# during steady Netflix playback). Every push-quiet window triggers a close
# back at the open-time `last_updated` -> the session records 0.0 minutes
# even though the kid was watching the whole time. Cross-checked against
# the live device on 2026-06-09: 2 of 3 zero-duration sessions today were
# real under-counts (Samsung TV stayed `on` throughout); a Samsung-on
# liveness gate would have correctly distinguished them.
#
# The fix here adds an out-of-band liveness signal (the Samsung TV
# state, the same `tv_entity_id` already used by the v0.4.0 TV-shutdown
# feature) to distinguish the two cases that look identical from the
# apple_tv mirror alone:
#   (A) overnight-Disney: device truly OFF, pyatv frozen on stale 'playing'
#       (Samsung is `off` -> close at last_updated, preserves v0.17.1)
#   (B) live playback:    device truly ON, pyatv just push-quiet
#       (Samsung is `on`/`unknown`/`unavailable` -> KEEP_OPEN, accrue to now)
#
# Bias under uncertainty is KEEP_OPEN, because for a screen-time enforcement
# tool under-counting (silently lifting the budget) is the worse failure
# than over-counting (a fair count the kid can dispute).
#
# Safety nets to prevent unbounded over-count:
# - When `tv_entity_id` is NOT configured, v0.20.1 biases to KEEP_OPEN
#   (accrue through push gaps) and relies on the runaway ceiling to bound
#   a genuine stuck-mirror. Pre-v0.20.1 this closed at last_updated, which
#   under-counted real tvOS-26 playback on the common no-TV install.
# - When the open event's age exceeds STALE_RUNAWAY_CEILING_S (the
#   `CLOSE_RUNAWAY` path), force-close regardless of the Samsung state.
#   Caps the worst-case stuck-Samsung / uninstalled-Samsung failure mode.
#
# AdGuard DNS-recency is NOT used as a liveness signal — a standby Apple TV
# emits Apple background keepalive every 1-12 min, so DNS recency cannot
# separate standby from active (proven earlier in this development cycle).
# Including it would re-introduce the original under-count regression.


class StaleAction(Enum):
    """Result of `decide_stale_action`.

    - `NOOP`: nothing to do this tick (threshold disabled, no open event,
      apple_tv not in ACTIVE_MEDIA_STATES, or not stale yet).
    - `KEEP_OPEN`: entity is stale-in-ACTIVE but a corroborating liveness
      signal indicates the device is still live (or the gate cannot rule
      it out). Leave the open event alone — it accrues to now naturally.
    - `CLOSE_AT_LAST_UPDATED`: entity is stale-in-ACTIVE AND the device
      is definitively off. Close the open event at `last_updated`
      (best-guess true end time). This is the v0.17.1 overnight-Disney
      path.
    - `CLOSE_RUNAWAY`: hard cap fired regardless of liveness — the open
      event has been accruing too long. Close at `last_updated` and emit
      a distinct audit reason so the parent can see why.
    """
    NOOP = "noop"
    KEEP_OPEN = "keep_open"
    CLOSE_AT_LAST_UPDATED = "close_at_last_updated"
    CLOSE_RUNAWAY = "close_runaway"


# Display states we treat as "definitively off". Anything else (incl.
# `unknown`, `unavailable`, `on`, `idle`, `playing`, `paused`, `None`) is
# treated as "not definitively off" -> may still be live -> KEEP_OPEN.
# Strict allow-list FOR closing; everything else keeps counting. Flip
# states into / out of this set to tune the entire policy — this is the
# only place the over- vs under-count knob lives.
_TV_DEFINITELY_OFF_STATES: frozenset[str] = frozenset({"off", "standby"})


# Runaway protector: if the open event has been accruing for this many
# seconds while the apple_tv mirror is stale, force-close at last_updated
# regardless of the Samsung gate. Caps the worst-case "Samsung stuck on
# `on`" / "Samsung integration uninstalled and we keep returning `unknown`"
# failure modes. 150 min = ~2x the longest legitimate continuous-`playing`
# span observed in 8 days of recorder history on the owner's device (74 min,
# from the self-heal data-validation), so a real long movie clears the cap
# with margin; a true overnight stuck-mirror does not.
STALE_RUNAWAY_CEILING_S: int = 150 * 60


def decide_stale_action(
    *,
    apple_tv_state: str | None,
    apple_tv_last_updated_age_s: float | None,
    stale_threshold_s: int,
    has_open_event: bool,
    open_event_age_s: float | None,
    tv_entity_configured: bool,
    tv_state: str | None,
) -> StaleAction:
    """Decide what `_check_for_stale_session` should do for this tick.

    Pure function: takes only primitives so it can be unit-tested without
    importing Home Assistant. The coordinator method reads the relevant
    `hass.states` values, clamps clock skew, and calls this; everything
    else is decision logic.

    Decision rules — first match wins:

      1. `stale_threshold_s <= 0`                          -> NOOP (opt-out)
      2. `has_open_event` is False                          -> NOOP
      3. `apple_tv_state` not in `ACTIVE_MEDIA_STATES`     -> NOOP
         (transitions to off/idle are handled by `_sync_open_event`)
      4. `apple_tv_last_updated_age_s` is None
         OR `< stale_threshold_s`                           -> NOOP
         (entity is healthy; nothing stale yet)
      --- we are stale-in-ACTIVE; consult the liveness gate ---
      5. `open_event_age_s >= STALE_RUNAWAY_CEILING_S`     -> CLOSE_RUNAWAY
         (runaway protector — bounds stuck-Samsung damage)
      6. `tv_entity_configured` is False                    -> KEEP_OPEN
         (v0.20.1 — no liveness signal; accrue through tvOS-26 push gaps
         and let the runaway ceiling in rule 5 bound the worst case. Was
         CLOSE_AT_LAST_UPDATED, which under-counted real playback.)
      7. `tv_state` in `_TV_DEFINITELY_OFF_STATES`         -> CLOSE_AT_LAST_UPDATED
         (definitively off — overnight-Disney case)
      8. otherwise (Samsung `on`/`unknown`/`unavailable`/...) -> KEEP_OPEN
         (device may be live; bias to "under-count is the worse failure")

    The caller is expected to clamp negative ages to 0 to defuse clock
    skew before calling — this function trusts its inputs to be >= 0.
    """
    if stale_threshold_s <= 0:
        return StaleAction.NOOP
    if not has_open_event:
        return StaleAction.NOOP
    if apple_tv_state not in ACTIVE_MEDIA_STATES:
        return StaleAction.NOOP
    if apple_tv_last_updated_age_s is None:
        return StaleAction.NOOP
    if apple_tv_last_updated_age_s < stale_threshold_s:
        return StaleAction.NOOP

    # Stale-in-ACTIVE. Apply liveness gate in priority order.
    if (
        open_event_age_s is not None
        and open_event_age_s >= STALE_RUNAWAY_CEILING_S
    ):
        return StaleAction.CLOSE_RUNAWAY
    if not tv_entity_configured:
        # v0.20.1 — no TV liveness signal (the common "just an Apple TV"
        # install). On tvOS 26, pyatv pushes rarely during steady playback
        # (~5 min gaps), so the pre-v0.20.1 CLOSE_AT_LAST_UPDATED here recorded
        # 0 min for real watching every push-quiet window — silently WEAKER
        # enforcement than a TV-equipped install. Bias to KEEP_OPEN instead:
        # accrue through push gaps, and rely on the STALE_RUNAWAY_CEILING_S cap
        # above (2.5 h) to bound the worst case (a genuine off-but-frozen
        # overnight session accrues at most that ceiling, then CLOSE_RUNAWAY
        # fires). Under-counting a fair budget is the worse failure for an
        # enforcement tool than a bounded, disputable over-count.
        return StaleAction.KEEP_OPEN
    if tv_state in _TV_DEFINITELY_OFF_STATES:
        return StaleAction.CLOSE_AT_LAST_UPDATED
    return StaleAction.KEEP_OPEN


# ---------------------------------------------------------------------------
# v0.18.0 — DNS-corroborated attribution decision (pure function).
# ---------------------------------------------------------------------------
#
# The piece that closes the bundle-attribution gap exposed on 2026-06-14:
# pyatv on tvOS 26 4K goes push-silent during steady playback. v0.17.3
# correctly KEEPS COUNTING via the Samsung-on liveness gate (time IS being
# used), but the bundle_id stays frozen at whatever pyatv last said. If the
# kid switches Disney+ -> KooApps game, the integration keeps booking the
# session as Disney+/movies for hours.
#
# `decide_attribution` consults the v0.18.0 DNS classifier (see
# dns_classifier.py) as an out-of-band corroborator and produces ONE of
# three actions:
#   - PRESERVE: pyatv is right, or we lack signal to disagree
#   - ANNOTATE_GROUP: open event has been misattributed; append a
#     GroupSegment so the breakdown is correct WITHOUT closing the event
#     (preserves audit truth: "Disney+ event, but minutes M..N reclassified
#     to gaming via DNS evidence")
#   - CLOSE_AT_LAST_UPDATED: sustained ambient signal (device online but
#     no foreground app traffic seen) -> v0.17.1 close path. The visible
#     gap is honest under-count, made explicit via a diagnostic sensor.
#
# Critical safeguards baked into the rules:
# - Sticky-bundle / curated-safelist: Disney+ / Netflix / Prime / YouTube
#   etc. CANNOT be downgraded to gaming by an Apple Game Center heartbeat
#   alone. The kid can't claim "movies budget" via a real movie + happen-
#   stance GC ping.
# - >= 3 hits required to cross groups via BUNDLE confidence. Single noisy
#   query can't flip the bucket.
# - DNS-down = fail-open = v0.17.3 behavior preserved exactly.
#
# Inputs are primitives; the coordinator adapter is responsible for
# clamping ages and resolving the open-event-current-group (from the
# latest segment or the bundle's curated group).


class AttributionAction(Enum):
    """The three outcomes `decide_attribution` can return.

    - `PRESERVE`: do nothing; pyatv is right or we lack the signal to disagree.
    - `ANNOTATE_GROUP`: append a `GroupSegment` to the open event with
      `decision.new_group`. The event's `bundle_id` is NOT changed (truth
      preservation); the group breakdown is what matters for budget math.
    - `CLOSE_AT_LAST_UPDATED`: sustained ambient signal — call the v0.17.1
      close path. The visible time gap is intentional and surfaced via the
      `attribution_gap_minutes_today` sensor.
    """
    PRESERVE = "preserve"
    ANNOTATE_GROUP = "annotate_group"
    CLOSE_AT_LAST_UPDATED = "close_at_last_updated"


@dataclass(frozen=True)
class AttributionDecision:
    """Result of `decide_attribution`. Frozen so the coordinator can stash
    it in an audit row without worrying about later mutation."""
    action: AttributionAction
    new_group: str | None = None         # set when ANNOTATE_GROUP
    reason: str = ""                      # for audit row + sensor
    confidence: str = ""                  # DnsClassification.confidence.name


# Curated streaming bundles that CANNOT be downgraded to gaming/other based
# on a GROUP_ONLY DNS hit alone. The combination "real Disney+ session +
# occasional Game Center heartbeat" is the dominant false-positive shape;
# this safelist closes it. Kept in sync with `categorize.CURATED` for the
# major streaming entries; the safelist is intentionally narrower than
# CURATED (only the ones where false-promotion to gaming is plausible).
CURATED_STREAMING_BUNDLES: frozenset[str] = frozenset({
    "com.disney.disneyplus",
    "com.netflix.Netflix",
    "com.amazon.aiv.AIVApp",
    "com.google.ios.youtube",
    "tv.twitch",
    "tv.plex.player",
    "org.jellyfin.swiftfin",
    "com.jellyfin.jellyfin",
    "org.jellyfin.expo-mobile",
    "com.paramountplus.ott",
    "com.hulu.plus",
    "com.skygo.SkyTicket",
    "de.skygo.skygo",
    "de.dazn.dazn",
    "de.zdf.zdfmediathek.tvos",
    "de.ard.mediathek.tvos",
    "de.rtl.now",
    "de.prosiebensat1.app7tv",
    "tv.joyn.app",
    # Music apps too — important not to misattribute a HomePod AirPlay
    # session to gaming because GC pinged in the background.
    "com.apple.TVMusic",
    "com.spotify.client",
    "com.apple.Music",
    # AirPlay isn't really an "app" per se but pyatv reports it; same
    # protection applies.
    "com.apple.TVAirPlay",
})


# How fresh pyatv must be (seconds since last_updated) to OVERRIDE the
# DNS signal. While pyatv is talking we trust it; only when it goes silent
# does DNS take over as the corroborator.
PYATV_FRESH_THRESHOLD_S: int = 90

# How many consecutive ticks of AMBIENT_ONLY DNS (no foreground signal)
# we tolerate before forcing the v0.17.1 visible-gap close. With a 30s
# coordinator tick, 4 ticks = ~2 minutes — long enough to filter out
# transient DNS lulls during legitimate playback but short enough to
# surface a real "kid walked away with Samsung on" situation.
AMBIENT_CLOSE_STREAK: int = 4


def decide_attribution(
    *,
    pyatv_age_s: float | None,
    dns,                                  # DnsClassification | None
    open_event_bundle_id: str | None,
    open_event_current_group: str,        # latest segment group, or bundle's curated group
    consecutive_ambient_ticks: int = 0,
    pyatv_fresh_threshold_s: int = PYATV_FRESH_THRESHOLD_S,
    ambient_close_streak: int = AMBIENT_CLOSE_STREAK,
    min_bundle_cross_group_hits: int = 3,
) -> AttributionDecision:
    """v0.18.0 — Decide what to do with the open UsageEvent given pyatv +
    DNS state.

    Pure function: takes primitives + the DnsClassification object,
    returns an AttributionDecision. The coordinator adapter is responsible
    for reading hass.states, querying AdGuard, and acting on the result.

    Decision order (first match wins):

      1. No open event (bundle None)                  -> PRESERVE
      2. pyatv fresh (< pyatv_fresh_threshold_s)      -> PRESERVE
         (we trust pyatv when it's talking)
      3. DNS unavailable (None or Confidence.NONE)    -> PRESERVE
         (fail-open — v0.17.3 behavior preserved)
      4. DNS AMBIENT_ONLY + streak >= ambient_close_streak -> CLOSE_AT_LAST_UPDATED
         (visible-gap close — sustained "no foreground" signal)
      5. DNS AMBIENT_ONLY (short streak)              -> PRESERVE
      6. DNS BUNDLE matches open event bundle         -> PRESERVE (confirming)
      7. DNS BUNDLE different bundle, same group as current -> PRESERVE
         (bundle swapped but group unchanged — irrelevant for budgets)
      8. DNS BUNDLE different group, hits >= min_cross_group_hits -> ANNOTATE_GROUP
         (the live-bug fix: Disney+ -> KooApps -> gaming group)
      9. DNS BUNDLE different group, hits < min       -> PRESERVE
         (insufficient evidence to cross groups)
     10. DNS GROUP_ONLY + open bundle in CURATED_STREAMING_BUNDLES -> PRESERVE
         (sticky-bundle safelist — Disney+ won't downgrade to gaming on a
         lone Game Center heartbeat)
     11. DNS GROUP_ONLY + open group already matches  -> PRESERVE
     12. DNS GROUP_ONLY + bundle is None/UNKNOWN      -> ANNOTATE_GROUP
         (best-effort group attribution for unknown apps)
     13. DNS GROUP_ONLY + open bundle is non-curated  -> PRESERVE
         (conservative: don't flip a confirmed bundle on group-only signal)

    Returns the decision frozen so the coordinator can pass it to audit.
    """
    # Rule 1 — nothing to attribute.
    if open_event_bundle_id is None:
        return AttributionDecision(
            action=AttributionAction.PRESERVE,
            reason="no_open_event",
        )

    # Rule 2 — pyatv is talking; trust it.
    if pyatv_age_s is not None and pyatv_age_s < pyatv_fresh_threshold_s:
        return AttributionDecision(
            action=AttributionAction.PRESERVE,
            reason="pyatv_fresh",
            confidence=f"pyatv_age_{int(pyatv_age_s)}s",
        )

    # Rule 3 — no DNS signal at all.
    if dns is None:
        return AttributionDecision(
            action=AttributionAction.PRESERVE,
            reason="dns_unavailable",
        )

    # The classifier returns its Confidence enum; we compare by name to
    # avoid an import cycle (decide_attribution lives in media_attribution
    # which is imported widely; dns_classifier should stay leaf-level).
    confidence_name = getattr(dns.confidence, "name", str(dns.confidence))

    if confidence_name == "NONE":
        return AttributionDecision(
            action=AttributionAction.PRESERVE,
            reason="dns_no_signal",
            confidence=confidence_name,
        )

    # Rules 4-5 — AMBIENT_ONLY.
    if confidence_name == "AMBIENT_ONLY":
        # v0.18.0 follow-up — sticky-bundle protection for AMBIENT.
        # Streaming services (Disney+, Netflix, etc.) use long-lived TCP +
        # heavy client-side buffering; AMBIENT (no foreground DNS) is the
        # EXPECTED steady-state of a real session, not evidence the kid
        # walked away. Live monitor-mode data on Marc's install
        # (2026-06-14) showed 30+ spurious CLOSE proposals fired during
        # a real Disney+ session in 20 min. Refuse the close when the
        # open bundle is on the curated streaming safelist; the sustained-
        # ambient close is meant for "kid actually went idle" scenarios
        # with non-streaming bundles (idle/unknown/games-without-DNS).
        if (
            consecutive_ambient_ticks >= ambient_close_streak
            and open_event_bundle_id not in CURATED_STREAMING_BUNDLES
        ):
            return AttributionDecision(
                action=AttributionAction.CLOSE_AT_LAST_UPDATED,
                reason=f"sustained_ambient_{consecutive_ambient_ticks}_ticks",
                confidence=confidence_name,
            )
        if consecutive_ambient_ticks >= ambient_close_streak:
            # Curated bundle protects against the close — explain why in
            # the audit so the parent can see the safelist firing.
            return AttributionDecision(
                action=AttributionAction.PRESERVE,
                reason=(
                    f"ambient_close_blocked_by_curated_safelist_"
                    f"{open_event_bundle_id}"
                ),
                confidence=confidence_name,
            )
        return AttributionDecision(
            action=AttributionAction.PRESERVE,
            reason=f"ambient_streak_{consecutive_ambient_ticks}",
            confidence=confidence_name,
        )

    # Rules 6-9 — BUNDLE.
    if confidence_name == "BUNDLE":
        dns_bundle = dns.bundle_id
        dns_group = dns.group
        hits = getattr(dns, "bundle_hit_count", 0)

        # Rule 6 — DNS confirms the current bundle.
        if dns_bundle == open_event_bundle_id:
            return AttributionDecision(
                action=AttributionAction.PRESERVE,
                reason="bundle_match",
                confidence=confidence_name,
            )

        # Rules 7-9 — DNS shows a different bundle.
        if dns_group == open_event_current_group:
            # Same group, different bundle — no budget impact.
            return AttributionDecision(
                action=AttributionAction.PRESERVE,
                reason="bundle_swap_same_group",
                confidence=confidence_name,
            )

        # Different group: require minimum-hit threshold to act.
        if hits >= min_bundle_cross_group_hits:
            return AttributionDecision(
                action=AttributionAction.ANNOTATE_GROUP,
                new_group=dns_group,
                reason=(
                    f"dns_bundle_{dns_bundle}_{hits}hits_"
                    f"crosses_{open_event_current_group}_to_{dns_group}"
                ),
                confidence=confidence_name,
            )

        # Insufficient hits — wait for more evidence.
        return AttributionDecision(
            action=AttributionAction.PRESERVE,
            reason=f"insufficient_cross_group_hits_{hits}_of_{min_bundle_cross_group_hits}",
            confidence=confidence_name,
        )

    # Rules 10-13 — GROUP_ONLY.
    if confidence_name == "GROUP_ONLY":
        dns_group = dns.group

        # Rule 10 — curated streaming bundle is exempt from group-only
        # downgrade. Closes the "Disney+ + background GC ping" false
        # positive AND prevents Apple Music sessions from being misread
        # as gaming.
        if open_event_bundle_id in CURATED_STREAMING_BUNDLES:
            return AttributionDecision(
                action=AttributionAction.PRESERVE,
                reason=f"curated_safelist_protects_{open_event_bundle_id}",
                confidence=confidence_name,
            )

        # Rule 11 — already attributed to this group.
        if dns_group == open_event_current_group:
            return AttributionDecision(
                action=AttributionAction.PRESERVE,
                reason="group_match",
                confidence=confidence_name,
            )

        # Rule 12 — bundle is unknown; group-only is the best signal we have.
        if (
            open_event_bundle_id is None
            or open_event_bundle_id == "unknown"
        ):
            return AttributionDecision(
                action=AttributionAction.ANNOTATE_GROUP,
                new_group=dns_group,
                reason=f"group_only_attribution_for_unknown_bundle",
                confidence=confidence_name,
            )

        # Rule 13 — non-curated bundle, group-only signal disagrees.
        # Conservative: don't flip a positively-identified bundle on a
        # weaker group-only signal. If the kid is actually gaming, the
        # game's own CDN will eventually fire and we'll catch it at
        # BUNDLE confidence with the cross-group rule above.
        return AttributionDecision(
            action=AttributionAction.PRESERVE,
            reason=(
                f"group_only_too_weak_to_flip_"
                f"{open_event_bundle_id}_to_{dns_group}"
            ),
            confidence=confidence_name,
        )

    # Unknown confidence value (defensive — future enum additions).
    return AttributionDecision(
        action=AttributionAction.PRESERVE,
        reason=f"unknown_confidence_{confidence_name}",
    )


class AttributionResult(NamedTuple):
    bundle_id: str | None              # what to attribute to (None = don't)
    last_known_bundle_id: str | None   # for the caller to persist
    last_known_seen_at: datetime | None


def resolve_effective_bundle_id(
    *,
    media_state: str | None,
    app_id: str | None,
    now: datetime,
    last_known_bundle_id: str | None,
    last_known_seen_at: datetime | None,
    idle_grace_minutes: int,
) -> AttributionResult:
    """Decide what (if anything) this tick should count toward.

    Inputs are the current media_player state, the app_id pyatv reports,
    and the caller's "last-known" memory (so a brief pyatv reconnect
    doesn't bounce the bundle id).
    """
    # Off / unavailable / unreported → not in use.
    if media_state in INACTIVE_MEDIA_STATES:
        return AttributionResult(None, None, None)

    if media_state in ACTIVE_MEDIA_STATES:
        # Clearly playing app — take it, refresh last-known.
        if app_id:
            return AttributionResult(app_id, app_id, now)

        # No current app_id. If we saw one recently and we're inside
        # the grace window, treat it as the same app (covers a brief
        # pyatv reconnect or a pause inside the same app).
        if last_known_bundle_id and last_known_seen_at is not None:
            grace = timedelta(minutes=max(0, idle_grace_minutes))
            if (now - last_known_seen_at) <= grace:
                return AttributionResult(
                    last_known_bundle_id, last_known_bundle_id, last_known_seen_at
                )

        # No recent app context. Decide by state:
        # - 'idle' / 'on'  → home screen / screensaver / receiver — don't count.
        # - 'playing' / 'paused' / 'buffering' → real audio/video; count as
        #   "unknown" (likely a game pyatv can't identify).
        if media_state in _ACTIVELY_PLAYING_STATES:
            return AttributionResult(UNKNOWN_APP_BUNDLE_ID, None, None)
        return AttributionResult(None, None, None)

    # Defensive — should never hit unless HA adds a new media_player state.
    return AttributionResult(None, last_known_bundle_id, last_known_seen_at)


# ---------------------------------------------------------------------------
# v0.18.0 — In-integration proactive pyatv reload (pure decision).
# ---------------------------------------------------------------------------
#
# Symptom this feature heals: pyatv's push channel (the companion protocol)
# silently dies. HA's apple_tv media_player entity sits at `idle` or `on`
# for hours, last_updated frozen on the last push the integration ever
# received. While the entity is in {idle, on} the v0.17.3 stale-session
# path does NOT close anything (it only fires while ACTIVE_MEDIA_STATES
# include {playing, paused, buffering, on, idle} AND there's an open
# event) — so the stale-session safety net does not engage during stuck-
# idle pyatv. Meanwhile the kid is watching Netflix on the device and we
# record nothing.
#
# The heal: `hass.config_entries.async_reload(apple_tv_entry_id)` tears
# down + rebuilds the Apple TV core integration's config entry, which
# restarts pyatv's listener. Reconnect takes ~10-15s in practice.
#
# Gates (all must hold to trigger):
#   - apple_tv_state in {idle, on}:
#     these are the push-quiet symptom states. We deliberately DO NOT
#     trigger during {playing, paused, buffering} — a stale `playing`
#     is handled by the v0.17.3 stale-session path, and reloading
#     mid-playback would briefly disrupt attribution unnecessarily.
#   - pyatv_quiet_s >= PYATV_RELOAD_MIN_STUCK_S (10 min):
#     pyatv pushes every 30-60s during normal home-screen activity, so
#     10 min of silence is far longer than any legitimate quiet window.
#   - samsung_state == 'on':
#     out-of-band liveness gate. We must not reload pyatv when nobody is
#     actually using the device. Strict 'on' check here (unlike the
#     stale-session gate which fails-open on 'unknown'/'unavailable')
#     because a wrong reload is more disruptive than a missed one.
#   - dns_recent_hits >= DNS_RECENT_MIN_HITS (1, in last 60s):
#     second corroborator that the device is actually online — if Samsung
#     is stuck `on` due to a firmware bug AND DNS is quiet, the Apple TV
#     is likely off/sleeping; skip the reload.
#   - last_reload_age_s is None OR >= PYATV_RELOAD_RATE_LIMIT_S (10 min):
#     caps reloads at 6/hr worst case. State is NOT persisted; HA restart
#     re-enables reload immediately, which is acceptable (HA restart also
#     resets pyatv state).
#
# Fail-CLOSED posture: any unmet gate -> False. Unlike the stale-session
# path which fails open (under-count is the worse failure), here failing
# closed is correct — a spurious reload disrupts an active session.

# Minimum push-quiet duration before a reload is even considered.
PYATV_RELOAD_MIN_STUCK_S: int = 10 * 60
# v0.19.3 — higher threshold for the stuck-PLAYING case. Normal tvOS 26
# steady-playback push gaps run ~5 min (312 s observed); 20 min of zero
# updates while the display is on is unambiguously a dead push channel.
PYATV_RELOAD_PLAYING_STUCK_S: int = 20 * 60

# Minimum time between consecutive reloads. Caps spam at 6/hr.
PYATV_RELOAD_RATE_LIMIT_S: int = 10 * 60

# Window size for the AdGuard DNS-recency corroborator.
DNS_RECENT_WINDOW_S: int = 60

# Minimum DNS hits in DNS_RECENT_WINDOW_S to count as "device online".
DNS_RECENT_MIN_HITS: int = 1


# Apple TV states from which a reload is attempted.
#   idle/on  — push-quiet symptom (home screen / just-on). Requires a DNS
#              corroborator (device is making background lookups) because
#              `idle` alone is a weak "someone's here" signal.
#   playing/paused/buffering — v0.19.3: pyatv froze MID-PLAYBACK (the
#              recurring tvOS 26 push-death). The stale-session path keeps
#              COUNTING correctly via the Samsung-on gate, but never reloads,
#              so the mirror lies (frozen "current app") until something
#              reloads it — live-reported 2026-06-20 (stuck `playing` 73 min).
#              Uses a LONGER threshold (normal steady-playback push-quiet is
#              ~5 min) and does NOT require the DNS corroborator: a steady
#              stream is long-lived TCP that makes almost no DNS, so the 60s
#              DNS window is blind to it. Samsung-on is the liveness proof
#              instead, and the reload is wake-safe + rate-limited, so the
#              worst case (device actually off, Samsung stuck on) is a
#              harmless reconnect that simply un-sticks the mirror.
_PYATV_RELOAD_IDLE_STATES: frozenset[str] = frozenset({"idle", "on"})
# Back-compat alias (pre-v0.19.3 name).
_PYATV_RELOAD_TRIGGER_STATES = _PYATV_RELOAD_IDLE_STATES


@dataclass(frozen=True)
class PyatvReloadInputs:
    """Inputs to `decide_pyatv_reload`. Frozen so callers can stash it in
    an audit row without worrying about later mutation.

    All fields are primitives — the coordinator adapter reads the relevant
    `hass.states` values, clamps clock skew, and queries AdGuard, then
    constructs this dataclass and calls the decision function.
    """
    apple_tv_state: str | None
    pyatv_quiet_s: float | None
    samsung_state: str | None
    dns_recent_hits: int
    last_reload_age_s: float | None


def decide_pyatv_reload(inputs: PyatvReloadInputs) -> bool:
    """v0.18.0 — Decide whether to fire `async_reload` on the Apple TV
    core config entry this tick.

    Pure function: takes only primitives via a frozen dataclass, returns
    a bool. The coordinator adapter is responsible for reading state,
    clamping ages, querying AdGuard, and (on True) calling
    `hass.config_entries.async_reload(apple_tv_entry_id)`.

    Two trigger shapes (v0.19.3):

    A) idle/on (push-quiet symptom). ALL must hold:
      1. `apple_tv_state` in `{idle, on}`.
      2. `pyatv_quiet_s >= PYATV_RELOAD_MIN_STUCK_S` (10 min).
      3. `samsung_state == 'on'`.
      4. `dns_recent_hits >= DNS_RECENT_MIN_HITS` (online corroborator —
         `idle` is a weak "someone's here" signal, so require DNS proof).
      5. rate-limit OK.

    B) playing/paused/buffering (pyatv froze MID-PLAYBACK). ALL must hold:
      1. `apple_tv_state` in ACTIVELY_PLAYING_STATES.
      2. `pyatv_quiet_s >= PYATV_RELOAD_PLAYING_STUCK_S` (20 min — past
         normal steady-playback push gaps).
      3. `samsung_state == 'on'` (the liveness proof — see note above; the
         60s DNS gate is blind to long-lived streaming TCP, so it is NOT
         required here).
      4. rate-limit OK.

    Any unmet gate returns False (fail-CLOSED). Negative ages should be
    clamped to 0 by the caller before passing in.
    """
    state = inputs.apple_tv_state
    if state in _PYATV_RELOAD_IDLE_STATES:
        threshold = PYATV_RELOAD_MIN_STUCK_S
        require_dns = True
    elif state in ACTIVELY_PLAYING_STATES:
        threshold = PYATV_RELOAD_PLAYING_STUCK_S
        require_dns = False
    else:
        return False
    if inputs.pyatv_quiet_s is None:
        return False
    if inputs.pyatv_quiet_s < threshold:
        return False
    if inputs.samsung_state != "on":
        return False
    if require_dns and inputs.dns_recent_hits < DNS_RECENT_MIN_HITS:
        return False
    if (
        inputs.last_reload_age_s is not None
        and inputs.last_reload_age_s < PYATV_RELOAD_RATE_LIMIT_S
    ):
        return False
    return True
