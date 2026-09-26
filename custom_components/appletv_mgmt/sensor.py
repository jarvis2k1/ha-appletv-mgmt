"""Sensor entities for Apple TV Mgmt."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .categorize import ALL_GROUPS
from .const import DOMAIN, app_display_name
from .coordinator import AppleTVMgmtCoordinator
from .policy import should_act

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class AppleTVMgmtSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict], object]
    # Optional: pull additional fields from the coordinator snapshot and
    # publish them as entity attributes (used for `active_quiet_window`
    # on the enforcement_state sensor).
    attr_keys: tuple[str, ...] = ()


SENSORS: tuple[AppleTVMgmtSensorDescription, ...] = (
    AppleTVMgmtSensorDescription(
        key="time_used_today",
        translation_key="time_used_today",
        name="Time used today",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: round(d["used_seconds_today"] / 60, 1),
    ),
    AppleTVMgmtSensorDescription(
        key="time_remaining_today",
        translation_key="time_remaining_today",
        name="Time remaining today",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: round(d["remaining_seconds_today"] / 60, 1),
    ),
    AppleTVMgmtSensorDescription(
        key="current_app",
        translation_key="current_app",
        name="Current app",
        value_fn=lambda d: d["current_bundle_id"] or "none",
    ),
    AppleTVMgmtSensorDescription(
        key="enforcement_state",
        translation_key="enforcement_state",
        # Phase C: re-labelled + downgraded to diagnostic in v0.15.0+.
        # The new `sensor.<profile>_effective_state` is the user-facing
        # one (it consults policy.should_act + collapses monitor/paused/
        # adult). This raw state machine reading stays for back-compat
        # and for diagnostics.
        name="State machine (raw)",
        entity_category=EntityCategory.DIAGNOSTIC,
        # Carries `active_quiet_window` as an attribute so dashboards can
        # show "blocked by Bedtime" vs "blocked by daily limit" without
        # template gymnastics.
        value_fn=lambda d: d["enforcement_state"],
        attr_keys=("active_quiet_window",),
    ),
    AppleTVMgmtSensorDescription(
        key="extension_minutes_today",
        translation_key="extension_minutes_today",
        name="Extension minutes today",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d["extension_minutes_today"],
    ),
    # v0.18.0 — DNS-corroboration diagnostic sensors.
    #
    # All three are EntityCategory.DIAGNOSTIC so they stay out of the
    # primary device card by default. The intended audience is the
    # operator (Marc) verifying classifier rollout and the parent
    # double-checking what's been counted, not the kid.
    #
    # `attribution_source` shows the latest classifier verdict in the
    # form "<action>:<reason_hint>" — e.g. "preserve:pyatv_fresh",
    # "annotate_group:dns_bundle", "close_at_last_updated:sustained".
    # When the feature is off the value is "disabled"; between sessions
    # it's "no_open_event".
    AppleTVMgmtSensorDescription(
        key="attribution_source",
        translation_key="attribution_source",
        name="Attribution source",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:source-branch",
        value_fn=lambda d: d.get("attribution_source") or "unknown",
    ),
    # `dns_classifier_confidence` mirrors the dns_classifier's Confidence
    # enum name ("NONE" / "AMBIENT_ONLY" / "GROUP_ONLY" / "BUNDLE"). NONE
    # is also the value while the feature is off or has no signal yet.
    AppleTVMgmtSensorDescription(
        key="dns_classifier_confidence",
        translation_key="dns_classifier_confidence",
        name="DNS classifier confidence",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:dns-outline",
        value_fn=lambda d: d.get("dns_classifier_confidence") or "NONE",
    ),
    # `attribution_gap_minutes_today` is the cumulative duration of
    # DNS-driven group reclassifications today — the "visible gap"
    # between what pyatv reported and what the DNS corroborator
    # corrected. Equivalent to "how much time today did we have to
    # rescue from pyatv push-silence".
    AppleTVMgmtSensorDescription(
        key="attribution_gap_minutes_today",
        translation_key="attribution_gap_minutes_today",
        name="Attribution gap minutes today",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-alert-outline",
        value_fn=lambda d: d.get("attribution_gap_minutes_today", 0.0),
    ),
    # v0.18.1 — TV-on minutes today (observability only, no enforcement).
    # Accrues seconds while the configured `tv_entity_id` reports an
    # on-ish state (on/playing/paused/buffering). Resets at local midnight.
    # 0 when `tv_entity_id` is empty. Helps the parent see TV-use trends
    # even when the kid wasn't on the Apple TV.
    AppleTVMgmtSensorDescription(
        # v0.20.1 — renamed from `samsung_tv_on_minutes_today` so the unique_id
        # doesn't bake a TV brand into every install (an LG owner shouldn't get
        # a *_samsung_* entity). A registry migration in async_setup_entry moves
        # any existing entity to the new id. TV-brand-neutral.
        key="tv_on_minutes_today",
        translation_key="tv_on_minutes_today",
        name="TV on minutes today",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:television",
        value_fn=lambda d: round(d.get("tv_on_seconds_today", 0.0) / 60.0, 1),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    bundle = hass.data[DOMAIN][entry.entry_id]
    coordinator: AppleTVMgmtCoordinator = bundle["coordinator"]
    profile = bundle["profile"]

    # v0.20.1 — one-time unique_id migration for the TV-on sensor rename
    # (samsung_tv_on_minutes_today -> tv_on_minutes_today). Idempotent and a
    # no-op on fresh installs (no old entity to move). Skipped if the new id
    # already exists to avoid a registry collision.
    try:
        from homeassistant.helpers import entity_registry as er

        reg = er.async_get(hass)
        old_uid = f"{profile.id}_samsung_tv_on_minutes_today"
        new_uid = f"{profile.id}_tv_on_minutes_today"
        old_eid = reg.async_get_entity_id("sensor", DOMAIN, old_uid)
        new_eid = reg.async_get_entity_id("sensor", DOMAIN, new_uid)
        if old_eid and not new_eid:
            reg.async_update_entity(old_eid, new_unique_id=new_uid)
            _LOGGER.info(
                "Migrated TV-on sensor unique_id %s -> %s", old_uid, new_uid
            )
    except Exception as err:  # noqa: BLE001 — migration is best-effort
        _LOGGER.debug("TV-on sensor unique_id migration skipped: %s", err)
    store = bundle["store"]

    entities: list[SensorEntity] = [
        AppleTVMgmtSensor(coordinator, profile.id, profile.display_name, desc)
        for desc in SENSORS
    ]
    entities.append(
        AppleTVTodayHistorySensor(coordinator, store, profile.id, profile.display_name)
    )
    # v0.15.0 — effective_state sensor per spec §3.3 (the 10-state matrix).
    entities.append(
        EffectiveStateSensor(coordinator, store, profile)
    )
    # Per-group sensors — one "used" + one "remaining" per group. Disabled
    # by default for groups the user hasn't budgeted; they still register
    # so dashboards can opt in.
    for group in ALL_GROUPS:
        entities.append(
            AppleTVGroupSensor(coordinator, profile.id, profile.display_name, group, mode="used")
        )
        entities.append(
            AppleTVGroupSensor(coordinator, profile.id, profile.display_name, group, mode="remaining")
        )
    async_add_entities(entities)


class _ProfileRolloverMixin:
    """Shared access to the profile's day-rollover hour.

    v0.23.1 — this lived on `AppleTVMgmtSensor` alone, but
    `AppleTVTodayHistorySensor` (a sibling, not a subclass) called it too, so
    every read of that entity raised AttributeError and HA reported it as
    `unavailable` — silently, on every v0.23.0 install. A mixin rather than a
    second copy, so the next sensor class cannot repeat the mistake.
    """

    def _rollover_hour(self) -> int:
        """The profile's day-rollover hour, via the coordinator.

        Falls back to 0 (midnight) if the coordinator or profile is not
        reachable, which matches the pre-v0.23.0 behaviour rather than
        inventing a boundary.
        """
        prof = getattr(getattr(self, "coordinator", None), "_profile", None)
        return int(getattr(prof, "day_rollover_hour", 0) or 0)


class AppleTVMgmtSensor(_ProfileRolloverMixin, CoordinatorEntity[AppleTVMgmtCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: AppleTVMgmtSensorDescription

    def __init__(
        self,
        coordinator: AppleTVMgmtCoordinator,
        profile_id: str,
        profile_display_name: str,
        description: AppleTVMgmtSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{profile_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, profile_id)},
            "name": f"Apple TV Mgmt — {profile_display_name}",
            "manufacturer": "Apple TV Mgmt",
            "model": "Profile",
        }

    @property
    def native_value(self) -> object:
        if not self.coordinator.data:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        if not self.coordinator.data or not self.entity_description.attr_keys:
            return None
        return {k: self.coordinator.data.get(k) for k in self.entity_description.attr_keys}


# v0.21.0 — group keys whose generic title-casing reads poorly. Keeps the
# per-group sensor names natural without an i18n round-trip (these sensors set
# `_attr_name` directly rather than a translation_key).
_GROUP_DISPLAY_OVERRIDES: dict[str, str] = {"linear_tv": "Live TV"}


class AppleTVGroupSensor(_ProfileRolloverMixin, CoordinatorEntity[AppleTVMgmtCoordinator], SensorEntity):
    """Per-group `used` / `remaining` minutes today.

    Auto-disabled until a budget is set for the group (avoids registry
    bloat for groups the user doesn't care about). Once enabled, the
    sensor reports `0` for `used` when the group has no events today
    and `None` for `remaining` when no budget is configured for it.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AppleTVMgmtCoordinator,
        profile_id: str,
        profile_display_name: str,
        group: str,
        *,
        mode: str,
    ) -> None:
        super().__init__(coordinator)
        assert mode in ("used", "remaining")
        self._group = group
        self._mode = mode
        suffix = "time_used" if mode == "used" else "time_remaining"
        self._attr_unique_id = f"{profile_id}_{group}_{suffix}_today"
        # v0.21.0 — a couple of group keys don't title-case naturally; special-
        # case them so the sensor reads "Live TV" not "Linear Tv". Everything
        # else keeps the generic titling (movies → "Movies", etc.).
        label = _GROUP_DISPLAY_OVERRIDES.get(
            group, group.replace("_", " ").title()
        )
        self._attr_name = f"{label} time {mode} today"
        self._attr_native_unit_of_measurement = "min"
        self._attr_device_class = SensorDeviceClass.DURATION
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_entity_registry_enabled_default = False  # opt-in per group
        self._attr_icon = "mdi:timer-outline" if mode == "used" else "mdi:timer-sand"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, profile_id)},
            "name": f"Apple TV Mgmt — {profile_display_name}",
            "manufacturer": "Apple TV Mgmt",
            "model": "Profile",
        }

    @property
    def native_value(self) -> object:
        data = self.coordinator.data or {}
        totals_s = data.get("group_totals_seconds") or {}
        budgets_min = data.get("group_budgets_minutes") or {}
        used_s = totals_s.get(self._group, 0)
        if self._mode == "used":
            return round(used_s / 60, 1)
        # remaining
        budget_min = budgets_min.get(self._group)
        if budget_min is None:
            return None
        return round(max(0, int(budget_min) * 60 - used_s) / 60, 1)


class AppleTVTodayHistorySensor(_ProfileRolloverMixin, CoordinatorEntity[AppleTVMgmtCoordinator], SensorEntity):
    """Today's per-app usage log, exposed as JSON-shaped attributes.

    The native state is the number of distinct apps used today (useful as a
    glanceable metric). The interesting data lives in `extra_state_attributes`:

    * `apps` — per-app totals, sorted by most-used:
      [{bundle_id, display_name, total_minutes, sessions}, ...]
    * `events` — chronological session log clipped to today:
      [{bundle_id, display_name, started_at, ended_at, duration_minutes}, ...]

    Used by the appletv-mgmt-history-card Lovelace card, or by your own
    Markdown / apexcharts cards.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "today_history"
    _attr_name = "Today's app usage"
    _attr_icon = "mdi:history"

    def __init__(self, coordinator, store, profile_id: str, profile_display_name: str) -> None:
        super().__init__(coordinator)
        self._store = store
        self._profile_id = profile_id
        self._attr_unique_id = f"{profile_id}_today_history"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, profile_id)},
            "name": f"Apple TV Mgmt — {profile_display_name}",
            "manufacturer": "Apple TV Mgmt",
            "model": "Profile",
        }

    @property
    def native_value(self) -> int:
        """State = number of distinct apps used today."""
        now = dt_util.utcnow()
        return len(
            self._store.app_totals_today(
                self._profile_id, now=now, rollover_hour=self._rollover_hour()
            )
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        now = dt_util.utcnow()
        totals = self._store.app_totals_today(
            self._profile_id, now=now, rollover_hour=self._rollover_hour()
        )
        apps = [
            {
                "bundle_id": bid,
                "display_name": app_display_name(bid),
                "total_minutes": round(int(slot["seconds"]) / 60, 1),
                "sessions": int(slot["sessions"]),
            }
            for bid, slot in totals.items()
        ]
        apps.sort(key=lambda a: a["total_minutes"], reverse=True)

        events_today = self._store.events_today(
            self._profile_id, now=now, rollover_hour=self._rollover_hour()
        )
        events = [
            {
                "bundle_id": e.bundle_id,
                "display_name": app_display_name(e.bundle_id),
                "started_at": e.started_at.isoformat(),
                "ended_at": e.ended_at.isoformat() if e.ended_at else None,
                "duration_minutes": round(e.duration_seconds(now=now) / 60, 1),
                "open": e.ended_at is None,
            }
            for e in events_today
        ]
        return {"apps": apps, "events": events}


# v0.15.0 — effective_state sensor (spec §3.3 / §3.4.1) -------------------

# State-machine values from .state (re-imported here to avoid pulling in
# the full module + circulars). Kept in lockstep with state.py.
_STATE_OK = "ok"
_STATE_WARNING = "warning"
_STATE_GRACE = "grace"
_STATE_ENFORCING = "enforcing"

# Monitor-mode collapse: per spec §3.3 rows 3-5, all three "active"
# state-machine values map to a single banner family.
_MONITOR_COLLAPSE = {
    _STATE_OK: "observing",
    _STATE_WARNING: "observing_warn",
    _STATE_GRACE: "observing_over_budget",
    _STATE_ENFORCING: "observing_over_budget",
}


class EffectiveStateSensor(CoordinatorEntity[AppleTVMgmtCoordinator], SensorEntity):
    """User-facing collapse of (mode × state-machine × adult × enforcement_failed).

    Implements the 10-row matrix in spec §3.3. The raw
    `sensor.<profile>_enforcement_state` is kept for back-compat; this
    new sensor is the one the panel + Lovelace card should consume.

    Precedence (highest first), matching `should_act` plus the
    `enforcing_failed` row:
      1. adult_mode_active     → "adult_mode"
      2. mode=paused           → "paused"
      3. mode=enforced + raw=enforcing + enforcement_failed → "enforcing_failed"
      4. mode=monitor_only     → observing[_warn|_over_budget] (collapsed)
      5. mode=enforced         → ok | warning | grace | enforcing (passthrough)
    """

    _attr_has_entity_name = True
    _attr_translation_key = "effective_state"
    _attr_name = "Effective state"
    _attr_icon = "mdi:shield-search"

    def __init__(
        self,
        coordinator: AppleTVMgmtCoordinator,
        store,           # storage.AppleTVMgmtStore (avoid circular import)
        profile,         # storage.Profile
    ) -> None:
        super().__init__(coordinator)
        self._store = store
        self._profile = profile
        self._attr_unique_id = f"{profile.id}_effective_state"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, profile.id)},
            "name": f"Apple TV Mgmt — {profile.display_name}",
            "manufacturer": "Apple TV Mgmt",
            "model": "Profile",
        }

    @property
    def native_value(self) -> str:
        # Resolve the should_act decision from store + live mode. The
        # mode is read off the in-memory Profile (kept in sync by the
        # select entity and the legacy switches in B6) — this avoids
        # an extra store fetch on every state read.
        now = dt_util.utcnow()
        decision = should_act(
            mode=getattr(self._profile, "mode", "enforced"),
            adult_mode_until=self._store.adult_mode_until(self._profile.id),
            now=now,
        )

        # 1. Adult mode wins over everything.
        if decision.reason == "adult_mode":
            return "adult_mode"
        # 2. Paused wins over monitor/enforced (but not adult).
        if decision.reason == "paused":
            return "paused"

        data = self.coordinator.data or {}
        raw = data.get("enforcement_state") or _STATE_OK

        # 3. Monitor mode collapses the four state-machine values.
        if decision.reason == "monitor_only":
            return _MONITOR_COLLAPSE.get(raw, "observing")

        # 4. ACT path — enforced mode. Check for the enforcing_failed
        # case (spec row 10) before passing raw through.
        if raw == _STATE_ENFORCING and data.get("enforcement_failed"):
            return "enforcing_failed"
        return raw
