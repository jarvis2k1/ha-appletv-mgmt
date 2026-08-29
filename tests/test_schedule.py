"""Unit tests for the per-weekday schedule resolver (v0.11.0)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from custom_components.appletv_mgmt.schedule import (
    WEEKDAYS,
    WEEKEND,
    effective_daily_budget,
    effective_group_budgets,
    effective_quiet_windows_string,
    normalize_weekday_dict,
    normalize_weekday_group_dict,
    weekday_key,
)


# ---------- weekday_key ----------------------------------------------------


@pytest.mark.parametrize(
    "iso,expected",
    [
        ("2026-05-18", "mon"),  # Monday
        ("2026-05-19", "tue"),
        ("2026-05-20", "wed"),
        ("2026-05-21", "thu"),
        ("2026-05-22", "fri"),
        ("2026-05-23", "sat"),
        ("2026-05-24", "sun"),
    ],
)
def test_weekday_key_for_iso_date(iso: str, expected: str):
    assert weekday_key(date.fromisoformat(iso)) == expected


def test_weekday_key_accepts_datetime():
    dt = datetime(2026, 5, 24, 23, 30, tzinfo=timezone.utc)
    assert weekday_key(dt) == "sun"


def test_weekdays_constant_order():
    assert WEEKDAYS == ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def test_weekend_set():
    assert WEEKEND == frozenset({"sat", "sun"})


# ---------- effective_daily_budget ----------------------------------------


def test_daily_budget_default_when_no_overrides():
    out = effective_daily_budget(
        base_min=60, weekday_overrides={}, on=date(2026, 5, 18)
    )
    assert out == 60


def test_daily_budget_override_for_specific_day():
    out = effective_daily_budget(
        base_min=60,
        weekday_overrides={"sat": 120, "sun": 90},
        on=date(2026, 5, 23),  # Saturday
    )
    assert out == 120


def test_daily_budget_falls_back_when_day_not_overridden():
    out = effective_daily_budget(
        base_min=60,
        weekday_overrides={"sat": 120},
        on=date(2026, 5, 18),  # Monday — not in overrides
    )
    assert out == 60


def test_daily_budget_clamps_negative_to_zero():
    out = effective_daily_budget(
        base_min=60,
        weekday_overrides={"sat": -5},
        on=date(2026, 5, 23),
    )
    assert out == 0


def test_daily_budget_ignores_bad_value_in_override():
    out = effective_daily_budget(
        base_min=60,
        weekday_overrides={"sat": "not-a-number"},  # type: ignore[dict-item]
        on=date(2026, 5, 23),
    )
    assert out == 60


# ---------- effective_group_budgets ----------------------------------------


def test_group_budgets_returns_base_when_no_overrides():
    out = effective_group_budgets(
        base_groups={"movies": 60, "gaming": 30},
        weekday_group_overrides={},
        on=date(2026, 5, 18),
    )
    assert out == {"movies": 60, "gaming": 30}


def test_group_budgets_override_only_named_group():
    """A weekend override of 'gaming' shouldn't affect 'movies'."""
    out = effective_group_budgets(
        base_groups={"movies": 60, "gaming": 30},
        weekday_group_overrides={"sat": {"gaming": 60}},
        on=date(2026, 5, 23),  # Saturday
    )
    assert out == {"movies": 60, "gaming": 60}


def test_group_budgets_override_can_add_group():
    """An override CAN introduce a new group not in the base map."""
    out = effective_group_budgets(
        base_groups={"movies": 60},
        weekday_group_overrides={"sat": {"gaming": 90}},
        on=date(2026, 5, 23),
    )
    assert out == {"movies": 60, "gaming": 90}


def test_group_budgets_weekday_not_in_overrides():
    out = effective_group_budgets(
        base_groups={"movies": 60},
        weekday_group_overrides={"sat": {"movies": 120}},
        on=date(2026, 5, 18),  # Monday — no override
    )
    assert out == {"movies": 60}


# ---------- effective_quiet_windows_string ---------------------------------


def test_quiet_windows_default_when_no_override():
    out = effective_quiet_windows_string(
        base_string="20:30-07:00:Bedtime",
        weekday_overrides={},
        on=date(2026, 5, 18),
    )
    assert out == "20:30-07:00:Bedtime"


def test_quiet_windows_per_day_override_replaces_base():
    out = effective_quiet_windows_string(
        base_string="20:30-07:00:Bedtime",
        weekday_overrides={"sat": "23:00-09:00:WeekendBed"},
        on=date(2026, 5, 23),
    )
    assert out == "23:00-09:00:WeekendBed"


def test_quiet_windows_empty_override_disables_quiet_for_that_day():
    out = effective_quiet_windows_string(
        base_string="20:30-07:00:Bedtime",
        weekday_overrides={"sat": ""},
        on=date(2026, 5, 23),
    )
    assert out == ""


# ---------- normalize_weekday_dict ----------------------------------------


def test_normalize_weekday_dict_passes_valid_input():
    out = normalize_weekday_dict({"sat": 120, "sun": 90})
    assert out == {"sat": 120, "sun": 90}


def test_normalize_weekday_dict_rejects_unknown_weekday():
    with pytest.raises(ValueError, match="unknown weekday"):
        normalize_weekday_dict({"funday": 60})


def test_normalize_weekday_dict_handles_none_input():
    assert normalize_weekday_dict(None) == {}


def test_normalize_weekday_dict_returns_stable_order():
    """Result keys should appear in WEEKDAYS order regardless of input order."""
    out = normalize_weekday_dict({"sun": 90, "mon": 60, "fri": 75})
    assert list(out.keys()) == ["mon", "fri", "sun"]


def test_normalize_weekday_dict_decoder_raises_on_bad_value():
    with pytest.raises((ValueError, TypeError)):
        normalize_weekday_dict({"mon": "not-int"})


# ---------- normalize_weekday_group_dict ----------------------------------


def test_normalize_weekday_group_dict_valid():
    out = normalize_weekday_group_dict({"sat": {"gaming": 60, "movies": 90}})
    assert out == {"sat": {"gaming": 60, "movies": 90}}


def test_normalize_weekday_group_dict_rejects_bad_minute_value():
    with pytest.raises(ValueError, match="minutes must be int"):
        normalize_weekday_group_dict({"sat": {"gaming": "lots"}})


def test_normalize_weekday_group_dict_rejects_out_of_range():
    with pytest.raises(ValueError, match="out of range"):
        normalize_weekday_group_dict({"sat": {"gaming": 5000}})


def test_normalize_weekday_group_dict_handles_none():
    assert normalize_weekday_group_dict(None) == {}
