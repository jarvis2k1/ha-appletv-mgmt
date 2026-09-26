"""Config & options flow for Apple TV Mgmt.

Single-step user flow: pick the Apple TV entity, point at AdGuard, set the
budget. Validates AdGuard credentials by trying to fetch the named client.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ADGUARD_API_KEY,
    CONF_ADGUARD_CLIENT_NAME,
    CONF_ADGUARD_PASSWORD,
    CONF_ADGUARD_URL,
    CONF_ADGUARD_USER,
    CONF_APPLE_TV_ENTITY,
    CONF_DAILY_BUDGET_MIN,
    CONF_DEVICE_KIND,
    CONF_ENFORCEMENT_SWITCH_ENTITY_ID,
    CONF_GRACE_SECONDS,
    CONF_IDLE_GRACE_MINUTES,
    CONF_ADULT_MODE_DURATION_MIN,
    CONF_GROUP_BUDGETS,
    CONF_NOTIFY_TARGET,
    CONF_PROFILE_NAME,
    CONF_QUIET_WINDOWS,
    CONF_REQUEST_EXPIRE_MIN,
    CONF_TV_ENTITY_ID,
    CONF_TV_SHUTDOWN_ENABLED,
    DEFAULT_DAILY_BUDGET_MIN,
    DEFAULT_GRACE_SECONDS,
    DEFAULT_IDLE_GRACE_MINUTES,
    DEFAULT_ADULT_MODE_DURATION_MIN,
    DEFAULT_GROUP_BUDGETS,
    DEFAULT_PROFILE_NAME,
    DEFAULT_QUIET_WINDOWS,
    DEFAULT_REQUEST_EXPIRE_MIN,
    DEFAULT_TV_SHUTDOWN_ENABLED,
    DEVICE_KIND_APPLE_TV,
    DEVICE_KIND_XBOX_PRESENCE,
    DEVICE_KINDS,
    DOMAIN,
)
from .categorize import ALL_GROUPS
from .enforcer import AdGuardClient, AdGuardError
from .quiet import validate_windows_string

_LOGGER = logging.getLogger(__name__)


def _device_kind_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """v0.19.0 — first step asks which device kind to set up."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_DEVICE_KIND, default=d.get(CONF_DEVICE_KIND, DEVICE_KIND_APPLE_TV)
            ): vol.In(list(DEVICE_KINDS)),
        }
    )


def _user_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Apple TV profile schema (the v0.1.x — v0.18.x default path)."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_PROFILE_NAME, default=d.get(CONF_PROFILE_NAME, DEFAULT_PROFILE_NAME)
            ): str,
            vol.Required(
                CONF_APPLE_TV_ENTITY, default=d.get(CONF_APPLE_TV_ENTITY)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="media_player")
            ),
            # AdGuard Home is an OPTIONAL supplementary DNS-block layer. Leave
            # the URL blank to run without it — enforcement then relies on the
            # media_player.turn_off + TV-shutdown path (and `enable_adguard_block`
            # defaults OFF anyway). When the URL is set, the client name is
            # required and the pair is validated before the entry is created.
            vol.Optional(CONF_ADGUARD_URL, default=d.get(CONF_ADGUARD_URL, "")): str,
            # API key path (used with the appletv_adguard_proxy addon).
            vol.Optional(
                CONF_ADGUARD_API_KEY, default=d.get(CONF_ADGUARD_API_KEY, "")
            ): str,
            # Basic-auth path (used when hitting AdGuard directly with HTTP auth).
            vol.Optional(CONF_ADGUARD_USER, default=d.get(CONF_ADGUARD_USER, "")): str,
            vol.Optional(
                CONF_ADGUARD_PASSWORD, default=d.get(CONF_ADGUARD_PASSWORD, "")
            ): str,
            vol.Optional(
                CONF_ADGUARD_CLIENT_NAME, default=d.get(CONF_ADGUARD_CLIENT_NAME, "")
            ): str,
            vol.Required(
                CONF_DAILY_BUDGET_MIN,
                default=d.get(CONF_DAILY_BUDGET_MIN, DEFAULT_DAILY_BUDGET_MIN),
            ): vol.All(int, vol.Range(min=1, max=24 * 60)),
        }
    )


def _xbox_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """v0.19.0 — Xbox-via-FRITZ profile schema. No AdGuard credentials —
    enforcement is a switch flip, presence is a device_tracker."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_PROFILE_NAME, default=d.get(CONF_PROFILE_NAME, "Xbox")
            ): str,
            # The presence sensor. Typically the FRITZ!Box device_tracker.* for
            # the Xbox by MAC address. Activity is "this is at home" (= on the
            # network); off / not_home = "powered down or unplugged".
            vol.Required(
                CONF_APPLE_TV_ENTITY, default=d.get(CONF_APPLE_TV_ENTITY)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="device_tracker")
            ),
            # The internet-access switch the enforcer flips OFF to block Xbox
            # traffic when the daily budget is exhausted. Typically the FRITZ!
            # Box-driven switch.<host>_internet_access entity.
            vol.Required(
                CONF_ENFORCEMENT_SWITCH_ENTITY_ID,
                default=d.get(CONF_ENFORCEMENT_SWITCH_ENTITY_ID),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="switch")
            ),
            vol.Required(
                CONF_DAILY_BUDGET_MIN,
                default=d.get(CONF_DAILY_BUDGET_MIN, DEFAULT_DAILY_BUDGET_MIN),
            ): vol.All(int, vol.Range(min=1, max=24 * 60)),
        }
    )


async def _validate_adguard(
    session: aiohttp.ClientSession, data: dict[str, Any]
) -> str | None:
    """Probe AdGuard. Returns an error key or None on success.

    AdGuard is optional: an empty URL means "no AdGuard" and validation is
    skipped so families without AdGuard Home can complete setup. When a URL
    IS provided, a client name is required (so the DNS-block layer has a
    target) and the pair is probed live.
    """
    if not (data.get(CONF_ADGUARD_URL) or "").strip():
        return None  # no AdGuard configured — nothing to validate
    if not (data.get(CONF_ADGUARD_CLIENT_NAME) or "").strip():
        return "client_name_required"
    client = AdGuardClient(
        session,
        data[CONF_ADGUARD_URL],
        username=data.get(CONF_ADGUARD_USER) or None,
        password=data.get(CONF_ADGUARD_PASSWORD) or None,
        api_key=data.get(CONF_ADGUARD_API_KEY) or None,
    )
    try:
        await client.get_client(data[CONF_ADGUARD_CLIENT_NAME])
    except AdGuardError as err:
        msg = str(err).lower()
        if "not found" in msg:
            _LOGGER.warning("AdGuard client %r not found", data[CONF_ADGUARD_CLIENT_NAME])
            return "client_not_found"
        _LOGGER.warning("AdGuard returned an error: %s", err)
        return "cannot_connect"
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as err:
        _LOGGER.warning(
            "Cannot reach AdGuard at %s: %s: %s",
            data[CONF_ADGUARD_URL],
            type(err).__name__,
            err,
        )
        return "cannot_connect"
    except Exception:  # noqa: BLE001 -- surface anything we didn't anticipate
        _LOGGER.exception("Unexpected error validating AdGuard config")
        return "unknown"
    return None


class AppleTVMgmtConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """v0.19.0 — first step picks the device kind.

        Dispatches to async_step_apple_tv (existing Apple TV flow) or
        async_step_xbox (new Xbox-via-FRITZ flow). On a single-device install
        the chooser shows just one radio and is one click — minimal friction.
        """
        if user_input is not None:
            kind = user_input.get(CONF_DEVICE_KIND, DEVICE_KIND_APPLE_TV)
            if kind == DEVICE_KIND_XBOX_PRESENCE:
                return await self.async_step_xbox()
            return await self.async_step_apple_tv()

        return self.async_show_form(
            step_id="user",
            data_schema=_device_kind_schema(),
        )

    async def async_step_apple_tv(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """v0.1.x — v0.18.x Apple TV path, now reached via the device-kind chooser."""
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            error = await _validate_adguard(session, user_input)
            if error:
                errors["base"] = error
            else:
                # One config entry per ATV entity.
                await self.async_set_unique_id(user_input[CONF_APPLE_TV_ENTITY])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_PROFILE_NAME],
                    data={**user_input, CONF_DEVICE_KIND: DEVICE_KIND_APPLE_TV},
                )

        return self.async_show_form(
            step_id="apple_tv",
            data_schema=_user_schema(user_input),
            errors=errors,
        )

    async def async_step_xbox(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """v0.19.0 — Xbox-via-FRITZ path. No AdGuard validation step (no
        AdGuard credentials needed for this kind); enforcement is a switch
        flip on the user-selected enforcement_switch_entity_id."""
        if user_input is not None:
            # One config entry per presence entity (same uniqueness contract
            # as the Apple TV path).
            await self.async_set_unique_id(user_input[CONF_APPLE_TV_ENTITY])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input[CONF_PROFILE_NAME],
                data={**user_input, CONF_DEVICE_KIND: DEVICE_KIND_XBOX_PRESENCE},
            )

        return self.async_show_form(
            step_id="xbox",
            data_schema=_xbox_schema(user_input),
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return AppleTVMgmtOptionsFlow(entry)


class AppleTVMgmtOptionsFlow(OptionsFlow):
    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            # Validate quiet windows before saving — bad input is rejected
            # with a meaningful error instead of silently failing on next
            # tick. Trim whitespace so trailing-comma input is forgiving.
            raw_windows = (user_input.get(CONF_QUIET_WINDOWS) or "").strip()
            try:
                validate_windows_string(raw_windows)
            except ValueError as err:
                _LOGGER.warning("Invalid quiet_windows %r: %s", raw_windows, err)
                errors[CONF_QUIET_WINDOWS] = "invalid_quiet_windows"
            else:
                user_input[CONF_QUIET_WINDOWS] = raw_windows
                # Collapse the per-group `budget_*` fields back into a
                # single dict so the rest of the code only sees one shape.
                group_budgets = {
                    g: int(user_input.pop(f"budget_{g}"))
                    for g in ALL_GROUPS
                    if user_input.get(f"budget_{g}") is not None
                }
                user_input[CONF_GROUP_BUDGETS] = group_budgets
                return self.async_create_entry(title="", data=user_input)

        merged = {**self._entry.data, **self._entry.options}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DAILY_BUDGET_MIN,
                    default=merged.get(CONF_DAILY_BUDGET_MIN, DEFAULT_DAILY_BUDGET_MIN),
                ): vol.All(int, vol.Range(min=1, max=24 * 60)),
                vol.Required(
                    CONF_GRACE_SECONDS,
                    default=merged.get(CONF_GRACE_SECONDS, DEFAULT_GRACE_SECONDS),
                ): vol.All(int, vol.Range(min=0, max=600)),
                vol.Required(
                    CONF_IDLE_GRACE_MINUTES,
                    default=merged.get(
                        CONF_IDLE_GRACE_MINUTES, DEFAULT_IDLE_GRACE_MINUTES
                    ),
                ): vol.All(int, vol.Range(min=0, max=60)),
                # Optional TV shutdown — both fields are optional. The toggle
                # below is also exposed as a switch entity so the user can
                # enable/disable from the dashboard without reopening this
                # form. Pick any HA media_player entity for the TV.
                vol.Optional(
                    CONF_TV_ENTITY_ID,
                    description={"suggested_value": merged.get(CONF_TV_ENTITY_ID)},
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="media_player")
                ),
                vol.Required(
                    CONF_TV_SHUTDOWN_ENABLED,
                    default=merged.get(
                        CONF_TV_SHUTDOWN_ENABLED, DEFAULT_TV_SHUTDOWN_ENABLED
                    ),
                ): bool,
                # v0.21.1 (FIX 3) — native-TV tracking is PATCH/panel-
                # authoritative ONLY (like secondary_devices /
                # enforcement_switch_entity_id). track_native_tv +
                # native_tv_excluded_sources are deliberately NOT in this
                # options schema: the selective-merge preserve loops make the
                # stored (PATCHed) value always win, so an Options-UI edit here
                # was a silent no-op in both directions — and the disable
                # direction was dangerous (UI showed off while the TV still got
                # killed). Enable native TV via the panel / PATCH /limits.
                # (budget_linear_tv stays below in the per-group budgets loop.)
                # Quiet windows — comma-separated "HH:MM-HH:MM" pairs.
                # Optional label after a second colon: "HH:MM-HH:MM:Bedtime".
                # Examples:
                #     "20:30-07:00:Bedtime"
                #     "12:00-14:00:Lunch, 20:30-07:00:Bedtime"
                # See quiet.py for the full format spec.
                vol.Optional(
                    CONF_QUIET_WINDOWS,
                    default=merged.get(CONF_QUIET_WINDOWS, DEFAULT_QUIET_WINDOWS),
                ): str,
                # Adult-mode duration in minutes — when the switch is
                # flipped on, ALL enforcement is bypassed for this long.
                vol.Required(
                    CONF_ADULT_MODE_DURATION_MIN,
                    default=merged.get(
                        CONF_ADULT_MODE_DURATION_MIN, DEFAULT_ADULT_MODE_DURATION_MIN
                    ),
                ): vol.All(int, vol.Range(min=5, max=24 * 60)),
                **{
                    # Per-group budgets — one optional minutes field per group.
                    vol.Optional(
                        f"budget_{g}",
                        default=(merged.get(CONF_GROUP_BUDGETS) or {}).get(
                            g, DEFAULT_GROUP_BUDGETS.get(g)
                        ),
                    ): vol.All(int, vol.Range(min=0, max=24 * 60))
                    for g in ALL_GROUPS
                },
                # v0.22.0 — the integration-specific REST api_key is gone.
                # The API now requires Home Assistant's own authentication
                # (a long-lived access token), so a second home-grown key
                # added no security and hid the fact that a fresh install
                # had no protection at all.
                vol.Optional(
                    CONF_NOTIFY_TARGET,
                    description={
                        "suggested_value": merged.get(CONF_NOTIFY_TARGET) or ""
                    },
                ): str,
                vol.Required(
                    CONF_REQUEST_EXPIRE_MIN,
                    default=merged.get(
                        CONF_REQUEST_EXPIRE_MIN, DEFAULT_REQUEST_EXPIRE_MIN
                    ),
                ): vol.All(int, vol.Range(min=1, max=60)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
