"""Unit tests for the quiet-windows module.

Covers parsing (single + multi, with/without labels), the same-day vs
crosses-midnight `contains()` logic, find_active_window picking the first
match, and the malformed-input failure paths.
"""
from datetime import datetime, time, timezone

import pytest

from custom_components.appletv_mgmt.quiet import (
    QuietWindow,
    find_active_window,
    parse_windows,
    validate_windows_string,
    windows_to_string,
)


# ---------- parsing ---------------------------------------------------------


def test_parse_simple_window():
    w = QuietWindow.parse("20:30-07:00")
    assert w.start == time(20, 30)
    assert w.end == time(7, 0)
    assert w.label == ""


def test_parse_window_with_label():
    w = QuietWindow.parse("20:30-07:00:Bedtime")
    assert w.label == "Bedtime"


def test_parse_window_label_may_contain_colon_separators_after_HH_MM():
    # Anything past the second colon is the label, even if it has more colons.
    w = QuietWindow.parse("20:30-07:00:Quiet hours: Sleep")
    assert w.label == "Quiet hours: Sleep"


def test_parse_window_trims_whitespace():
    w = QuietWindow.parse("  20:30 -  07:00 : Bedtime ")
    assert w.start == time(20, 30)
    assert w.end == time(7, 0)
    assert w.label == "Bedtime"


def test_parse_multi_windows():
    ws = parse_windows("12:00-14:00:Lunch, 20:30-07:00:Bedtime")
    assert len(ws) == 2
    assert ws[0].label == "Lunch"
    assert ws[1].label == "Bedtime"


def test_parse_empty_or_blank_returns_empty_list():
    assert parse_windows("") == []
    assert parse_windows("   ") == []
    assert parse_windows(None) == []


def test_parse_trailing_comma_is_forgiving():
    ws = parse_windows("20:30-07:00:Bedtime,")
    assert len(ws) == 1


@pytest.mark.parametrize(
    "bad",
    [
        "not a window",
        "20:30",          # no dash
        "20:30-",         # missing end
        "20:30-25:00",    # invalid hour
        "20:30-07",       # end missing minutes
        ":",
    ],
)
def test_parse_window_rejects_malformed(bad):
    with pytest.raises(ValueError):
        QuietWindow.parse(bad)


def test_validate_windows_string_propagates_errors():
    validate_windows_string("20:30-07:00, 12:00-13:00")  # ok
    with pytest.raises(ValueError):
        validate_windows_string("20:30-07:00, garbage")


# ---------- contains() ------------------------------------------------------


def test_contains_same_day_window():
    w = QuietWindow(start=time(12, 0), end=time(14, 0))
    assert not w.contains(time(11, 59))
    assert w.contains(time(12, 0))     # inclusive start
    assert w.contains(time(13, 0))
    assert not w.contains(time(14, 0)) # exclusive end
    assert not w.contains(time(20, 0))


def test_contains_crosses_midnight_window():
    w = QuietWindow(start=time(20, 30), end=time(7, 0))
    assert w.contains(time(20, 30))    # inclusive start
    assert w.contains(time(23, 59))    # after start, before midnight
    assert w.contains(time(0, 0))      # exactly midnight
    assert w.contains(time(6, 59))     # before end
    assert not w.contains(time(7, 0))  # exclusive end
    assert not w.contains(time(8, 0))
    assert not w.contains(time(19, 0))


def test_contains_zero_length_window_is_never_active():
    w = QuietWindow(start=time(20, 0), end=time(20, 0))
    assert not w.contains(time(20, 0))
    assert not w.contains(time(0, 0))


def test_contains_midnight_boundary_window():
    # 00:00 to 06:00 — same-day window starting exactly at midnight.
    w = QuietWindow(start=time(0, 0), end=time(6, 0))
    assert w.contains(time(0, 0))
    assert w.contains(time(5, 59))
    assert not w.contains(time(6, 0))


# ---------- find_active_window ---------------------------------------------


NOON = datetime(2026, 5, 17, 12, 30, tzinfo=timezone.utc)
EVENING = datetime(2026, 5, 17, 21, 0, tzinfo=timezone.utc)
EARLY_AM = datetime(2026, 5, 17, 3, 0, tzinfo=timezone.utc)


def _windows():
    return [
        QuietWindow(start=time(12, 0), end=time(14, 0), label="Lunch"),
        QuietWindow(start=time(20, 30), end=time(7, 0), label="Bedtime"),
    ]


def test_find_active_window_picks_first_matching():
    w = find_active_window(_windows(), NOON)
    assert w is not None and w.label == "Lunch"


def test_find_active_window_handles_crosses_midnight():
    assert find_active_window(_windows(), EVENING).label == "Bedtime"
    assert find_active_window(_windows(), EARLY_AM).label == "Bedtime"


def test_find_active_window_returns_none_when_outside_all():
    midmorning = datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc)
    assert find_active_window(_windows(), midmorning) is None


def test_find_active_window_empty_list_returns_none():
    assert find_active_window([], NOON) is None


# ---------- round-trip -----------------------------------------------------


def test_windows_to_string_roundtrip():
    raw = "20:30-07:00:Bedtime, 12:00-14:00:Lunch"
    parsed = parse_windows(raw)
    again = parse_windows(windows_to_string(parsed))
    assert again == parsed


def test_windows_to_string_omits_empty_label():
    raw = "20:30-07:00, 12:00-14:00"
    parsed = parse_windows(raw)
    assert windows_to_string(parsed) == "20:30-07:00, 12:00-14:00"
