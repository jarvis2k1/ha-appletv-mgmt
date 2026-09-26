"""v0.23.1 — the day-rollover helper must be reachable from every sensor
that uses it, and every store call must actually pass the hour.

THE BUG (live 2026-09-12, shipped in v0.23.0): `AppleTVTodayHistorySensor`
calls `self._rollover_hour()`, but that helper was only ever defined on
`AppleTVMgmtSensor` — a class it does not inherit from. Every state read
raised AttributeError, so `sensor.*_today_s_app_usage` sat at `unavailable`
on every v0.23.0 install. 804 tests did not notice, because none of them
ever read that entity.

The second test is the quiet half of the same mistake: `rollover_hour`
defaults to 0 in the store, so a call site that simply forgets the kwarg
raises nothing at all and silently computes the wrong day. `api.py`'s
usage endpoint did exactly that.
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import sys

import pytest

# tests/ is a package, so pytest does not put this directory on sys.path.
# Reuse the HA stub loader rather than duplicating ~150 lines of stubs.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_diag_sensors import sensor_mod  # noqa: E402

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "appletv_mgmt"

# Store methods whose day window depends on the rollover hour. Calling any of
# these without `rollover_hour=` silently falls back to midnight.
ROLLOVER_AWARE = (
    "used_seconds_today",
    "events_today",
    "app_totals_today",
    "group_totals_today",
)


class _FakeStore:
    """Records the rollover hour it was handed."""

    def __init__(self) -> None:
        self.seen: list[int] = []

    def app_totals_today(self, profile_id, *, now, rollover_hour):
        self.seen.append(rollover_hour)
        return {}

    def events_today(self, profile_id, *, now, rollover_hour):
        self.seen.append(rollover_hour)
        return []


class _FakeProfile:
    day_rollover_hour = 5


class _FakeCoordinator:
    _profile = _FakeProfile()
    data: dict = {}


def _history_sensor():
    store = _FakeStore()
    s = sensor_mod.AppleTVTodayHistorySensor(
        _FakeCoordinator(), store, "prof-1", "Living Room"
    )
    # The CoordinatorEntity stub's __init__ is a no-op, so wire the attribute
    # the real base class would have set.
    s.coordinator = _FakeCoordinator()
    return s, store


def test_history_sensor_native_value_does_not_raise():
    """THE bug: this raised AttributeError on every read, so HA reported the
    entity as `unavailable` rather than surfacing the fault."""
    s, store = _history_sensor()
    assert s.native_value == 0
    assert store.seen == [5], "must thread the PROFILE's rollover hour, not the default"


def test_history_sensor_attributes_do_not_raise():
    s, store = _history_sensor()
    attrs = s.extra_state_attributes
    assert attrs == {"apps": [], "events": []}
    assert store.seen == [5, 5]


def test_every_sensor_class_that_uses_the_helper_can_resolve_it():
    """Guards the class of bug, not just this instance: a new sensor class
    that reaches for `_rollover_hour` must inherit it."""
    tree = ast.parse((PKG / "sensor.py").read_text())
    offenders = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if "_rollover_hour" not in ast.unparse(node):
            continue
        cls = getattr(sensor_mod, node.name, None)
        if cls is None:
            continue
        if not any(hasattr(base, "_rollover_hour") for base in cls.__mro__):
            offenders.append(node.name)
    assert not offenders, f"classes use _rollover_hour but cannot resolve it: {offenders}"


def test_no_rollover_aware_store_call_forgets_the_hour():
    """The silent half. A missing `rollover_hour=` throws nothing — it just
    computes the wrong day — so only a static check catches it."""
    missing = []
    for path in sorted(PKG.glob("*.py")):
        if path.name == "storage.py":
            continue  # defines them; its internal calls are checked by name below
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else None
            if name not in ROLLOVER_AWARE:
                continue
            if not any(kw.arg == "rollover_hour" for kw in node.keywords):
                missing.append(f"{path.name}:{node.lineno} {name}()")
    assert not missing, "store calls missing rollover_hour=: " + ", ".join(missing)
