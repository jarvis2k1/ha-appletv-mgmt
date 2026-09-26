"""Tests for the v0.15.0 audit `actor` field + payload-key fallback.

Loads storage directly (no HA stack needed for the dataclass round-trip)
and exercises:
  - ActionLogEntry.to_dict + from_dict round-trips `actor`
  - from_dict on legacy rows (no `actor` key) defaults to None
  - record_action accepts `actor` kwarg and persists it
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_storage_module():
    """Import storage with HA stubs (same trick as test_storage_helpers)."""
    for mod_name in (
        "homeassistant",
        "homeassistant.core",
        "homeassistant.helpers",
        "homeassistant.helpers.storage",
        "homeassistant.util",
        "homeassistant.util.dt",
    ):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)
    hass_core = sys.modules["homeassistant.core"]
    if not hasattr(hass_core, "HomeAssistant"):
        hass_core.HomeAssistant = type("HomeAssistant", (), {})
    storage_mod = sys.modules["homeassistant.helpers.storage"]
    if not hasattr(storage_mod, "Store"):
        class _Store:
            def __init__(self, *a, **kw): pass
            async def async_load(self): return None
            async def async_save(self, data): pass
        storage_mod.Store = _Store
    dt_mod = sys.modules["homeassistant.util.dt"]
    if not hasattr(dt_mod, "utcnow"):
        dt_mod.utcnow = lambda: datetime.now(timezone.utc)
        dt_mod.as_local = lambda d: d
        dt_mod.as_utc = lambda d: d

    import importlib.util

    pkg_path = ROOT / "custom_components" / "appletv_mgmt"
    if "custom_components.appletv_mgmt.const" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "custom_components.appletv_mgmt.const", pkg_path / "const.py"
        )
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        spec.loader.exec_module(m)
    spec = importlib.util.spec_from_file_location(
        "custom_components.appletv_mgmt.storage", pkg_path / "storage.py"
    )
    storage = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = storage
    spec.loader.exec_module(storage)
    return storage


storage = _load_storage_module()


def _make_store():
    s = storage.AppleTVMgmtStore.__new__(storage.AppleTVMgmtStore)
    s._profiles = {}
    s._events = []
    s._extensions_granted_today = {}
    s._app_categories = {}
    s._adult_mode_until = {}
    s._requests = {}
    s._actions = []
    return s


# ---------- actor field round-trip ------------------------------------------


def test_action_log_entry_to_dict_includes_actor():
    e = storage.ActionLogEntry(
        id="act_1",
        profile_id="p1",
        at=datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
        action="mode_changed",
        actor="select_entity",
    )
    d = e.to_dict()
    assert d["actor"] == "select_entity"


def test_action_log_entry_from_dict_round_trips_actor():
    src = storage.ActionLogEntry(
        id="act_2",
        profile_id="p1",
        at=datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
        action="adult_mode_on",
        actor="rest",
    )
    rt = storage.ActionLogEntry.from_dict(src.to_dict())
    assert rt.actor == "rest"


def test_action_log_entry_from_dict_legacy_row_defaults_actor_none():
    """Old persisted rows (pre-v0.15.0) have no `actor` key — must not crash."""
    raw = {
        "id": "act_legacy",
        "profile_id": "p1",
        "at": "2026-05-25T12:00:00+00:00",
        "action": "enforce_start",
        "reason": "daily_limit",
        "detail": "daily budget reached",
        "count": 1,
        "bundle_id": None,
        # NO `actor` key
    }
    rt = storage.ActionLogEntry.from_dict(raw)
    assert rt.actor is None


def test_record_action_accepts_actor_kwarg():
    s = _make_store()
    e = s.record_action(
        profile_id="p1",
        action="mode_changed",
        detail="enforced → paused",
        actor="select_entity",
    )
    assert e.actor == "select_entity"
    assert s._actions[-1].actor == "select_entity"


def test_record_action_actor_default_none():
    """When caller doesn't pass `actor`, the field defaults to None."""
    s = _make_store()
    e = s.record_action(profile_id="p1", action="enforce_start", reason="daily_limit")
    assert e.actor is None


def test_record_action_coalesced_bypass_carries_actor():
    s = _make_store()
    now = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    from datetime import timedelta

    for i in range(3):
        s.record_action(
            profile_id="p1",
            action="enforce_start",
            at=now + timedelta(seconds=i * 30),
            actor="system",
        )
    # Coalesced bypass entry should carry the last actor.
    assert len(s._actions) == 1
    assert s._actions[0].action == "bypass_attempt"
    assert s._actions[0].actor == "system"


# ---------- TZ-aware assert on set_adult_mode_until -------------------------


def test_set_adult_mode_until_rejects_naive_datetime():
    s = _make_store()
    naive = datetime(2026, 5, 25, 13, 0)  # no tzinfo
    with pytest.raises(AssertionError, match="TZ-aware"):
        s.set_adult_mode_until("p1", naive)


def test_set_adult_mode_until_accepts_tz_aware():
    s = _make_store()
    aware = datetime(2026, 5, 25, 13, 0, tzinfo=timezone.utc)
    s.set_adult_mode_until("p1", aware)  # must not raise
    assert s.adult_mode_until("p1") == aware


def test_set_adult_mode_until_none_clears():
    s = _make_store()
    aware = datetime(2026, 5, 25, 13, 0, tzinfo=timezone.utc)
    s.set_adult_mode_until("p1", aware)
    s.set_adult_mode_until("p1", None)
    assert s.adult_mode_until("p1") is None


# ---------- is_adult_mode_active_at purity ----------------------------------


def test_is_adult_mode_active_at_does_not_mutate_on_expiry():
    """The new pure accessor must not pop expired entries."""
    s = _make_store()
    past = datetime(2026, 5, 25, 11, 0, tzinfo=timezone.utc)
    now = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    s.set_adult_mode_until("p1", past)
    assert s.is_adult_mode_active_at("p1", now) is False
    # entry NOT auto-popped — that's the legacy is_adult_mode_active's job.
    assert s.adult_mode_until("p1") == past


def test_is_adult_mode_active_at_future_returns_true():
    s = _make_store()
    future = datetime(2026, 5, 25, 13, 0, tzinfo=timezone.utc)
    now = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    s.set_adult_mode_until("p1", future)
    assert s.is_adult_mode_active_at("p1", now) is True


def test_is_adult_mode_active_at_no_entry_returns_false():
    s = _make_store()
    now = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    assert s.is_adult_mode_active_at("p1", now) is False


# ---------- purge_expired_adult_mode mutates --------------------------------


def test_purge_expired_adult_mode_drops_expired_entries():
    s = _make_store()
    past = datetime(2026, 5, 25, 11, 0, tzinfo=timezone.utc)
    future = datetime(2026, 5, 25, 13, 0, tzinfo=timezone.utc)
    now = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    s.set_adult_mode_until("p1", past)
    s.set_adult_mode_until("p2", future)
    dropped = s.purge_expired_adult_mode(now=now)
    assert dropped == ["p1"]
    assert s.adult_mode_until("p1") is None
    assert s.adult_mode_until("p2") == future


def test_purge_expired_adult_mode_no_op_when_nothing_expired():
    s = _make_store()
    future = datetime(2026, 5, 25, 13, 0, tzinfo=timezone.utc)
    now = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    s.set_adult_mode_until("p1", future)
    dropped = s.purge_expired_adult_mode(now=now)
    assert dropped == []
    assert s.adult_mode_until("p1") == future
