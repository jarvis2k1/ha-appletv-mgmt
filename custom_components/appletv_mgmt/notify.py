"""HA Companion actionable notification for parent approvals.

A kid (via OpenClaw or the REST API directly) creates an `ExtensionRequest`
asking for more screen time. This module:

1. Builds and sends an actionable push notification to the configured
   parent's HA Companion device (`notify.<service>`).
2. Listens for the `mobile_app_notification_action` event when the parent
   taps Approve/Approve-half/Deny on their phone.
3. Updates the request in the store, grants minutes if approved, fires
   `appletv_mgmt_request_decided`, and triggers a coordinator refresh so
   the next tick clears the block.

Action ids encode the request id and the chosen action:

    APPLETV_MGMT_APPROVE_FULL_<req_id>     grant exactly requested_minutes
    APPLETV_MGMT_APPROVE_HALF_<req_id>     grant requested_minutes // 2
    APPLETV_MGMT_DENY_<req_id>             mark denied, no minutes granted
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback

from .const import (
    DOMAIN,
    EVENT_REQUEST_DECIDED,
    NOTIFY_ACTION_PREFIX,
    app_display_name,
)
from .storage import AppleTVMgmtStore, ExtensionRequest, Profile

_LOGGER = logging.getLogger(__name__)

_ACTION_APPROVE_FULL = "APPROVE_FULL"
_ACTION_APPROVE_HALF = "APPROVE_HALF"
_ACTION_DENY = "DENY"

_NOTIFY_ACTION_EVENT = "mobile_app_notification_action"


def _resolve_request_group(
    hass: HomeAssistant, bundle: dict, request: ExtensionRequest
) -> str | None:
    """v0.19.1 — pick the group to tag a request-approval extension to.

    Order: the currently binding/active group (so an approval relieves
    whatever is being nagged right now), else the group of the app the kid
    was using when they requested (`request.bundle_id`), else None
    (daily-only). Mirrors the panel/service grant resolution but adds the
    request's own bundle as a fallback for approvals decided after the kid
    has stopped watching.
    """
    from .audit import resolve_extension_target_group

    group = resolve_extension_target_group(hass, request.profile_id)
    if group is not None:
        return group
    coord = bundle.get("coordinator")
    if coord is not None and getattr(request, "bundle_id", None):
        try:
            return coord._group_for(request.bundle_id)
        except Exception:  # noqa: BLE001 — best-effort fallback; daily-only on error
            return None
    return None


async def send_request_notification(
    hass: HomeAssistant,
    *,
    request: ExtensionRequest,
    profile: Profile,
    notify_target: str,
) -> None:
    """Push an actionable HA Companion notification to the parent."""
    app_label = app_display_name(request.bundle_id) if request.bundle_id else None
    title = f"{profile.display_name}: {request.requested_minutes} min screen-time request"
    msg_parts = []
    if app_label:
        msg_parts.append(f"App: {app_label}")
    if request.reason:
        msg_parts.append(f"Reason: {request.reason}")
    message = " — ".join(msg_parts) or "Tap to decide."

    actions: list[dict[str, str]] = [
        {
            "action": f"{NOTIFY_ACTION_PREFIX}{_ACTION_APPROVE_FULL}_{request.id}",
            "title": f"Approve {request.requested_minutes} min",
        }
    ]
    half = max(1, request.requested_minutes // 2)
    if half != request.requested_minutes:
        actions.append(
            {
                "action": f"{NOTIFY_ACTION_PREFIX}{_ACTION_APPROVE_HALF}_{request.id}",
                "title": f"Approve {half} min",
            }
        )
    actions.append(
        {
            "action": f"{NOTIFY_ACTION_PREFIX}{_ACTION_DENY}_{request.id}",
            "title": "Deny",
            "destructive": True,
        }
    )

    try:
        await hass.services.async_call(
            "notify",
            notify_target,
            {
                "title": title,
                "message": message,
                "data": {
                    "tag": f"appletv_mgmt_request_{request.id}",
                    "group": "appletv-mgmt",
                    "actions": actions,
                },
            },
            blocking=False,
        )
    except Exception:  # noqa: BLE001 -- notify.* exists or it doesn't; log and continue
        _LOGGER.exception(
            "Could not send actionable notification via notify.%s for request %s",
            notify_target,
            request.id,
        )


@callback
def register_action_handler(hass: HomeAssistant) -> callable:
    """Subscribe to `mobile_app_notification_action`. Returns an unsubscribe fn."""

    async def _on_action(event: Event) -> None:
        action = event.data.get("action") or ""
        if not action.startswith(NOTIFY_ACTION_PREFIX):
            return  # not ours

        rest = action[len(NOTIFY_ACTION_PREFIX) :]
        # rest is like "APPROVE_FULL_<req_id>" or "DENY_<req_id>".
        # Bundle ids never contain spaces but request ids might contain '_'
        # (they're ulid-shaped — no underscores in practice). Split off
        # the longest known prefix.
        for prefix in (
            _ACTION_APPROVE_FULL,
            _ACTION_APPROVE_HALF,
            _ACTION_DENY,
        ):
            if rest.startswith(prefix + "_"):
                req_id = rest[len(prefix) + 1 :]
                await _handle_decision(hass, prefix, req_id, source="companion")
                return
        _LOGGER.debug("Ignoring unknown notification action %s", action)

    return hass.bus.async_listen(_NOTIFY_ACTION_EVENT, _on_action)


async def _handle_decision(
    hass: HomeAssistant,
    decision: str,
    request_id: str,
    *,
    source: str,
) -> None:
    """Apply Approve / Deny to a request. Called from the bus handler AND
    from the REST API write endpoint (DRY)."""
    # Find which config-entry owns this request.
    for entry_id, bundle in hass.data.get(DOMAIN, {}).items():
        store: AppleTVMgmtStore = bundle["store"]
        request = store.get_request(request_id)
        if request is None:
            continue
        if request.status != "pending":
            _LOGGER.info(
                "Decision %s for request %s ignored — already %s",
                decision,
                request_id,
                request.status,
            )
            return

        granted = 0
        if decision == _ACTION_APPROVE_FULL:
            granted = request.requested_minutes
            new_status = "approved"
        elif decision == _ACTION_APPROVE_HALF:
            granted = max(1, request.requested_minutes // 2)
            new_status = "approved"
        elif decision == _ACTION_DENY:
            granted = 0
            new_status = "denied"
        else:
            _LOGGER.warning("Unknown decision %r for request %s", decision, request_id)
            return

        store.update_request(
            request_id, status=new_status, granted_minutes=granted, decided_by=source
        )
        if granted > 0:
            # v0.19.1 — tag the grant to the binding/active group (or the
            # group of the app the kid requested time for) so it lifts that
            # GROUP budget, not just the daily pool. Same fix as the panel
            # "+min" button — otherwise approving "more movie time" leaves
            # the movies cap pinning and the nagging continues.
            store.add_extension_minutes(
                request.profile_id,
                granted,
                group=_resolve_request_group(hass, bundle, request),
            )
        await store.async_save()

        hass.bus.async_fire(
            EVENT_REQUEST_DECIDED,
            {
                "profile_id": request.profile_id,
                "request_id": request.id,
                "status": new_status,
                "granted_minutes": granted,
                "decided_by": source,
            },
        )
        await bundle["coordinator"].async_request_refresh()
        return

    _LOGGER.info("No request found for id %s", request_id)


async def handle_external_decision(
    hass: HomeAssistant, request_id: str, *, approve: bool, minutes: int | None
) -> None:
    """Apply a decision arriving from a non-Companion source (e.g. the
    REST API). `minutes=None` means "grant exactly requested_minutes".

    Rewritten in v0.10.0 to apply the decision atomically: prior version
    routed through `_handle_decision(APPROVE_HALF)` then corrected with a
    delta, which fired `EVENT_REQUEST_DECIDED` with the wrong
    `granted_minutes` (caught by QA review).
    """
    if not approve:
        await _handle_decision(hass, _ACTION_DENY, request_id, source="api")
        return

    for bundle in hass.data.get(DOMAIN, {}).values():
        store: AppleTVMgmtStore = bundle["store"]
        req = store.get_request(request_id)
        if req is None:
            continue
        if req.status != "pending":
            _LOGGER.info(
                "Request %s already decided (%s); ignoring API approve",
                request_id,
                req.status,
            )
            return

        granted = req.requested_minutes if minutes is None else int(minutes)
        if granted < 0:
            granted = 0
        store.update_request(
            request_id,
            status="approved",
            granted_minutes=granted,
            decided_by="api",
        )
        if granted > 0:
            # v0.19.1 — group-tag (see _handle_decision above).
            store.add_extension_minutes(
                req.profile_id,
                granted,
                group=_resolve_request_group(hass, bundle, req),
            )
        await store.async_save()
        hass.bus.async_fire(
            EVENT_REQUEST_DECIDED,
            {
                "profile_id": req.profile_id,
                "request_id": req.id,
                "status": "approved",
                "granted_minutes": granted,
                "decided_by": "api",
            },
        )
        await bundle["coordinator"].async_request_refresh()
        return

    _LOGGER.info("No request found for id %s (external decision)", request_id)
