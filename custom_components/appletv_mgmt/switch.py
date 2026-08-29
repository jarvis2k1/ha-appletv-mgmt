"""Switch entities for Apple TV Mgmt.

Exposes three switches per profile (one ConfigEntry == one profile):
  - `switch.<profile>_tv_shutdown` — also shut down a configured TV on
    enforcement (legacy in v0.15.0+; reads/writes the stored Profile via
    the `tv_shutdown_target` field).
  - `switch.<profile>_adult_mode` — time-boxed override that bypasses ALL
    enforcement for `profile.adult_mode_duration_min` minutes.
  - `switch.<profile>_enforcement_enabled` — legacy monitor-mode toggle.
    In v0.15.0+ this is a thin alias for the `select.<profile>_mode` entity:
    ON ↔ mode="enforced", OFF ↔ mode="monitor_only". Both surfaces stay in
    sync because both read/write the stored Profile.

Audit rows are written on every toggle (v0.15.0+; previously only REST emitted).
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from datetime import timedelta

from homeassistant.util import dt as dt_util

from .audit import record_admin_action
from .const import (
    CONF_ENFORCEMENT_ENABLED,
    DEFAULT_ENFORCEMENT_ENABLED,
    CONF_TV_ENTITY_ID,
    CONF_TV_SHUTDOWN_ENABLED,
    DEFAULT_TV_SHUTDOWN_ENABLED,
    DOMAIN,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    bundle = hass.data[DOMAIN][entry.entry_id]
    profile = bundle["profile"]
    store = bundle["store"]
    coordinator = bundle["coordinator"]
    # All three switches share the same store/coordinator pattern in
    # v0.15.0+ (previously only AdultModeSwitch did). The legacy
    # enforcement_enabled + tv_shutdown switches now read/write the
    # stored Profile directly (no more entry.options + reload-blip).
    async_add_entities(
        [
            TvShutdownEnabledSwitch(hass, entry, profile, store, coordinator),
            AdultModeSwitch(hass, entry, profile, store, coordinator),
            EnforcementEnabledSwitch(hass, entry, profile, store, coordinator),
        ]
    )


class TvShutdownEnabledSwitch(SwitchEntity):
    """v0.15.0+ — legacy toggle for the TV-shutdown target.

    Mirrors `profile.tv_shutdown_target`: ON when target is set; OFF clears
    it. ON-toggle restores from `profile.tv_entity_id` (the memory field).
    OFF-toggle preserves `tv_entity_id` (the user's chosen entity isn't
    lost just because they paused the secondary TV-shutdown).

    Reads/writes the stored Profile directly — NO entry.options writes,
    NO reload-blip. Mirrors AdultModeSwitch's pattern (spec §4.3).

    In Phase C this entity becomes `entity_category=CONFIG, default_disabled=True`
    so it's hidden from new device cards. Existing automations that
    reference it keep working (the toggle semantics are unchanged).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "tv_shutdown"
    _attr_name = "Shut down TV on enforcement"
    _attr_icon = "mdi:television-off"
    # Phase C: legacy switch hidden from new device cards. Existing
    # installs that already had the entity enabled keep it (HA respects
    # the entity registry's user-enabled state).
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        profile,           # storage.Profile (avoid circular import)
        store,             # storage.AppleTVMgmtStore
        coordinator,       # coordinator.AppleTVMgmtCoordinator
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._profile = profile
        self._store = store
        self._coordinator = coordinator
        self._attr_unique_id = f"{profile.id}_tv_shutdown_enabled"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, profile.id)},
            "name": f"Apple TV Mgmt — {profile.display_name}",
            "manufacturer": "Apple TV Mgmt",
            "model": "Profile",
        }

    @property
    def is_on(self) -> bool:
        # Source of truth: stored Profile.tv_shutdown_target (v0.15.0+).
        # Use the cached in-memory profile (kept fresh by the store on
        # any write path — select entity, REST, switch toggle).
        return self._profile.tv_shutdown_target is not None

    @property
    def available(self) -> bool:
        """Switch is meaningful only if a TV entity is remembered.

        After Phase B migration `tv_entity_id` is preserved across
        OFF toggles, so even a switched-off entity has a "memory" target
        to restore. New installs with no configured target → unavailable.
        """
        return bool(self._profile.tv_entity_id or self._profile.tv_shutdown_target)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "tv_shutdown_target": self._profile.tv_shutdown_target,
            "tv_entity_id": self._profile.tv_entity_id,  # legacy/memory
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        # Restore from memory if available; else log + snap back to off.
        target = self._profile.tv_entity_id
        if not target:
            # No remembered target → can't enable. Log INFO (per spec §4.3
            # legacy switch behavior) and write_ha_state so HA snaps back.
            import logging
            logging.getLogger(__name__).info(
                "tv_shutdown switch on but no tv_entity_id configured for %s; "
                "set one via PATCH /limits {tv_shutdown_target: ...} or the options flow.",
                self._profile.id,
            )
            self.async_write_ha_state()
            return
        await self._set_target(target)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_target(None)

    async def _set_target(self, new_target: str | None) -> None:
        # No-op guard (spec §3.1 pattern — applied uniformly per §4.3).
        if self._profile.tv_shutdown_target == new_target:
            return
        old = self._profile.tv_shutdown_target
        self._profile.tv_shutdown_target = new_target
        # Legacy round-trip mirror.
        self._profile.tv_shutdown_enabled = new_target is not None
        # Preserve the memory: setting a new target updates tv_entity_id;
        # clearing keeps the prior value so a subsequent re-enable works.
        if new_target is not None:
            self._profile.tv_entity_id = new_target
        self._store.upsert_profile(self._profile)
        await self._store.async_save()
        record_admin_action(
            self._hass,
            profile_id=self._profile.id,
            action="tv_shutdown_target_changed",
            detail=f"{old or 'none'} → {new_target or 'none'}",
            actor="switch_entity",
        )
        await self._coordinator.async_request_refresh()
        self.async_write_ha_state()


class AdultModeSwitch(SwitchEntity):
    """Time-boxed override — when on, ALL enforcement is bypassed.

    Designed for the parent who's watching a movie and doesn't want the
    integration cutting them off mid-scene. Auto-toggles back to off
    after `profile.adult_mode_duration_min` minutes (default 120).

    Persists across HA restarts via the store's `adult_mode_until` field.
    Toggleable from the Companion app, the dashboard, voice (HA Assist),
    or REST (Phase 3 — when shipped).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "adult_mode"
    _attr_name = "Adult mode"
    _attr_icon = "mdi:shield-account"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        profile,           # storage.Profile (avoid circular import)
        store,             # storage.AppleTVMgmtStore
        coordinator,       # coordinator.AppleTVMgmtCoordinator
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._profile = profile
        self._store = store
        self._coordinator = coordinator
        self._attr_unique_id = f"{profile.id}_adult_mode"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, profile.id)},
            "name": f"Apple TV Mgmt — {profile.display_name}",
            "manufacturer": "Apple TV Mgmt",
            "model": "Profile",
        }

    @property
    def is_on(self) -> bool:
        return self._store.is_adult_mode_active(self._profile.id)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        until = self._store.adult_mode_until(self._profile.id)
        return {
            "until": until.isoformat() if until else None,
            "duration_minutes": self._profile.adult_mode_duration_min,
        }

    async def async_turn_on(self, **kwargs) -> None:
        duration = max(1, int(self._profile.adult_mode_duration_min))
        until = dt_util.utcnow() + timedelta(minutes=duration)
        self._store.set_adult_mode_until(self._profile.id, until)
        await self._store.async_save()
        # v0.15.6 — record an explicit adult_mode_on audit row + fire
        # the new adult_mode_on voice trigger (was previously emitted
        # only by REST POST /adult_mode; switch toggle was silent).
        record_admin_action(
            self._hass,
            profile_id=self._profile.id,
            action="adult_mode_on",
            detail=f"{duration} min (switch)",
            actor="switch_entity",
        )
        from .voice_notifier import fire_adult_mode_on_voice
        self._hass.async_create_task(
            fire_adult_mode_on_voice(self._hass, self._profile)
        )
        # Trigger a coordinator refresh so the enforcer drops to OK
        # immediately (without waiting up to 30s for the next tick).
        await self._coordinator.async_request_refresh()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._store.set_adult_mode_until(self._profile.id, None)
        await self._store.async_save()
        # v0.15.6 — audit row on switch-driven adult_mode_off too.
        record_admin_action(
            self._hass,
            profile_id=self._profile.id,
            action="adult_mode_off",
            detail="switch",
            actor="switch_entity",
        )
        await self._coordinator.async_request_refresh()
        self.async_write_ha_state()


class EnforcementEnabledSwitch(SwitchEntity):
    """v0.15.0+ — legacy alias for `select.<profile>_mode`.

    ON ↔ `profile.mode == "enforced"`. OFF ↔ `profile.mode == "monitor_only"`.
    The select entity is the canonical UI control (3 options: enforced /
    monitor_only / paused); this 2-state switch is kept so existing
    automations + HA Assist phrases keep working.

    Reads/writes the stored Profile directly — NO entry.options writes,
    NO reload-blip. Mirrors AdultModeSwitch's pattern (spec §4.3). Both
    surfaces (select + switch) stay in sync automatically because they
    read the same stored field.

    In Phase C this entity becomes `entity_category=CONFIG, default_disabled=True`
    so it's hidden from new device cards. Existing automations that
    reference it keep working unchanged.

    Note: the switch can't represent the "paused" mode. Toggling OFF
    when mode is already "paused" is a no-op (we don't downgrade paused
    to monitor_only via the legacy switch — use the select for that).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "enforcement_enabled"
    _attr_name = "Enforcement enabled"
    _attr_icon = "mdi:shield-check"
    # Phase C: legacy switch hidden from new device cards. Existing
    # installs that already had the entity enabled keep it.
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        profile,           # storage.Profile
        store,             # storage.AppleTVMgmtStore
        coordinator,       # coordinator.AppleTVMgmtCoordinator
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._profile = profile
        self._store = store
        self._coordinator = coordinator
        self._attr_unique_id = f"{profile.id}_enforcement_enabled"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, profile.id)},
            "name": f"Apple TV Mgmt — {profile.display_name}",
            "manufacturer": "Apple TV Mgmt",
            "model": "Profile",
        }

    @property
    def is_on(self) -> bool:
        # Source of truth: stored Profile.mode == "enforced".
        # "paused" reports OFF (legacy switch can't represent paused).
        return getattr(self._profile, "mode", "enforced") == "enforced"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_mode("enforced")

    async def async_turn_off(self, **kwargs: Any) -> None:
        # Per docstring: OFF means monitor_only via legacy switch.
        # A user who actually wants "paused" must use the select entity.
        # However, if mode is already "paused", don't trample it back
        # to monitor_only (silent guard — protect the parent's choice).
        current = getattr(self._profile, "mode", "enforced")
        if current == "paused":
            # Re-write state so HA UI snaps back to "off" (which is
            # consistent: mode != enforced → is_on=False).
            self.async_write_ha_state()
            return
        await self._set_mode("monitor_only")

    async def _set_mode(self, new_mode: str) -> None:
        # No-op guard (spec §3.1 pattern — applied uniformly per §4.3).
        current = getattr(self._profile, "mode", "enforced")
        if current == new_mode:
            return
        old_mode = current
        self._profile.mode = new_mode
        # Legacy round-trip mirror so on-disk JSON keeps both fields
        # consistent (per spec §4.2 step 2 reconciliation).
        self._profile.enforcement_enabled = (new_mode == "enforced")
        self._store.upsert_profile(self._profile)
        await self._store.async_save()
        record_admin_action(
            self._hass,
            profile_id=self._profile.id,
            action="mode_changed",
            detail=f"{old_mode} → {new_mode}",
            actor="switch_entity",
        )
        # v0.15.6 — fire mode_change voice if profile opts in
        from .voice_notifier import fire_mode_change_voice
        self._hass.async_create_task(
            fire_mode_change_voice(
                self._hass, self._profile,
                old_mode=old_mode, new_mode=new_mode,
            )
        )
        await self._coordinator.async_request_refresh()
        self.async_write_ha_state()
