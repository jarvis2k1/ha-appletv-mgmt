"""Unit tests for the pure helpers in storage.py.

The full `AppleTVMgmtStore` requires HA's `Store` helper — too heavy for
this dev install (we skip `pytest-homeassistant-custom-component`). But
the v0.10.0 load-resilience helpers (`_safe_decode_list`,
`_safe_decode_map`) and `prune_old_requests` are pure-Python and can be
exercised by stubbing out the HA imports.

The stub trick is in `_load_storage_module()`: we register sentinel
modules for `homeassistant.*` so the real storage module imports
without ever touching HA.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_storage_module():
    """Import custom_components.appletv_mgmt.storage with HA stubs."""
    # Stub the homeassistant modules the storage module needs at import time.
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

    # Provide the symbols storage.py imports.
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

    # Now import the real module.
    import importlib.util

    pkg_path = ROOT / "custom_components" / "appletv_mgmt"
    # The const module is pure — load it first under the right name.
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


# ---------- _safe_decode_list ----------------------------------------------


def test_safe_decode_list_passes_through_valid_records():
    items = [{"x": 1}, {"x": 2}, {"x": 3}]
    out = storage._safe_decode_list(items, decode=lambda r: r["x"], label="test")
    assert out == [1, 2, 3]


def test_safe_decode_list_skips_bad_records_with_logging():
    items = [{"x": 1}, {"oops": True}, {"x": 3}]
    out = storage._safe_decode_list(items, decode=lambda r: r["x"], label="test")
    assert out == [1, 3]


def test_safe_decode_list_handles_empty_input():
    out = storage._safe_decode_list([], decode=lambda r: r, label="any")
    assert out == []


def test_safe_decode_list_skips_when_decode_raises_value_error():
    def decoder(r):
        if "bad" in r:
            raise ValueError("malformed")
        return r["good"]
    out = storage._safe_decode_list(
        [{"good": "a"}, {"bad": True}, {"good": "c"}],
        decode=decoder,
        label="event",
    )
    assert out == ["a", "c"]


# ---------- _safe_decode_map -----------------------------------------------


def test_safe_decode_map_builds_dict():
    items = [{"id": "a", "v": 1}, {"id": "b", "v": 2}]
    out = storage._safe_decode_map(
        items, key=lambda r: r["id"], decode=lambda r: r["v"], label="t"
    )
    assert out == {"a": 1, "b": 2}


def test_safe_decode_map_skips_records_missing_key():
    items = [{"id": "a"}, {"noid": True}, {"id": "c"}]
    out = storage._safe_decode_map(
        items, key=lambda r: r["id"], decode=lambda r: r, label="t"
    )
    assert set(out.keys()) == {"a", "c"}


# ---------- expire_old_requests + prune_old_requests -----------------------


def _make_store():
    """Construct a bare AppleTVMgmtStore without invoking HA's Store."""
    s = storage.AppleTVMgmtStore.__new__(storage.AppleTVMgmtStore)
    s._profiles = {}
    s._events = []
    s._extensions_granted_today = {}
    s._app_categories = {}
    s._adult_mode_until = {}
    s._requests = {}
    s._actions = []  # v0.12.0
    return s


def _req(rid: str, status: str, requested_at: datetime, decided_at: datetime | None = None,
         auto_expires_at: datetime | None = None):
    return storage.ExtensionRequest(
        id=rid,
        profile_id="p1",
        requested_minutes=15,
        reason="",
        requested_at=requested_at,
        auto_expires_at=auto_expires_at or (requested_at + timedelta(minutes=10)),
        status=status,
        decided_at=decided_at,
    )


def test_expire_old_requests_marks_past_pending_as_expired():
    s = _make_store()
    now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    old = _req("req-old", "pending", requested_at=now - timedelta(hours=2),
               auto_expires_at=now - timedelta(hours=1))
    fresh = _req("req-fresh", "pending", requested_at=now,
                 auto_expires_at=now + timedelta(minutes=10))
    s._requests = {old.id: old, fresh.id: fresh}

    expired = s.expire_old_requests(now=now)
    assert [r.id for r in expired] == ["req-old"]
    assert s._requests["req-old"].status == "expired"
    assert s._requests["req-old"].decided_at == now
    assert s._requests["req-fresh"].status == "pending"


def test_expire_old_requests_ignores_already_decided():
    s = _make_store()
    now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    approved = _req("req-a", "approved", requested_at=now - timedelta(hours=2),
                    decided_at=now - timedelta(minutes=30),
                    auto_expires_at=now - timedelta(hours=1))
    s._requests = {approved.id: approved}
    assert s.expire_old_requests(now=now) == []
    assert s._requests["req-a"].status == "approved"


def test_prune_old_requests_drops_decided_beyond_retention():
    s = _make_store()
    now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    old_decided = _req("old", "approved", requested_at=now - timedelta(days=45),
                       decided_at=now - timedelta(days=44))
    recent_decided = _req("recent", "approved", requested_at=now - timedelta(days=5),
                          decided_at=now - timedelta(days=5))
    pending = _req("pending", "pending", requested_at=now - timedelta(days=90))
    s._requests = {old_decided.id: old_decided, recent_decided.id: recent_decided,
                   pending.id: pending}

    removed = s.prune_old_requests(retain_days=30, now=now)
    assert removed == 1
    assert "old" not in s._requests
    assert "recent" in s._requests
    assert "pending" in s._requests, "pending must never be pruned"


def test_prune_old_requests_never_removes_pending():
    """Pending requests must survive prune even if they're decades old."""
    s = _make_store()
    now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    ancient_pending = _req("ancient", "pending", requested_at=now - timedelta(days=3650))
    s._requests = {ancient_pending.id: ancient_pending}
    assert s.prune_old_requests(retain_days=30, now=now) == 0
    assert "ancient" in s._requests


# ---------- v0.12.0 action log ----------


def test_record_action_appends_entry():
    s = _make_store()
    e = s.record_action(profile_id="p1", action="enforce_start",
                        reason="daily_limit", detail="daily budget reached")
    assert e.profile_id == "p1"
    assert e.action == "enforce_start"
    assert e.reason == "daily_limit"
    assert e.count == 1
    assert s._actions == [e]


def test_record_action_assigns_unique_ids():
    s = _make_store()
    ids = {s.record_action(profile_id="p1", action="warn").id for _ in range(20)}
    assert len(ids) == 20, "every record should have a distinct id"


def test_record_action_coalesces_bypass():
    """3+ enforce_start within 5 min → coalesced into one bypass_attempt."""
    s = _make_store()
    now = datetime(2026, 5, 22, 7, 15, 0, tzinfo=timezone.utc)
    e1 = s.record_action(profile_id="p1", action="enforce_start",
                         reason="daily_limit", at=now)
    e2 = s.record_action(profile_id="p1", action="enforce_start",
                         reason="daily_limit", at=now + timedelta(seconds=30))
    e3 = s.record_action(profile_id="p1", action="enforce_start",
                         reason="daily_limit", at=now + timedelta(seconds=60))
    # After the 3rd call there should be only ONE retained entry: the
    # coalesced bypass_attempt with count=3.
    assert len(s._actions) == 1
    only = s._actions[0]
    assert only.action == "bypass_attempt"
    assert only.count == 3
    # And a 4th attempt keeps incrementing count.
    s.record_action(profile_id="p1", action="enforce_start",
                    reason="daily_limit", at=now + timedelta(seconds=90))
    assert len(s._actions) == 1
    assert s._actions[0].count == 4


def test_record_action_does_not_coalesce_across_profiles():
    s = _make_store()
    now = datetime(2026, 5, 22, 7, 15, 0, tzinfo=timezone.utc)
    for _ in range(3):
        s.record_action(profile_id="p1", action="enforce_start", at=now)
    for _ in range(3):
        s.record_action(profile_id="p2", action="enforce_start", at=now)
    # Each profile gets its own coalesced bypass entry.
    actions_p1 = [a for a in s._actions if a.profile_id == "p1"]
    actions_p2 = [a for a in s._actions if a.profile_id == "p2"]
    assert len(actions_p1) == 1 and actions_p1[0].action == "bypass_attempt"
    assert len(actions_p2) == 1 and actions_p2[0].action == "bypass_attempt"


def test_record_action_does_not_coalesce_after_window_expires():
    """Enforce → wait 10 min → enforce: that's a fresh enforce_start, not coalesced."""
    s = _make_store()
    now = datetime(2026, 5, 22, 7, 0, 0, tzinfo=timezone.utc)
    for i in range(3):
        s.record_action(profile_id="p1", action="enforce_start", at=now + timedelta(seconds=i*30))
    # 10 minutes later → new isolated enforce_start
    later = now + timedelta(minutes=10)
    s.record_action(profile_id="p1", action="enforce_start", at=later)
    assert len(s._actions) == 2
    assert s._actions[0].action == "bypass_attempt"
    assert s._actions[1].action == "enforce_start"


def test_actions_for_profile_newest_first_with_limit():
    s = _make_store()
    base = datetime(2026, 5, 22, 8, 0, 0, tzinfo=timezone.utc)
    for i in range(10):
        s.record_action(profile_id="p1", action="warn",
                        at=base + timedelta(minutes=i))
    rows = s.actions_for_profile("p1", limit=3)
    assert len(rows) == 3
    # Newest first.
    assert rows[0].at > rows[1].at > rows[2].at


def test_actions_since_until_filter():
    s = _make_store()
    base = datetime(2026, 5, 22, 0, 0, 0, tzinfo=timezone.utc)
    for i in range(5):
        s.record_action(profile_id="p1", action="warn",
                        at=base + timedelta(hours=i*6))
    since = base + timedelta(hours=6)
    until = base + timedelta(hours=18)
    rows = s.actions_for_profile("p1", since=since, until=until, limit=50)
    assert len(rows) == 3  # hours 6, 12, 18


def test_prune_actions_drops_old_entries():
    s = _make_store()
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    fresh = s.record_action(profile_id="p1", action="warn", at=now - timedelta(days=30))
    old = s.record_action(profile_id="p1", action="warn", at=now - timedelta(days=95))
    removed = s._prune_actions(now=now)
    assert removed == 1
    assert old not in s._actions
    assert fresh in s._actions


def test_action_log_round_trips_via_dict():
    storage = _load_storage_module()
    ActionLogEntry = storage.ActionLogEntry
    now = datetime(2026, 5, 22, 9, 15, 0, tzinfo=timezone.utc)
    src = ActionLogEntry(
        id="act_1",
        profile_id="p1",
        at=now,
        action="bypass_attempt",
        reason="daily_limit",
        detail="daily budget reached",
        count=5,
        bundle_id="com.netflix.Netflix",
    )
    rt = ActionLogEntry.from_dict(src.to_dict())
    assert rt.id == src.id
    assert rt.action == "bypass_attempt"
    assert rt.count == 5
    assert rt.at == now
    assert rt.bundle_id == "com.netflix.Netflix"


# ---------- v0.14.0 monitor mode ----------


def test_profile_enforcement_enabled_defaults_true():
    """Existing installs (no enforcement_enabled in storage) must keep
    enforcing — opt-in to monitor, not opt-out."""
    storage_mod = _load_storage_module()
    p = storage_mod.Profile(
        id="p1", display_name="Living Room",
        apple_tv_entity_id="media_player.living_room_apple_tv",
        adguard_client_name="AppleTV",
        daily_budget_min=60, grace_seconds=60,
        warn_thresholds_min=[5], idle_grace_minutes=5,
    )
    assert p.enforcement_enabled is True


def test_profile_enforcement_enabled_round_trip():
    """Round-trip with ee=False + mode=monitor_only (the v0.15.0 canonical pair)."""
    storage_mod = _load_storage_module()
    p = storage_mod.Profile(
        id="p1", display_name="Living Room",
        apple_tv_entity_id="media_player.living_room_apple_tv",
        adguard_client_name="AppleTV",
        daily_budget_min=60, grace_seconds=60,
        warn_thresholds_min=[5], idle_grace_minutes=5,
        enforcement_enabled=False,
        mode="monitor_only",  # v0.15.0: explicit mode required to round-trip ee=False
    )
    d = p.to_dict()
    assert d["enforcement_enabled"] is False
    assert d["mode"] == "monitor_only"
    rt = storage_mod.Profile.from_dict(d)
    assert rt.enforcement_enabled is False
    assert rt.mode == "monitor_only"


def test_profile_from_dict_missing_enforcement_enabled_defaults_true():
    """Stored data from pre-v0.14.0 lacks the field; load must not crash
    and must default to True."""
    storage_mod = _load_storage_module()
    raw = {
        "id": "p1", "display_name": "Living Room",
        "apple_tv_entity_id": "media_player.living_room_apple_tv",
        "adguard_client_name": "AppleTV",
        "daily_budget_min": 60, "grace_seconds": 60,
        "warn_thresholds_min": [5], "idle_grace_minutes": 5,
        # NO enforcement_enabled key
    }
    p = storage_mod.Profile.from_dict(raw)
    assert p.enforcement_enabled is True


# ---------- v0.15.0 mode redesign — new field defaults + round-trip ----------


def _base_profile_raw() -> dict:
    return {
        "id": "p1", "display_name": "Living Room",
        "apple_tv_entity_id": "media_player.living_room_apple_tv",
        "adguard_client_name": "AppleTV",
        "daily_budget_min": 60, "grace_seconds": 60,
        "warn_thresholds_min": [5], "idle_grace_minutes": 5,
    }


def test_profile_new_v015_fields_default():
    storage_mod = _load_storage_module()
    p = storage_mod.Profile(
        id="p1", display_name="Living Room",
        apple_tv_entity_id="media_player.living_room_apple_tv",
        adguard_client_name="AppleTV",
        daily_budget_min=60, grace_seconds=60,
        warn_thresholds_min=[5], idle_grace_minutes=5,
    )
    assert p.mode == "enforced"
    assert p.tv_shutdown_target is None
    assert p.warn_in_monitor_mode is False
    assert p.voice_on_mode_change is False
    assert p.adult_mode_on_message == ""
    assert p.mode_change_message == ""


def test_profile_new_v015_fields_round_trip():
    storage_mod = _load_storage_module()
    p = storage_mod.Profile(
        id="p1", display_name="Living Room",
        apple_tv_entity_id="media_player.living_room_apple_tv",
        adguard_client_name="AppleTV",
        daily_budget_min=60, grace_seconds=60,
        warn_thresholds_min=[5], idle_grace_minutes=5,
        mode="paused",
        tv_shutdown_target="media_player.tv",
        warn_in_monitor_mode=True,
        voice_on_mode_change=True,
        adult_mode_on_message="Adult mode for {duration_minutes} min",
        mode_change_message="Mode changed: {old_mode} → {new_mode}",
    )
    rt = storage_mod.Profile.from_dict(p.to_dict())
    assert rt.mode == "paused"
    assert rt.tv_shutdown_target == "media_player.tv"
    assert rt.warn_in_monitor_mode is True
    assert rt.voice_on_mode_change is True
    assert rt.adult_mode_on_message == "Adult mode for {duration_minutes} min"
    assert rt.mode_change_message == "Mode changed: {old_mode} → {new_mode}"


# ---------- 4-way mode reconciliation in from_dict (spec §4.2 Step 1+2) ----------


def test_from_dict_migration_ee_false_no_mode_yields_monitor_only():
    """Pre-v0.15.0 install with enforcement disabled → mode=monitor_only."""
    storage_mod = _load_storage_module()
    raw = {**_base_profile_raw(), "enforcement_enabled": False}
    p = storage_mod.Profile.from_dict(raw)
    assert p.mode == "monitor_only"
    assert p.enforcement_enabled is False


def test_from_dict_migration_ee_true_no_mode_yields_enforced():
    """Pre-v0.15.0 install with enforcement enabled → mode=enforced."""
    storage_mod = _load_storage_module()
    raw = {**_base_profile_raw(), "enforcement_enabled": True}
    p = storage_mod.Profile.from_dict(raw)
    assert p.mode == "enforced"
    assert p.enforcement_enabled is True


def test_from_dict_migration_no_ee_no_mode_yields_enforced():
    """Pre-v0.14.0 install lacking enforcement_enabled entirely → mode=enforced (default)."""
    storage_mod = _load_storage_module()
    raw = _base_profile_raw()  # no enforcement_enabled key at all
    p = storage_mod.Profile.from_dict(raw)
    assert p.mode == "enforced"
    assert p.enforcement_enabled is True


def test_from_dict_migration_mode_wins_over_disagreeing_ee():
    """Both fields present but disagree → mode wins, enforcement_enabled reconciled."""
    storage_mod = _load_storage_module()
    raw = {**_base_profile_raw(), "mode": "monitor_only", "enforcement_enabled": True}
    p = storage_mod.Profile.from_dict(raw)
    assert p.mode == "monitor_only"
    # Reconciled to match mode
    assert p.enforcement_enabled is False


def test_from_dict_migration_mode_and_ee_already_agree_no_change():
    """Both fields present + agreeing → no change."""
    storage_mod = _load_storage_module()
    raw = {**_base_profile_raw(), "mode": "enforced", "enforcement_enabled": True}
    p = storage_mod.Profile.from_dict(raw)
    assert p.mode == "enforced"
    assert p.enforcement_enabled is True


def test_from_dict_migration_mode_paused_reconciles_ee_to_false():
    """mode=paused implies ee=False after reconciliation."""
    storage_mod = _load_storage_module()
    raw = {**_base_profile_raw(), "mode": "paused", "enforcement_enabled": True}
    p = storage_mod.Profile.from_dict(raw)
    assert p.mode == "paused"
    assert p.enforcement_enabled is False


# ---------- tv_shutdown migration matrix (spec §3.5 — 4 rows) ----------


def test_from_dict_tv_shutdown_enabled_with_entity_migrates_to_target():
    """Row 1: enabled=True + entity set → tv_shutdown_target = entity."""
    storage_mod = _load_storage_module()
    raw = {
        **_base_profile_raw(),
        "tv_shutdown_enabled": True,
        "tv_entity_id": "media_player.tv",
    }
    p = storage_mod.Profile.from_dict(raw)
    assert p.tv_shutdown_target == "media_player.tv"
    assert p.tv_entity_id == "media_player.tv"  # memory preserved


def test_from_dict_tv_shutdown_disabled_with_entity_preserves_memory():
    """Row 2 (the 'killer' case): enabled=False + entity set →
    tv_shutdown_target=None BUT tv_entity_id preserved as memory."""
    storage_mod = _load_storage_module()
    raw = {
        **_base_profile_raw(),
        "tv_shutdown_enabled": False,
        "tv_entity_id": "media_player.tv",
    }
    p = storage_mod.Profile.from_dict(raw)
    assert p.tv_shutdown_target is None
    assert p.tv_entity_id == "media_player.tv"  # preserved!


def test_from_dict_tv_shutdown_enabled_no_entity_yields_none_target():
    """Row 3 (footgun): enabled=True + entity None → tv_shutdown_target=None."""
    storage_mod = _load_storage_module()
    raw = {
        **_base_profile_raw(),
        "tv_shutdown_enabled": True,
        "tv_entity_id": None,
    }
    p = storage_mod.Profile.from_dict(raw)
    assert p.tv_shutdown_target is None
    assert p.tv_entity_id is None


def test_from_dict_tv_shutdown_disabled_no_entity_yields_none_target():
    """Row 4 (default): enabled=False + entity None → both None."""
    storage_mod = _load_storage_module()
    raw = {
        **_base_profile_raw(),
        "tv_shutdown_enabled": False,
        "tv_entity_id": None,
    }
    p = storage_mod.Profile.from_dict(raw)
    assert p.tv_shutdown_target is None
    assert p.tv_entity_id is None


def test_from_dict_explicit_tv_shutdown_target_wins_over_legacy():
    """When tv_shutdown_target is already in data, migration is skipped."""
    storage_mod = _load_storage_module()
    raw = {
        **_base_profile_raw(),
        "tv_shutdown_target": "media_player.tv",
        "tv_shutdown_enabled": False,  # disagreeing legacy — should be ignored
        "tv_entity_id": "media_player.other",
    }
    p = storage_mod.Profile.from_dict(raw)
    assert p.tv_shutdown_target == "media_player.tv"
    # Memory preserved verbatim — migration only fills tv_shutdown_target when absent
    assert p.tv_entity_id == "media_player.other"


def test_from_dict_explicit_tv_shutdown_target_none_round_trip():
    """A profile saved with tv_shutdown_target=None must remain None."""
    storage_mod = _load_storage_module()
    raw = {
        **_base_profile_raw(),
        "tv_shutdown_target": None,
    }
    p = storage_mod.Profile.from_dict(raw)
    assert p.tv_shutdown_target is None


# ---------- v0.16.0 anti-defeat fields — defaults + round-trip ----------


def test_profile_v016_fields_default_empty():
    """All four v0.16.0 message/target fields default to empty so existing
    v0.15.9 installs don't get surprise voice on upgrade."""
    storage_mod = _load_storage_module()
    p = storage_mod.Profile(
        id="p1", display_name="Living Room",
        apple_tv_entity_id="media_player.living_room_apple_tv",
        adguard_client_name="AppleTV",
        daily_budget_min=60, grace_seconds=60,
        warn_thresholds_min=[5], idle_grace_minutes=5,
    )
    assert p.countdown_message == ""
    assert p.reactivation_message_friendly == ""
    assert p.reactivation_message_stern == ""
    assert p.notify_parent_target == ""


def test_profile_v016_fields_round_trip():
    storage_mod = _load_storage_module()
    p = storage_mod.Profile(
        id="p1", display_name="Living Room",
        apple_tv_entity_id="media_player.living_room_apple_tv",
        adguard_client_name="AppleTV",
        daily_budget_min=60, grace_seconds=60,
        warn_thresholds_min=[5], idle_grace_minutes=5,
        countdown_message="Achtung! Noch 30 Sekunden Bildschirmzeit.",
        reactivation_message_friendly="Bildschirmzeit ist vorbei. Apple TV bitte aus lassen.",
        reactivation_message_stern="Apple TV bleibt aus. Die Eltern wurden jetzt informiert.",
        notify_parent_target="mobile_app_your_phone",
    )
    rt = storage_mod.Profile.from_dict(p.to_dict())
    assert rt.countdown_message == "Achtung! Noch 30 Sekunden Bildschirmzeit."
    assert rt.reactivation_message_friendly == (
        "Bildschirmzeit ist vorbei. Apple TV bitte aus lassen."
    )
    assert rt.reactivation_message_stern == (
        "Apple TV bleibt aus. Die Eltern wurden jetzt informiert."
    )
    assert rt.notify_parent_target == "mobile_app_your_phone"


def test_profile_v016_fields_missing_in_storage_default_empty():
    """Loading a v0.15.x profile (no v0.16.0 keys) defaults to empty strings."""
    storage_mod = _load_storage_module()
    raw = _base_profile_raw()  # no v0.16.0 keys at all
    p = storage_mod.Profile.from_dict(raw)
    assert p.countdown_message == ""
    assert p.reactivation_message_friendly == ""
    assert p.reactivation_message_stern == ""
    assert p.notify_parent_target == ""
