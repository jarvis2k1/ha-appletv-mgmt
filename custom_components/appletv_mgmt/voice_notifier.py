"""Voice-announcement notifier (v0.13.0).

When the state machine transitions to WARNING or ENFORCING, optionally
speak a configurable message over any HA media_player (Sonos, Apple TV
speaker, Google Home, …) via the `tts.speak` service. Audio plays in
the room while the kid's video keeps running on the Apple TV — much
less disruptive than playing audio over the Apple TV itself.

Pure-ish: the only HA dependency is hass.services.async_call. The
message-template substitution and the should-speak gating are pure
and unit-tested in test_voice_notifier.py.

The audit recorder logs every successful announcement as a
`voice_announcement` action entry.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .storage import Profile

_LOGGER = logging.getLogger(__name__)


def format_message(
    template: str,
    *,
    minutes: int | None = None,
    app: str | None = None,
    old_mode: str | None = None,
    new_mode: str | None = None,
) -> str:
    """Substitute {minutes}, {app}, {old_mode}, {new_mode} placeholders.

    Missing values become empty strings — keeps the message speakable
    even when (e.g.) we don't know the current app. `old_mode`/`new_mode`
    added in v0.15.0 for the `mode_change_message` template.
    """
    if not template:
        return ""
    return template.format(
        minutes=("" if minutes is None else int(minutes)),
        app=app or "",
        old_mode=old_mode or "",
        new_mode=new_mode or "",
    ).strip()


def should_speak(profile: Profile, template: str) -> bool:
    """Whether the configured profile+message combo would actually speak."""
    if not template:
        return False
    if not profile.notify_media_player_entity_id:
        return False
    return True


async def speak(
    hass: HomeAssistant,
    profile: Profile,
    *,
    template: str,
    minutes: int | None = None,
    app: str | None = None,
    old_mode: str | None = None,
    new_mode: str | None = None,
) -> dict[str, Any]:
    """Speak the templated message on the profile's configured target.

    Returns a small dict describing what happened — the caller (audit
    recorder) persists this into the action log.

    Possible returns:
      {"status": "skipped", "reason": "no media_player" | "no message"}
      {"status": "spoken", "message": "...", "entity_id": "..."}
      {"status": "error",   "error": "..."}
    """
    if not should_speak(profile, template):
        if not template:
            return {"status": "skipped", "reason": "no message"}
        return {"status": "skipped", "reason": "no media_player"}

    message = format_message(
        template,
        minutes=minutes,
        app=app,
        old_mode=old_mode,
        new_mode=new_mode,
    )
    target = profile.notify_media_player_entity_id

    try:
        # Bump volume FIRST (best effort — ignore errors).
        if profile.notify_volume and 0 < profile.notify_volume <= 1:
            try:
                await hass.services.async_call(
                    "media_player",
                    "volume_set",
                    {
                        "entity_id": target,
                        "volume_level": float(profile.notify_volume),
                    },
                    blocking=True,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("volume_set on %s failed (will speak anyway): %s", target, err)

        # Speak.
        service_data = {
            "media_player_entity_id": target,
            "message": message,
        }
        # Language: the per-profile override wins; otherwise fall back to HA's
        # own configured language so a fresh install speaks in the user's
        # language rather than a hardcoded default (v0.20.1 — was "de").
        lang = profile.notify_tts_language or getattr(
            getattr(hass, "config", None), "language", None
        )
        if lang:
            service_data["language"] = lang
        # If user configured a specific TTS entity, pass it; else HA picks
        # the default tts.* entity (works when only one is installed).
        if profile.notify_tts_entity_id:
            service_data["entity_id"] = profile.notify_tts_entity_id

        await hass.services.async_call(
            "tts", "speak", service_data, blocking=False
        )
        _LOGGER.info(
            "Voice announcement on %s (%s): %r",
            target,
            profile.notify_tts_entity_id or "default-tts",
            message,
        )
        return {
            "status": "spoken",
            "message": message,
            "entity_id": target,
            "at": dt_util.utcnow().isoformat(),
        }
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Voice announcement failed on %s: %s", target, err)
        return {"status": "error", "error": str(err)}


# v0.15.6 — convenience helpers for the two new triggers shipped in
# v0.15.0 (Profile fields + format_message placeholders existed, but no
# callers fired them). Each helper does its own gating + speak + audit-row
# recording so call sites just `await fire_xxx(hass, profile, ...)`.

async def fire_mode_change_voice(
    hass: HomeAssistant,
    profile: Profile,
    *,
    old_mode: str,
    new_mode: str,
) -> None:
    """Speak `mode_change_message` on mode transitions when
    `voice_on_mode_change=True` on the profile (default False — silent).
    Spec §3.7 row "Mode changed".

    Substitutes `{old_mode}` and `{new_mode}` placeholders. No-op when
    the toggle was a no-op (old == new), when the flag is False, or
    when the template is empty.
    """
    if old_mode == new_mode:
        return
    if not getattr(profile, "voice_on_mode_change", False):
        return
    template = getattr(profile, "mode_change_message", "") or ""
    if not should_speak(profile, template):
        return
    result = await speak(
        hass, profile, template=template,
        old_mode=old_mode, new_mode=new_mode,
    )
    if result.get("status") == "spoken":
        _record_voice_audit(hass, profile, result, reason="mode_changed")


async def fire_adult_mode_on_voice(hass: HomeAssistant, profile: Profile) -> None:
    """Speak `adult_mode_on_message` when adult mode is enabled.
    Spec §3.7 row "Adult mode enabled (new)" — speaks unless mode=paused
    (paused = the parent has explicitly muted the integration; the new
    adult-mode trigger respects that).
    """
    if getattr(profile, "mode", "enforced") == "paused":
        return
    template = getattr(profile, "adult_mode_on_message", "") or ""
    if not should_speak(profile, template):
        return
    result = await speak(hass, profile, template=template)
    if result.get("status") == "spoken":
        _record_voice_audit(hass, profile, result, reason="adult_mode_on")


def _record_voice_audit(
    hass: HomeAssistant,
    profile: Profile,
    result: dict[str, Any],
    *,
    reason: str,
) -> None:
    """Persist a `voice_announcement` audit row after a successful
    fire_*_voice call. Mirrors what audit.py's `_maybe_speak` does for
    the legacy enforce/extension triggers — same shape, same kind of
    row, so the panel renders them with the existing 🔊 icon.
    """
    # Lazy import: voice_notifier may load before audit/storage at boot.
    try:
        from .const import DOMAIN
        bundle = hass.data.get(DOMAIN, {}).get(profile.id)
        if not bundle:
            return
        store = bundle.get("store")
        if store is None:
            return
        store.record_action(
            profile_id=profile.id,
            action="voice_announcement",
            reason=reason,
            detail=result.get("message"),
        )
        # Save asynchronously — record_action already mutated the
        # in-memory store, persisting on the next tick is fine.
        hass.async_create_task(store.async_save())
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("voice audit record failed (non-fatal): %s", err)
