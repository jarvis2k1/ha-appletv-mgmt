"""v0.23.0 — the usage day rolls over at a configurable hour, not midnight.

Live-reported 2026-08-30: a parent watching until ~02:00 had that time charged
to the NEW calendar day, so the children's budget was already partly spent
before they woke up. A household day plainly does not end at midnight.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone

import pytest

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "sched_rollover", ROOT / "custom_components" / "appletv_mgmt" / "schedule.py"
)
sched = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sched)

UTC = timezone.utc
# 2026-08-29 is a Saturday; 2026-08-28 a Friday.
def at(hour, minute=0, day=29):
    return datetime(2026, 8, day, hour, minute, tzinfo=UTC)


def test_late_night_belongs_to_the_evening_it_started():
    """THE bug. 01:30 on Saturday with rollover 05:00 is still Friday's day,
    so a parent's late film does not consume Saturday's budget."""
    assert sched.day_start(at(1, 30), 5) == at(5, 0, day=28)
    assert sched.logical_date(at(1, 30), 5) == datetime(2026, 8, 28).date()
    assert sched.weekday_key(sched.logical_date(at(1, 30), 5)) == "fri"


def test_after_rollover_is_the_new_day():
    assert sched.day_start(at(5, 0), 5) == at(5, 0)
    assert sched.day_start(at(9, 0), 5) == at(5, 0)
    assert sched.logical_date(at(9, 0), 5) == datetime(2026, 8, 29).date()


def test_evening_is_the_same_day_as_the_morning():
    """05:00 and 23:59 on the same date must share one budget window."""
    assert sched.day_start(at(6), 5) == sched.day_start(at(23, 59), 5)


def test_just_before_rollover_still_yesterday():
    assert sched.logical_date(at(4, 59), 5) == datetime(2026, 8, 28).date()
    assert sched.logical_date(at(5, 0), 5) == datetime(2026, 8, 29).date()


def test_midnight_default_reproduces_old_behaviour_exactly():
    """rollover=0 must be a no-op — existing installs must not silently
    re-account their history on upgrade."""
    for h in (0, 1, 5, 12, 23):
        assert sched.day_start(at(h), 0) == at(0, 0)
        assert sched.logical_date(at(h), 0) == datetime(2026, 8, 29).date()


@pytest.mark.parametrize("rollover", list(range(24)))
def test_window_is_always_24h_and_contains_now(rollover):
    """Whatever the rollover, the logical day must be exactly 24h long and
    actually contain the moment being asked about — an off-by-one here would
    either double-count or lose a day's usage."""
    now = at(13, 37)
    start = sched.day_start(now, rollover)
    assert start <= now < start + timedelta(days=1)


def test_weekday_budgets_follow_the_logical_day():
    """Regression for the subtlest half: with a midnight boundary, 02:00 on
    Saturday would pick SATURDAY's budget for Friday-night viewing, applying
    the weekend rules five hours early."""
    naive_weekday = sched.weekday_key(at(2, 0).date())      # what the old code did
    logical_weekday = sched.weekday_key(sched.logical_date(at(2, 0), 5))
    assert naive_weekday == "sat"
    assert logical_weekday == "fri"
    assert naive_weekday != logical_weekday
