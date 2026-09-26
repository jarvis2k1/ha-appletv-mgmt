"""The Apple TV Mgmt integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ADGUARD_API_KEY,
    CONF_ADGUARD_CLIENT_NAME,
    CONF_ADGUARD_PASSWORD,
    CONF_ADGUARD_URL,
    CONF_ADGUARD_USER,
    CONF_ADULT_MODE_DURATION_MIN,
    CONF_APPLE_TV_ENTITY,
    CONF_DAILY_BUDGET_MIN,
    CONF_DEVICE_KIND,
    CONF_SECONDARY_DEVICES,
    CONF_ENFORCEMENT_ENABLED,
    CONF_ENFORCEMENT_SWITCH_ENTITY_ID,
    CONF_GRACE_SECONDS,
    CONF_GROUP_BUDGETS,
    CONF_IDLE_GRACE_MINUTES,
    CONF_PROFILE_NAME,
    CONF_QUIET_WINDOWS,
    CONF_TRACK_NATIVE_TV,
    CONF_NATIVE_TV_EXCLUDED_SOURCES,
    CONF_TV_ENTITY_ID,
    CONF_TV_SHUTDOWN_ENABLED,
    CONF_WARN_THRESHOLDS,
    DEVICE_KIND_APPLE_TV,
    DEVICE_KIND_XBOX_PRESENCE,
    DEFAULT_DAILY_BUDGET_MIN,
    DEFAULT_ENFORCEMENT_ENABLED,
    DEFAULT_GRACE_SECONDS,
    DEFAULT_IDLE_GRACE_MINUTES,
    DEFAULT_ADULT_MODE_DURATION_MIN,
    DEFAULT_GROUP_BUDGETS,
    DEFAULT_NATIVE_TV_EXCLUDED_SOURCES,
    DEFAULT_PROFILE_NAME,
    DEFAULT_QUIET_WINDOWS,
    DEFAULT_TRACK_NATIVE_TV,
    DEFAULT_TV_SHUTDOWN_ENABLED,
    DEFAULT_WARN_THRESHOLDS,
    DOMAIN,
)
from .api import register_views
from .audit import register_action_recorder
from .coordinator import AppleTVMgmtCoordinator
from .enforcer import AdGuardClient, EnforcementController
from .notify import register_action_handler
from .storage import AppleTVMgmtStore, Profile

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SELECT, Platform.SWITCH]

SERVICE_FORCE_BLOCK = "force_block"
SERVICE_GRANT_EXTENSION = "grant_extension"
SERVICE_RESET_USAGE = "reset_usage"

GRANT_EXTENSION_SCHEMA = vol.Schema(
    {
        vol.Required("profile_id"): cv.string,
        vol.Required("minutes"): vol.All(vol.Coerce(int), vol.Range(min=-240, max=240)),
        # v0.19.1 — optional group tag. Omit to auto-detect the binding/active
        # group; pass "daily" to force the daily-only pool; pass a group name
        # (movies/tv_shows/gaming/other) to target a specific group budget.
        vol.Optional("group"): cv.string,
    }
)
PROFILE_ONLY_SCHEMA = vol.Schema({vol.Required("profile_id"): cv.string})


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the integration's domain-level data once per HA boot."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry — one Apple TV Mgmt profile."""
    data = entry.data
    options = entry.options

    # v0.15.0 — load the store FIRST so we can resolve `mode` and
    # `tv_shutdown_target` from the stored Profile (those two fields are
    # store-primary per spec §4.2). All other fields stay options-primary
    # via the selective-merge logic further down (per Opus NS4).
    #
    # v0.20.1 — the store is a SINGLETON per HA boot, shared across every
    # config entry. Before this, each entry created its own AppleTVMgmtStore
    # bound to the SAME storage key, and each async_save wrote the full
    # snapshot from its own in-memory view — so a family with two profiles
    # (two kids / two devices) got last-writer-wins clobbering of usage,
    # extensions and runtime state (one kid's whole day could revert on
    # restart). Create it once (sibling key, same pattern as _handlers) and
    # share the instance so all entries read/write one coherent view.
    store_key = f"{DOMAIN}_store"
    if store_key not in hass.data:
        _store = AppleTVMgmtStore(hass)
        await _store.async_load()
        # setdefault guards the (rare) concurrent-entry-setup race: whoever
        # registers first wins; a loser's redundant load is discarded.
        hass.data.setdefault(store_key, _store)
    store = hass.data[store_key]
    stored_profile = store.get_profile(entry.entry_id)

    # mode / tv_shutdown_target / tv_entity_id come from the stored Profile.
    # The legacy entry.options values for enforcement_enabled and
    # tv_shutdown_enabled are kept in sync (via the reconciliation block
    # at the end of this function) so legacy switch entities behave
    # consistently when re-enabled via the registry.
    resolved_mode = stored_profile.mode if stored_profile else "enforced"
    resolved_tv_shutdown_target = (
        stored_profile.tv_shutdown_target if stored_profile else None
    )

    # v0.18.0 — resolve the Apple TV core integration's ConfigEntry.entry_id
    # for the proactive pyatv reload feature (see
    # media_attribution.decide_pyatv_reload + coordinator._check_pyatv_reload).
    # The entity registry maps apple_tv_entity_id -> RegistryEntry whose
    # `config_entry_id` IS the entry that owns the media_player.apple_tv_*
    # entity (i.e. the pyatv integration's entry id). Stored Profile value
    # (set via PATCH /limits, if any) wins over the registry lookup.
    resolved_atv_entry_id: str = ""
    if stored_profile and getattr(stored_profile, "apple_tv_entry_id", ""):
        resolved_atv_entry_id = stored_profile.apple_tv_entry_id
    else:
        try:
            from homeassistant.helpers import entity_registry as er
            reg = er.async_get(hass)
            ent = reg.async_get(data[CONF_APPLE_TV_ENTITY])
            if ent is not None and ent.config_entry_id:
                resolved_atv_entry_id = str(ent.config_entry_id)
        except Exception as err:  # noqa: BLE001 — feature is best-effort
            _LOGGER.debug(
                "v0.18.0 apple_tv_entry_id resolution skipped (non-fatal): %s",
                err,
            )

    # v0.19.0 — device_kind dispatch on the config-entry shape. Xbox
    # profiles' entry.data has no AdGuard credentials (the Xbox config
    # flow doesn't collect them), so reads against the AdGuard keys
    # would KeyError. Default to "apple_tv" for entries created before
    # v0.19.0 (back-compat — they have the AdGuard fields).
    device_kind = data.get(CONF_DEVICE_KIND, DEVICE_KIND_APPLE_TV)

    profile = Profile(
        id=entry.entry_id,
        display_name=data.get(CONF_PROFILE_NAME, DEFAULT_PROFILE_NAME),
        apple_tv_entity_id=data[CONF_APPLE_TV_ENTITY],
        adguard_client_name=data.get(CONF_ADGUARD_CLIENT_NAME, ""),
        daily_budget_min=options.get(
            CONF_DAILY_BUDGET_MIN, data.get(CONF_DAILY_BUDGET_MIN, DEFAULT_DAILY_BUDGET_MIN)
        ),
        grace_seconds=options.get(
            CONF_GRACE_SECONDS, data.get(CONF_GRACE_SECONDS, DEFAULT_GRACE_SECONDS)
        ),
        warn_thresholds_min=options.get(
            CONF_WARN_THRESHOLDS, data.get(CONF_WARN_THRESHOLDS, DEFAULT_WARN_THRESHOLDS)
        ),
        idle_grace_minutes=options.get(
            CONF_IDLE_GRACE_MINUTES,
            data.get(CONF_IDLE_GRACE_MINUTES, DEFAULT_IDLE_GRACE_MINUTES),
        ),
        # `tv_entity_id` is the legacy "memory" — for new installs read it
        # from entry.options if the stored Profile doesn't have one yet.
        tv_entity_id=(
            stored_profile.tv_entity_id
            if stored_profile and stored_profile.tv_entity_id
            else (options.get(CONF_TV_ENTITY_ID, data.get(CONF_TV_ENTITY_ID)) or None)
        ),
        # Legacy bool kept in sync with `tv_shutdown_target`.
        tv_shutdown_enabled=resolved_tv_shutdown_target is not None,
        # Legacy bool kept in sync with `mode`.
        enforcement_enabled=resolved_mode == "enforced",
        quiet_windows=options.get(
            CONF_QUIET_WINDOWS,
            data.get(CONF_QUIET_WINDOWS, DEFAULT_QUIET_WINDOWS),
        )
        or "",
        group_budgets=dict(
            options.get(
                CONF_GROUP_BUDGETS,
                data.get(CONF_GROUP_BUDGETS, DEFAULT_GROUP_BUDGETS),
            )
            or {}
        ),
        adult_mode_duration_min=int(
            options.get(
                CONF_ADULT_MODE_DURATION_MIN,
                data.get(
                    CONF_ADULT_MODE_DURATION_MIN, DEFAULT_ADULT_MODE_DURATION_MIN
                ),
            )
        ),
        # v0.15.0 mode-redesign fields — store-primary
        mode=resolved_mode,
        tv_shutdown_target=resolved_tv_shutdown_target,
        # v0.18.0 — proactive pyatv reload target.
        apple_tv_entry_id=resolved_atv_entry_id,
        # v0.19.0 — multi-device fields. Default device_kind to "apple_tv"
        # for back-compat (pre-v0.19.0 config entries have no key).
        device_kind=device_kind,
        enforcement_switch_entity_id=data.get(
            CONF_ENFORCEMENT_SWITCH_ENTITY_ID
        ),
        # v0.20.0 — secondary devices folded into this profile's single budget.
        # Options-primary (a future Options-flow value applies immediately) then
        # entry.data, defaulting to []. The PATCHed value (the live-migration
        # path) is overlaid from the store by the selective-merge block below.
        secondary_devices=(
            options.get(CONF_SECONDARY_DEVICES)
            or data.get(CONF_SECONDARY_DEVICES)
            or []
        ),
        # v0.21.0 — native TV watching. Options-primary → entry.data → default
        # (off). A PATCHed value is overlaid from the store by the selective-
        # merge block below (restart-survival). Default off ⇒ zero change for
        # existing installs.
        track_native_tv=bool(
            options.get(
                CONF_TRACK_NATIVE_TV,
                data.get(CONF_TRACK_NATIVE_TV, DEFAULT_TRACK_NATIVE_TV),
            )
        ),
        native_tv_excluded_sources=list(
            options.get(
                CONF_NATIVE_TV_EXCLUDED_SOURCES,
                data.get(
                    CONF_NATIVE_TV_EXCLUDED_SOURCES,
                    DEFAULT_NATIVE_TV_EXCLUDED_SOURCES,
                ),
            )
            or DEFAULT_NATIVE_TV_EXCLUDED_SOURCES
        ),
    )

    # Preserve fields that were edited via the REST PATCH endpoint
    # (v0.11.0). These don't live in the config entry, so without this
    # merge they'd be silently reset to defaults on every HA restart.
    existing = stored_profile  # alias for clarity within this block
    if existing is not None:
        profile.weekday_budgets_min = dict(existing.weekday_budgets_min or {})
        profile.weekday_group_budgets_min = {
            wd: dict(g) for wd, g in (existing.weekday_group_budgets_min or {}).items()
        }
        profile.weekday_quiet_windows = dict(existing.weekday_quiet_windows or {})
        # v0.13.0/v0.14.x — voice + monitor fields also live only in
        # the store (no CONF_ key + entry.options surface yet). Same
        # restart-survival treatment.
        for attr in (
            "notify_media_player_entity_id",
            "notify_tts_entity_id",
            "notify_tts_language",
            "notify_volume",
            "warning_message",
            "enforce_message",
            "extension_message",
        ):
            val = getattr(existing, attr, None)
            if val is not None and val != "":
                setattr(profile, attr, val)
        # v0.15.0 — new bool opt-in flags: always preserve stored value
        # (False is a meaningful state, not "unset", so don't skip on default).
        # v0.21.0 — track_native_tv joins this list so a PATCHed opt-in
        # survives HA restart (it's PATCH-able but not always in entry.options).
        for attr in (
            "warn_in_monitor_mode",
            "voice_on_mode_change",
            "track_native_tv",
        ):
            val = getattr(existing, attr, None)
            if isinstance(val, bool):
                setattr(profile, attr, val)
        # v0.15.0 — new voice templates (store-only; empty == disabled).
        for attr in ("adult_mode_on_message", "mode_change_message"):
            val = getattr(existing, attr, None)
            if val is not None and val != "":
                setattr(profile, attr, val)
        # v0.16.2 — restart-survival for v0.16.0 fields. The dev agent
        # added these Profile fields + PATCH validators but FORGOT to
        # extend the selective-merge list. Live-observed 2026-05-28:
        # the owner PATCHed countdown_message etc., I deployed v0.15.7→v0.16.1
        # (3 HA restarts), each silently reverted the 4 fields to "".
        # By test time, countdown_message was empty → timer scheduled
        # then aborted in should_speak (empty template). Root cause of
        # "no countdown" symptom.
        for attr in (
            "countdown_message",
            "reactivation_message_friendly",
            "reactivation_message_stern",
            "notify_parent_target",
        ):
            val = getattr(existing, attr, None)
            if val is not None and val != "":
                setattr(profile, attr, val)
        # v0.15.9 — PATCH-able numeric/dict fields that previously did
        # NOT survive HA restart. The PATCH /limits handler wrote them
        # to the store (good), but async_setup_entry rebuilt Profile
        # from entry.options on restart, silently reverting to whatever
        # the config-flow UI had last set (typically the defaults). Live-
        # observed 2026-05-28: the owner set daily_budget_min=2 via PATCH,
        # restarted HA, kid heard "Achtung noch 60 Minuten" — the budget
        # silently reverted to 60.
        #
        # Now: prefer the stored value when it's present + non-empty.
        # Empty collections (dict/list/str) are treated as "unset" so
        # the entry.options config-flow value still wins for the never-
        # PATCHed case (which is the common one on fresh installs).
        # Numeric scalars are always preferred when present — there's
        # no "unset" sentinel for them and a stored 0 is meaningful
        # (= the user deliberately set budget to zero).
        for attr in (
            "daily_budget_min", "warn_thresholds_min", "group_budgets",
            "grace_seconds", "idle_grace_minutes", "quiet_windows",
            "adult_mode_duration_min", "enable_adguard_block",
            # v0.18.0 — DNS-corroborated attribution + proactive pyatv reload.
            # Stored value wins (set via PATCH /limits); empty string is
            # "unset" so the registry-resolved value for apple_tv_entry_id
            # still wins on fresh installs.
            "apple_tv_ip", "dns_corroboration_mode", "apple_tv_entry_id",
            # v0.19.0 — Xbox MVP. enforcement_switch_entity_id is PATCH-able
            # so the parent can swap the FRITZ switch without re-running
            # the config flow. device_kind is fixed at create time and is
            # NOT in this list (the dataclass field is the source of truth).
            "enforcement_switch_entity_id",
            # v0.20.0 — secondary_devices is PATCH-able (the live-migration
            # attach path). An empty list is "unset" (the rule below skips it),
            # so the entry/options value still wins on fresh installs; a
            # non-empty PATCHed list survives every HA restart.
            "secondary_devices",
            # v0.21.0 — native TV excluded-source list. PATCH-able; a non-empty
            # stored list survives restart. Empty is "unset" (skipped below) so
            # the constructed default (["HDMI1", "HDMI2/DVI"]) still applies.
            "native_tv_excluded_sources",
            # v0.23.2 — THE day-rollover hour. v0.23.0 made this PATCH-able but
            # forgot this list, so every HA restart silently reset it to the
            # dataclass default of 0 (midnight). Live-observed: set to 5 on
            # 2026-09-02, found back at 0 on 2026-09-12 with no audit entry,
            # because nothing had "written" it — a restart had rebuilt the
            # Profile from entry.options. Exactly the v0.15.9 failure above,
            # repeated. A stored 0 is meaningful (deliberate midnight), and the
            # numeric rule below keeps it.
            "day_rollover_hour",
        ):
            val = getattr(existing, attr, None)
            if val is None:
                continue
            if isinstance(val, (dict, list, str)) and not val:
                continue
            setattr(profile, attr, val)

    store.upsert_profile(profile)
    # Drop any orphaned profiles that no longer correspond to a live config
    # entry — guards against accumulation from earlier mis-injected entries.
    live_entry_ids = {e.entry_id for e in hass.config_entries.async_entries(DOMAIN)} | {
        profile.id
    }
    removed = store.prune_profiles(live_entry_ids)
    if removed:
        _LOGGER.info("Pruned %d orphan profile(s) from storage", removed)
    await store.async_save()

    session = async_get_clientsession(hass)
    # v0.19.0 — Xbox profiles have no AdGuard credentials. The AdGuard
    # client is still instantiated (the enforcer holds a reference) but
    # with empty creds — every call would fail-open if anything tried to
    # use it. In practice the v0.19.0 dispatch in EnforcementController
    # routes Xbox enforcement to switch.turn_off/turn_on, NEVER touching
    # the AdGuard client.
    adguard = AdGuardClient(
        session,
        data.get(CONF_ADGUARD_URL, ""),
        username=data.get(CONF_ADGUARD_USER) or None,
        password=data.get(CONF_ADGUARD_PASSWORD) or None,
        api_key=data.get(CONF_ADGUARD_API_KEY) or None,
    )
    enforcer = EnforcementController(hass, adguard, profile, store=store)
    coordinator = AppleTVMgmtCoordinator(hass, store, enforcer, profile)
    await coordinator.async_start()

    hass.data[DOMAIN][entry.entry_id] = {
        "profile": profile,
        "store": store,
        "adguard": adguard,
        "enforcer": enforcer,
        "coordinator": coordinator,
        "entry": entry,
        # Populated below — kept here so async_unload_entry can call it.
        "audit_unsub": None,
    }

    # v0.15.0 — one-shot reconciliation: if the stored `mode` disagrees
    # with the legacy `entry.options[enforcement_enabled]`, write the
    # entry once so legacy switch entities (which read from options when
    # re-enabled via the registry) show the right state. This is
    # idempotent — on restart-after-first the values match and no write
    # happens. Per spec §4.2 + C7 fix (no version=2 bump, suppression
    # flag lives in hass.data[DOMAIN][entry.entry_id]).
    desired_ee = profile.mode == "enforced"
    if entry.options.get(CONF_ENFORCEMENT_ENABLED, DEFAULT_ENFORCEMENT_ENABLED) != desired_ee:
        bundle = hass.data[DOMAIN][entry.entry_id]
        bundle["_suppress_next_reload"] = True
        hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, CONF_ENFORCEMENT_ENABLED: desired_ee},
        )

    # Action-log recorder — listens for the integration's own bus events
    # (enforce/release/decision/limits) and writes a row in the audit log.
    # Profile-scoped so unloading one entry doesn't stop another's recorder.
    hass.data[DOMAIN][entry.entry_id]["audit_unsub"] = register_action_recorder(
        hass, profile_id=profile.id
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # v0.15.3 — one-shot cleanup: hide the legacy enforcement_enabled +
    # tv_shutdown switches in the entity registry. In v0.15.0 we marked
    # them `default_disabled=True`, but HA's entity registry preserves
    # the user's existing-enabled state from v0.14.x — so the owner's
    # install kept showing them after the upgrade, producing UI
    # confusion ("3 switches that all seem to control mode/tv-off"
    # — exactly the cross-wiring perception the v0.15 redesign was
    # supposed to fix). Disable them ONCE per install via the new
    # `legacy_cleanup_done` flag in entry.options. Users can re-enable
    # via Settings → Devices → Apple TV Mgmt → Entities if they have
    # automations that reference the legacy switch entity_ids.
    await _maybe_hide_legacy_switches(hass, entry, profile.id)

    _register_services(hass)
    register_views(hass)
    # Register the actionable-notification handler once per HA boot.
    # Stored in a sibling key so the per-entry bundle iteration in api.py
    # isn't polluted.
    handlers_key = f"{DOMAIN}_handlers"
    handlers = hass.data.setdefault(handlers_key, {})
    if "action_handler_unsub" not in handlers:
        handlers["action_handler_unsub"] = register_action_handler(hass)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down a config entry cleanly."""
    bundle = hass.data[DOMAIN].pop(entry.entry_id, None)
    if bundle:
        await bundle["coordinator"].async_stop()
        if audit_unsub := bundle.get("audit_unsub"):
            try:
                audit_unsub()
            except Exception:  # noqa: BLE001
                pass

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Drop services + the shared store singleton when the last entry goes away.
    if not hass.data[DOMAIN]:
        for service in (SERVICE_FORCE_BLOCK, SERVICE_GRANT_EXTENSION, SERVICE_RESET_USAGE):
            hass.services.async_remove(DOMAIN, service)
        # v0.20.1 — release the shared store so a later re-add reloads fresh
        # from disk (each coordinator.async_stop above already saved).
        hass.data.pop(f"{DOMAIN}_store", None)
    return unload_ok


LEGACY_CLEANUP_VERSION = "0.15.3"


async def _maybe_hide_legacy_switches(
    hass: HomeAssistant, entry: ConfigEntry, profile_id: str
) -> None:
    """v0.15.3 — one-shot per install: disable the legacy
    `<profile>_enforcement_enabled` + `<profile>_tv_shutdown_enabled`
    switches in the entity registry. Skipped on subsequent loads.

    Rationale: v0.15.0 set `default_disabled=True` on these switches,
    but HA preserves the user's existing-enabled state from v0.14.x —
    so existing installs kept showing 3 switches that are largely
    redundant with the new `select.<profile>_mode` (the canonical
    control). Hide them by default; user can re-enable via
    Settings → Devices → Apple TV Mgmt → Entities if they have
    automations referencing the legacy switch entity_ids.

    Marked done via the `legacy_cleanup_done` field in entry.options
    so we only do this ONCE per install (re-enables by the user
    after that are preserved).
    """
    if entry.options.get("legacy_cleanup_done") == LEGACY_CLEANUP_VERSION:
        return  # already cleaned up

    from homeassistant.helpers import entity_registry as er
    registry = er.async_get(hass)

    legacy_suffixes = ("enforcement_enabled", "tv_shutdown_enabled")
    hidden = []
    for suffix in legacy_suffixes:
        unique_id = f"{profile_id}_{suffix}"
        # entity_registry.async_get returns entry by entity_id; we need
        # to look up by (platform, domain, unique_id).
        ent_id = registry.async_get_entity_id(
            "switch", DOMAIN, unique_id
        )
        if ent_id is None:
            continue
        ent = registry.async_get(ent_id)
        if ent is None:
            continue
        if ent.disabled_by is not None:
            continue  # already disabled (manually or by integration default)
        _LOGGER.info(
            "v0.15.3 — hiding legacy switch %s (re-enable via Settings → "
            "Devices → Apple TV Mgmt → Entities if you have automations "
            "referencing it)",
            ent_id,
        )
        registry.async_update_entity(
            ent_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
        )
        hidden.append(ent_id)

    # Persist the cleanup-done flag so we don't repeat this on every load.
    # The suppress flag prevents the resulting options-update from
    # triggering a full integration reload (same pattern as the v0.15.0
    # migration in `async_setup_entry`).
    bundle = hass.data[DOMAIN].setdefault(entry.entry_id, {})
    bundle["_suppress_next_reload"] = True
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, "legacy_cleanup_done": LEGACY_CLEANUP_VERSION},
    )
    if hidden:
        _LOGGER.info(
            "v0.15.3 — hid %d legacy switch(es): %s. select.<profile>_mode "
            "is the new canonical control.",
            len(hidden), hidden,
        )


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the user edits options (budget, grace, etc.).

    v0.15.0 — the one-shot migration sync in `async_setup_entry` writes
    `entry.options[enforcement_enabled]` to match the stored `mode`.
    That write triggers this listener; we skip the reload exactly once
    via the per-entry suppression flag. Idempotent on subsequent loads.
    """
    bundle = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if bundle and bundle.pop("_suppress_next_reload", False):
        return
    await hass.config_entries.async_reload(entry.entry_id)


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_FORCE_BLOCK):
        return

    def _bundle_for(profile_id: str) -> dict[str, Any] | None:
        for bundle in hass.data[DOMAIN].values():
            if bundle["profile"].id == profile_id:
                return bundle
        return None

    async def _force_block(call: ServiceCall) -> None:
        bundle = _bundle_for(call.data["profile_id"])
        if not bundle:
            _LOGGER.warning("force_block: profile %s not found", call.data["profile_id"])
            return
        await bundle["enforcer"].force_block()

    async def _grant_extension(call: ServiceCall) -> None:
        bundle = _bundle_for(call.data["profile_id"])
        if not bundle:
            _LOGGER.warning("grant_extension: profile %s not found", call.data["profile_id"])
            return
        # v0.19.1 — tag the grant to the binding/active group (or an explicit
        # `group` field on the service call) so it lifts the GROUP budget too,
        # not just the daily pool.
        from .audit import record_extension_granted, resolve_extension_target_group
        target_group = resolve_extension_target_group(
            hass, call.data["profile_id"], call.data.get("group"),
            fallback_most_used=True,
        )
        new_total = bundle["store"].add_extension_minutes(
            call.data["profile_id"], call.data["minutes"], group=target_group
        )
        # v0.17.0 F-K — shared helper: REST POST /extension + this HA
        # service now BOTH record the audit row + fire the
        # extension-granted voice (positive minutes only). Pre-v0.17.0
        # the HA-service path was silent; the owner's homework-done automation
        # handed the kid minutes without the audible confirmation.
        record_extension_granted(
            hass,
            profile_id=call.data["profile_id"],
            minutes=call.data["minutes"],
            new_total=new_total,
            actor="ha_service",
            group=target_group,
        )
        await bundle["store"].async_save()
        # Force an immediate re-evaluation so unblock happens within seconds.
        await bundle["coordinator"].async_request_refresh()

    async def _reset_usage(call: ServiceCall) -> None:
        bundle = _bundle_for(call.data["profile_id"])
        if not bundle:
            return
        bundle["store"].reset_daily_state(call.data["profile_id"])
        # Also clear any open event so today starts clean.
        from homeassistant.util import dt as dt_util  # local import keeps deps tidy
        bundle["store"].close_open_event(call.data["profile_id"], at=dt_util.utcnow())
        await bundle["store"].async_save()
        await bundle["enforcer"].unblock()
        await bundle["coordinator"].async_request_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_FORCE_BLOCK, _force_block, schema=PROFILE_ONLY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_GRANT_EXTENSION, _grant_extension, schema=GRANT_EXTENSION_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESET_USAGE, _reset_usage, schema=PROFILE_ONLY_SCHEMA
    )
