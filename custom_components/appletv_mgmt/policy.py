"""Pure single-source-of-truth for "should the integration act right now?"

Introduced in v0.15.0. Replaces the 3-4 places where this question used to
be answered (enforcer + audit recorder + sensor + voice gate), each with
subtle drift. See `docs/biz_logic_spec_v2.2.md` §3.4 for full design.

NO HA imports. NO storage access. NO side effects. The caller is responsible
for materializing the inputs (mode string, current adult_mode_until,
current `now`) and acting on the returned decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

DecisionKind = Literal["ACT", "OBSERVE", "BYPASS"]
BypassReason = Literal["adult_mode", "paused", "monitor_only"]


@dataclass(frozen=True)
class ActDecision:
    """The decision returned by `should_act`.

    - `kind == "ACT"`: full enforcement is allowed (mode=enforced, no
      adult-mode override). `reason` is None.
    - `kind == "OBSERVE"`: monitor mode — track + warn (if configured)
      but never call AdGuard or sleep media players.
    - `kind == "BYPASS"`: integration is quiet — adult mode active OR
      mode=paused. No tracking-side action, no warnings, no enforcement.
    """

    kind: DecisionKind
    reason: BypassReason | None  # None when kind == "ACT"


def should_act(
    *,
    mode: str,
    adult_mode_until: datetime | None,
    now: datetime,
) -> ActDecision:
    """Pure function — no side effects, no store access.

    Precedence (highest first):
      1. `adult_mode_until > now` → BYPASS (reason: "adult_mode")
      2. mode == "paused"          → BYPASS (reason: "paused")
      3. mode == "monitor_only"    → OBSERVE (reason: "monitor_only")
      4. anything else (including "enforced" or unknown) → ACT (reason: None)

    Caller is responsible for passing TZ-aware UTC datetimes. This function
    does NOT defend against TZ-naive inputs — the `adult_mode_until > now`
    comparison would raise `TypeError`, which is desirable: it surfaces a
    caller bug rather than silently behaving wrong.

    `mode` defaults to ACT for unknown values so a malformed stored
    profile doesn't accidentally disable enforcement on the owner's kids.
    """
    if adult_mode_until is not None and adult_mode_until > now:
        return ActDecision("BYPASS", "adult_mode")
    if mode == "paused":
        return ActDecision("BYPASS", "paused")
    if mode == "monitor_only":
        return ActDecision("OBSERVE", "monitor_only")
    # mode == "enforced" OR unknown mode (defensive default)
    return ActDecision("ACT", None)


def voice_allowed(decision: ActDecision, trigger: str) -> bool:
    """Should voice fire for this trigger given the should_act decision?

    See spec §3.7.

    - Extension grants ALWAYS speak when configured, regardless of mode or
      adult-mode bypass (per PO D8). Rationale: extension is a positive
      parent-initiated action; silence would be misleading — the kid
      wouldn't hear that approval happened. Opt out by setting
      `extension_message=""`.
    - BYPASS (adult mode or paused) → no voice for any system-initiated
      trigger (warning / enforcing / mode_changed / adult_mode_on).
    - OBSERVE (monitor mode) → warnings and countdown cue allowed (caller
      still gates both on `profile.warn_in_monitor_mode` — the calibration
      use case wants the full heads-up sequence so the parent can validate
      thresholds end-to-end); enforce/turn-off voice never fires (nothing
      was enforced).
    - ACT (enforced + no adult mode) → all voices allowed.
    """
    if trigger == "extension_granted":
        return True
    if decision.kind == "BYPASS":
        return False
    if decision.kind == "OBSERVE":
        # Monitor mode: warnings + countdown allowed only if
        # profile.warn_in_monitor_mode — caller checks the flag. The
        # countdown rides on the same flag because it's the same kind of
        # heads-up cue (just 30s before "enforce" instead of N min before).
        # Enforce voice never fires.
        if trigger in ("warning", "countdown"):
            return True
        return False
    # ACT — enforced mode, no adult bypass
    return True
