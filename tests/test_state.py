"""Unit tests for the pure enforcement state machine.

Covers every transition in the OK -> WARNING -> GRACE -> ENFORCING -> OK
cycle plus the extension-granted shortcut.
"""
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.appletv_mgmt.const import (
    STATE_ENFORCING,
    STATE_GRACE,
    STATE_OK,
    STATE_WARNING,
)
from custom_components.appletv_mgmt.state import compute_next_state

NOW = datetime(2026, 5, 17, 14, 0, 0, tzinfo=timezone.utc)
BUDGET = 60 * 60  # 60 minutes
WARN = 5 * 60     # 5 minutes
GRACE = 60        # 60 seconds


def _decide(*, current, used, warn=WARN, grace=GRACE, grace_started=None, now=NOW):
    return compute_next_state(
        current_state=current,
        used_seconds=used,
        budget_seconds=BUDGET,
        warn_threshold_seconds=warn,
        grace_seconds=grace,
        grace_started_at=grace_started,
        now=now,
    )


def test_ok_when_lots_of_time_left():
    d = _decide(current=STATE_OK, used=10 * 60)
    assert d.state == STATE_OK
    assert d.grace_started_at is None


def test_ok_to_warning_when_inside_warn_window():
    # 56 min used out of 60 -> 4 min remaining, below 5 min warn
    d = _decide(current=STATE_OK, used=56 * 60)
    assert d.state == STATE_WARNING


def test_warning_to_grace_when_budget_exhausted():
    d = _decide(current=STATE_WARNING, used=BUDGET)
    assert d.state == STATE_GRACE
    assert d.grace_started_at == NOW


def test_ok_to_warning_when_budget_exhausted_fresh_from_ok():
    # v0.16.2 — was previously OK→GRACE direct; now OK→WARNING for one
    # tick first, so the warn voice gets a chance to fire (Sonnet+Opus
    # BA audit 2026-05-28: group-budget exhaustion at app-start was
    # silently going straight to GRACE → ENFORCING with no heads-up).
    # E.g. extension expired suddenly, group budget already 0.
    d = _decide(current=STATE_OK, used=BUDGET + 1)
    assert d.state == STATE_WARNING


def test_grace_holds_during_grace_window():
    started = NOW - timedelta(seconds=30)
    d = _decide(current=STATE_GRACE, used=BUDGET + 10, grace_started=started)
    assert d.state == STATE_GRACE
    assert d.grace_started_at == started


def test_grace_to_enforcing_when_window_expires():
    started = NOW - timedelta(seconds=GRACE + 1)
    d = _decide(current=STATE_GRACE, used=BUDGET + 10, grace_started=started)
    assert d.state == STATE_ENFORCING
    assert d.grace_started_at == started


def test_enforcing_to_ok_when_extension_granted():
    # An extension dropped used below budget mid-enforcement.
    d = _decide(current=STATE_ENFORCING, used=30 * 60)
    assert d.state == STATE_OK


def test_warning_to_ok_when_extension_granted():
    d = _decide(current=STATE_WARNING, used=30 * 60)
    assert d.state == STATE_OK


def test_enforcing_stays_when_still_over_budget():
    d = _decide(current=STATE_ENFORCING, used=BUDGET + 100, grace_started=NOW - timedelta(minutes=5))
    assert d.state == STATE_ENFORCING


@pytest.mark.parametrize("state", [STATE_OK, STATE_WARNING, STATE_GRACE, STATE_ENFORCING])
def test_negative_used_seconds_treated_as_ok(state):
    # used < 0 happens when extension_minutes exceeds used so far.
    d = _decide(current=state, used=-300)
    assert d.state == STATE_OK


def test_zero_warn_threshold_skips_warning_state():
    d = _decide(current=STATE_OK, used=BUDGET - 1, warn=0)
    assert d.state == STATE_OK  # still 1 second left, above 0 threshold


def test_used_exactly_at_budget_enters_warning_from_ok():
    # v0.16.2 — fresh from OK transitions through WARNING for one tick
    # (heads-up voice). Next tick will continue to GRACE per the
    # current_state==WARNING branch.
    d = _decide(current=STATE_OK, used=BUDGET)
    assert d.state == STATE_WARNING

    # Confirm: same call but current=WARNING progresses to GRACE.
    d2 = _decide(current=STATE_WARNING, used=BUDGET)
    assert d2.state == STATE_GRACE
