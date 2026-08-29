"""Pure enforcement-state logic.

Deliberately has **no** Home Assistant imports so it can be unit-tested
in isolation. Used by `enforcer.EnforcementController`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .const import STATE_ENFORCING, STATE_GRACE, STATE_OK, STATE_WARNING


@dataclass(frozen=True)
class StateDecision:
    state: str
    grace_started_at: datetime | None


def compute_next_state(
    *,
    current_state: str,
    used_seconds: int,
    budget_seconds: int,
    warn_threshold_seconds: int,
    grace_seconds: int,
    grace_started_at: datetime | None,
    now: datetime,
) -> StateDecision:
    """Decide the next enforcement state from current state + usage."""
    remaining = budget_seconds - used_seconds

    if remaining > warn_threshold_seconds:
        return StateDecision(STATE_OK, None)

    if remaining > 0:
        if current_state == STATE_ENFORCING:
            return StateDecision(STATE_OK, None)
        return StateDecision(STATE_WARNING, None)

    # remaining <= 0
    # v0.16.2 — force a WARNING tick when transitioning from OK with
    # remaining already at/below 0. Was previously OK→GRACE direct,
    # which meant the user got NO warning voice when a group budget was
    # already exhausted at app-start, when a quiet window opened mid-
    # session, or when usage overshot the warn threshold in one tick.
    # BA audit 2026-05-28 (Sonnet + Opus convergent: "WARNING permanently
    # skipped when remaining <= 0 fresh from OK"). Costs ~30s before
    # GRACE starts; gains a meaningful heads-up voice to the user.
    if current_state == STATE_OK:
        return StateDecision(STATE_WARNING, None)
    if current_state == STATE_WARNING:
        return StateDecision(STATE_GRACE, now)

    if current_state == STATE_GRACE:
        started = grace_started_at or now
        if (now - started).total_seconds() >= grace_seconds:
            return StateDecision(STATE_ENFORCING, started)
        return StateDecision(STATE_GRACE, started)

    return StateDecision(STATE_ENFORCING, grace_started_at)
