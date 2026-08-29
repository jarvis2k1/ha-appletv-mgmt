"""Select entities for Apple TV Mgmt — v0.15.0.

Per spec §3.1: ONE select entity per profile, three options:
  * `enforced`     — Track + warn + enforce (default)
  * `monitor_only` — Track + audit; no AdGuard, no sleep, no warning voice
                     by default (opt-in via profile.warn_in_monitor_mode)
  * `paused`       — Track + audit; no voice (except extension grants),
                     no AdGuard, no sleep

The entity is the canonical surface for changing mode. The legacy
`switch.<profile>_enforcement_enabled` is kept (rewritten in v0.15.0
to read/write the same stored Profile.mode field — see switch.py).

Contract (spec §3.1):
  * unique_id = f"{profile.id}_mode"
  * options   = ["enforced", "monitor_only", "paused"]
  * current_option reads profile.mode from the STORED Profile
  * async_select_option:
      1. No-op guard: if profile.mode == option, return immediately
      2. Write profile.mode = option (legacy enforcement_enabled synced)
      3. store.upsert_profile + async_save
      4. record_admin_action(action="mode_changed", actor="select_entity")
      5. coordinator.async_request_refresh + async_write_ha_state
  * Does NOT fire EVENT_LIMITS_UPDATED — the dedicated `mode_changed`
    audit row is the only audit emission for this change. Avoids the
    double-row Opus QA round-2 S5 flagged.
  * Does NOT call async_update_entry — no reload-blip.
"""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .audit import record_admin_action
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

MODE_OPTIONS = ["enforced", "monitor_only", "paused"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    bundle = hass.data[DOMAIN][entry.entry_id]
    profile = bundle["profile"]
    async_add_entities(
        [
            ModeSelect(
                hass,
                entry,
                profile,
                bundle["store"],
                bundle["coordinator"],
            )
        ]
    )


class ModeSelect(SelectEntity):
    """The canonical mode selector — `enforced` / `monitor_only` / `paused`."""

    _attr_has_entity_name = True
    _attr_translation_key = "mode"
    _attr_icon = "mdi:shield-search"
    _attr_options = MODE_OPTIONS
    _attr_entity_category = None  # primary control

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
        self._attr_unique_id = f"{profile.id}_mode"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, profile.id)},
            "name": f"Apple TV Mgmt — {profile.display_name}",
            "manufacturer": "Apple TV Mgmt",
            "model": "Profile",
        }

    @property
    def current_option(self) -> str | None:
        """Read mode from the STORED Profile (not entry.options).

        See spec §3.1: the stored Profile is the source of truth in
        v0.15.0+. If the store hasn't loaded yet (defensive — should
        not happen because async_setup_entry awaits async_load), fall
        back to the runtime Profile field.
        """
        stored = self._store.get_profile(self._profile.id)
        if stored is not None:
            return stored.mode
        # Defensive fallback — the in-memory Profile object always has `mode`.
        return getattr(self._profile, "mode", "enforced")

    async def async_select_option(self, option: str) -> None:
        if option not in MODE_OPTIONS:
            # Defensive — HA already validates against _attr_options, but
            # never trust caller input.
            _LOGGER.warning(
                "ModeSelect rejecting invalid option %r (allowed: %s)",
                option,
                MODE_OPTIONS,
            )
            return
        profile = self._store.get_profile(self._profile.id)
        if profile is None:
            _LOGGER.warning(
                "ModeSelect.async_select_option: stored profile %s missing",
                self._profile.id,
            )
            return
        # 1. No-op guard — spec §3.1 step 1: zero audit, zero refresh,
        # zero events if the value is unchanged.
        if profile.mode == option:
            return

        old_mode = profile.mode
        # 2. Write the new mode + keep the legacy bool in sync.
        profile.mode = option
        profile.enforcement_enabled = option == "enforced"
        # 3. Persist.
        self._store.upsert_profile(profile)
        await self._store.async_save()
        # Also update the live in-memory Profile reference so other
        # readers (enforcer, coordinator) see the change without a reload.
        self._profile.mode = option
        self._profile.enforcement_enabled = profile.enforcement_enabled

        # 4. Audit row with the dedicated action — spec §3.1 says this
        # is the ONLY audit emission for a mode change (no
        # EVENT_LIMITS_UPDATED, so no double row).
        record_admin_action(
            self._hass,
            profile_id=self._profile.id,
            action="mode_changed",
            detail=f"{old_mode} → {option}",
            actor="select_entity",
        )
        # 5. v0.15.6 — fire mode_change voice if profile opts in
        # (voice_on_mode_change=True + mode_change_message set).
        from .voice_notifier import fire_mode_change_voice
        self._hass.async_create_task(
            fire_mode_change_voice(
                self._hass, self._profile,
                old_mode=old_mode, new_mode=option,
            )
        )
        # 6. Trigger a coordinator refresh so the effective_state sensor
        # and any downstream consumers see the new mode immediately.
        await self._coordinator.async_request_refresh()
        self.async_write_ha_state()
