"""Tests for the pure helpers in voice_notifier.py.

Only `format_message` + `should_speak` are exercised — the `speak`
coroutine calls into HA services and is verified live.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_voice_notifier_module():
    """Import voice_notifier with HA stubs (it has light HA imports for typing)."""
    for mod_name in (
        "homeassistant",
        "homeassistant.core",
        "homeassistant.util",
        "homeassistant.util.dt",
        "homeassistant.helpers",
        "homeassistant.helpers.storage",
    ):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)
    hass_core = sys.modules["homeassistant.core"]
    setattr(hass_core, "HomeAssistant", type("HomeAssistant", (), {}))
    util_dt = sys.modules["homeassistant.util.dt"]
    from datetime import datetime, timezone
    setattr(util_dt, "utcnow", lambda: datetime(2026, 5, 24, 10, 0, tzinfo=timezone.utc))

    # Storage module is heavier; load via the same path
    storage_path = ROOT / "custom_components" / "appletv_mgmt" / "storage.py"
    spec = __import__("importlib.util", fromlist=["util"]).spec_from_file_location(
        "custom_components.appletv_mgmt.storage", storage_path
    )
    storage_mod = __import__("importlib.util", fromlist=["util"]).module_from_spec(spec)
    sys.modules["custom_components.appletv_mgmt.storage"] = storage_mod
    spec.loader.exec_module(storage_mod)

    vn_path = ROOT / "custom_components" / "appletv_mgmt" / "voice_notifier.py"
    spec2 = __import__("importlib.util", fromlist=["util"]).spec_from_file_location(
        "custom_components.appletv_mgmt.voice_notifier", vn_path
    )
    vn = __import__("importlib.util", fromlist=["util"]).module_from_spec(spec2)
    sys.modules["custom_components.appletv_mgmt.voice_notifier"] = vn
    spec2.loader.exec_module(vn)
    return vn, storage_mod


@pytest.fixture(scope="module")
def vn():
    mod, _ = _load_voice_notifier_module()
    return mod


@pytest.fixture
def storage_mod():
    _, mod = _load_voice_notifier_module()
    return mod


def _make_profile(storage_mod, **overrides):
    """Build a Profile with sensible defaults + overrides for the relevant fields."""
    p = storage_mod.Profile(
        id="p1",
        display_name="Living Room",
        apple_tv_entity_id="media_player.heimkinoaaa",
        adguard_client_name="AppleTV",
        daily_budget_min=60,
        grace_seconds=60,
        warn_thresholds_min=[5],
        idle_grace_minutes=5,
    )
    for k, v in overrides.items():
        setattr(p, k, v)
    return p


# ---------- format_message ----------


def test_format_message_substitutes_minutes(vn):
    out = vn.format_message("{minutes} Minuten übrig", minutes=5)
    assert out == "5 Minuten übrig"


def test_format_message_substitutes_app(vn):
    out = vn.format_message("Schluss mit {app}", app="Netflix")
    assert out == "Schluss mit Netflix"


def test_format_message_handles_both_placeholders(vn):
    out = vn.format_message("{minutes} Min {app} übrig", minutes=10, app="Disney+")
    assert out == "10 Min Disney+ übrig"


def test_format_message_missing_values_become_empty(vn):
    """Avoid speaking the literal '{minutes}' when we don't have it."""
    out = vn.format_message("Noch {minutes} Min {app}", minutes=None, app=None)
    # Spaces collapse to readable form on .strip(); inner gaps remain.
    assert "{minutes}" not in out
    assert "{app}" not in out


def test_format_message_empty_template_returns_empty(vn):
    assert vn.format_message("", minutes=5, app="Netflix") == ""
    assert vn.format_message(None, minutes=5) == ""


def test_format_message_strips_whitespace(vn):
    assert vn.format_message("  hello  ", minutes=1) == "hello"


# ---------- v0.15.0: {old_mode}, {new_mode} placeholders ----------


def test_format_message_substitutes_mode_change(vn):
    out = vn.format_message(
        "Modus von {old_mode} zu {new_mode}",
        old_mode="enforced",
        new_mode="paused",
    )
    assert out == "Modus von enforced zu paused"


def test_format_message_mode_placeholders_default_to_empty(vn):
    """Missing values render as empty so the template is speakable."""
    out = vn.format_message("alt: {old_mode}, neu: {new_mode}")
    assert "{old_mode}" not in out
    assert "{new_mode}" not in out


# ---------- should_speak ----------


def test_should_speak_requires_both_template_and_target(vn, storage_mod):
    # Default profile has neither configured.
    p = _make_profile(storage_mod)
    assert vn.should_speak(p, "hello") is False
    assert vn.should_speak(p, "") is False


def test_should_speak_true_when_both_configured(vn, storage_mod):
    p = _make_profile(
        storage_mod,
        notify_media_player_entity_id="media_player.dining_room",
    )
    assert vn.should_speak(p, "anything") is True


def test_should_speak_false_when_only_target_no_message(vn, storage_mod):
    p = _make_profile(
        storage_mod,
        notify_media_player_entity_id="media_player.dining_room",
    )
    assert vn.should_speak(p, "") is False


def test_should_speak_false_when_only_message_no_target(vn, storage_mod):
    p = _make_profile(storage_mod, warning_message="5 minutes left")
    # Target not set on the profile, so still false.
    assert vn.should_speak(p, p.warning_message) is False


# ---------- v0.15.6 fire_mode_change_voice + fire_adult_mode_on_voice ----------

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def _make_speakable_profile(storage_mod, **overrides):
    """Profile with media_player target set + reasonable defaults."""
    base = dict(
        id="p1",
        display_name="Living Room",
        apple_tv_entity_id="media_player.heimkinoaaa",
        adguard_client_name="AppleTV",
        daily_budget_min=60,
        grace_seconds=60,
        warn_thresholds_min=[5],
        idle_grace_minutes=5,
        notify_media_player_entity_id="media_player.dining_room",
        notify_tts_entity_id="tts.google_translate_en_com",
        notify_tts_language="de",
        notify_volume=0.45,
    )
    base.update(overrides)
    return storage_mod.Profile(**base)


def test_v0_15_6_fire_mode_change_voice_speaks_when_opted_in(vn, storage_mod):
    """voice_on_mode_change=True + template set → speak() called."""
    p = _make_speakable_profile(
        storage_mod,
        voice_on_mode_change=True,
        mode_change_message="Wechsel von {old_mode} zu {new_mode}",
    )
    hass = MagicMock()
    hass.services.async_call = AsyncMock(return_value=None)
    asyncio.run(vn.fire_mode_change_voice(
        hass, p, old_mode="enforced", new_mode="monitor_only",
    ))
    # tts.speak got called (the second async_call; first is volume_set)
    assert hass.services.async_call.call_count >= 1


def test_v0_15_6_fire_mode_change_voice_skips_when_flag_off(vn, storage_mod):
    """voice_on_mode_change=False (default) → no speak."""
    p = _make_speakable_profile(
        storage_mod,
        voice_on_mode_change=False,
        mode_change_message="Wechsel",
    )
    hass = MagicMock()
    hass.services.async_call = AsyncMock(return_value=None)
    asyncio.run(vn.fire_mode_change_voice(
        hass, p, old_mode="enforced", new_mode="monitor_only",
    ))
    hass.services.async_call.assert_not_called()


def test_v0_15_6_fire_mode_change_voice_skips_when_no_change(vn, storage_mod):
    """No-op when old_mode == new_mode (mirrors the no-op-guard in select)."""
    p = _make_speakable_profile(
        storage_mod,
        voice_on_mode_change=True,
        mode_change_message="x",
    )
    hass = MagicMock()
    hass.services.async_call = AsyncMock(return_value=None)
    asyncio.run(vn.fire_mode_change_voice(
        hass, p, old_mode="enforced", new_mode="enforced",
    ))
    hass.services.async_call.assert_not_called()


def test_v0_15_6_fire_adult_mode_on_voice_speaks_when_template_set(vn, storage_mod):
    """adult_mode_on_message set + mode != paused → speak()."""
    p = _make_speakable_profile(
        storage_mod,
        mode="enforced",
        adult_mode_on_message="Erwachsenenmodus aktiv",
    )
    hass = MagicMock()
    hass.services.async_call = AsyncMock(return_value=None)
    asyncio.run(vn.fire_adult_mode_on_voice(hass, p))
    assert hass.services.async_call.call_count >= 1


def test_v0_15_6_fire_adult_mode_on_voice_silent_under_paused(vn, storage_mod):
    """Spec §3.7 — adult_mode_on respects mode=paused (be quiet)."""
    p = _make_speakable_profile(
        storage_mod,
        mode="paused",
        adult_mode_on_message="Erwachsenenmodus aktiv",
    )
    hass = MagicMock()
    hass.services.async_call = AsyncMock(return_value=None)
    asyncio.run(vn.fire_adult_mode_on_voice(hass, p))
    hass.services.async_call.assert_not_called()


def test_v0_15_6_fire_adult_mode_on_voice_silent_when_template_empty(vn, storage_mod):
    """No template → no voice (the opt-out path; default)."""
    p = _make_speakable_profile(storage_mod, mode="enforced", adult_mode_on_message="")
    hass = MagicMock()
    hass.services.async_call = AsyncMock(return_value=None)
    asyncio.run(vn.fire_adult_mode_on_voice(hass, p))
    hass.services.async_call.assert_not_called()
