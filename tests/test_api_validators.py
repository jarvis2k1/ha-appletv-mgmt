"""Unit tests for the pure validator helpers in api.py.

api.py pulls in heavy HA imports (`homeassistant.components.http`,
`aiohttp.web`, etc.) so we use the same stub-the-world pattern as
test_storage_helpers.py to import just the pure helpers we want to
exercise.

Scope: the v0.16.0 anti-defeat REST surface (countdown_message,
reactivation_message_friendly, reactivation_message_stern, and
notify_parent_target) — both happy-path round-trips through
`_validate_limits_patch` and the per-field validators.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_api_module():
    """Import custom_components.appletv_mgmt.api with HA + aiohttp stubs."""
    # Stub the homeassistant modules api.py touches at import time.
    for mod_name in (
        "homeassistant",
        "homeassistant.core",
        "homeassistant.components",
        "homeassistant.components.http",
        "homeassistant.helpers",
        "homeassistant.helpers.storage",
        "homeassistant.util",
        "homeassistant.util.dt",
        "aiohttp",
        "aiohttp.web",
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
        dt_mod.parse_datetime = lambda s: datetime.fromisoformat(s)

    http_mod = sys.modules["homeassistant.components.http"]
    if not hasattr(http_mod, "HomeAssistantView"):
        http_mod.HomeAssistantView = type("HomeAssistantView", (), {})

    web_mod = sys.modules["aiohttp.web"]
    if not hasattr(web_mod, "Request"):
        web_mod.Request = type("Request", (), {})
        web_mod.Response = type("Response", (), {})
        web_mod.json_response = lambda *a, **kw: None

    # Pre-load const, schedule, quiet, storage, etc. via the file-based
    # loader. They're already stubbed by conftest.py's _load() helper for
    # the modules it covers, so we only need to ensure the rest are there.
    import importlib.util

    pkg_path = ROOT / "custom_components" / "appletv_mgmt"
    # Pre-load const if not loaded yet
    for sub in ("const", "quiet", "schedule", "categorize", "media_attribution",
                "policy", "state"):
        name = f"custom_components.appletv_mgmt.{sub}"
        if name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(name, pkg_path / f"{sub}.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        spec.loader.exec_module(m)

    # Storage requires HA stubs above — load via dedicated path.
    if "custom_components.appletv_mgmt.storage" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "custom_components.appletv_mgmt.storage", pkg_path / "storage.py"
        )
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        spec.loader.exec_module(m)

    # Stub the notify + audit + voice_notifier modules api.py imports.
    # We deliberately include the full "interface surface" that *other*
    # tests (test_init.py particularly) may also reach for, so this stub
    # remains compatible if pytest runs my file first and leaves it in
    # sys.modules — test_init.py's `if full not in sys.modules` guard
    # skips re-stubbing when we win the race.
    audit_symbols = ("record_admin_action", "register_action_recorder")
    notify_symbols = ("handle_external_decision",
                      "send_request_notification",
                      "register_action_handler")
    voice_symbols = ("fire_mode_change_voice", "fire_adult_mode_on_voice",
                     "should_speak", "speak")
    for sub, symbols in (
        ("audit", audit_symbols),
        ("notify", notify_symbols),
        ("voice_notifier", voice_symbols),
    ):
        name = f"custom_components.appletv_mgmt.{sub}"
        if name not in sys.modules:
            stub = types.ModuleType(name)
            for sym in symbols:
                # Default-arg trick so the closure captures the sym at
                # bind time (not at call time).
                setattr(stub, sym, lambda *a, _sym=sym, **kw: (lambda: None)
                        if _sym in ("register_action_recorder",
                                    "register_action_handler")
                        else None)
            sys.modules[name] = stub

    # Now load api.py
    if "custom_components.appletv_mgmt.api" in sys.modules:
        return sys.modules["custom_components.appletv_mgmt.api"]
    spec = importlib.util.spec_from_file_location(
        "custom_components.appletv_mgmt.api", pkg_path / "api.py"
    )
    api = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = api
    spec.loader.exec_module(api)
    return api


api = _load_api_module()


# ---------- v0.16.0 — countdown_message + reactivation messages ----------


@pytest.mark.parametrize(
    "field_name",
    [
        "countdown_message",
        "reactivation_message_friendly",
        "reactivation_message_stern",
    ],
)
def test_v016_message_fields_accept_normal_strings(field_name):
    out = api._validate_field(field_name, "Achtung! Noch 30 Sekunden Bildschirmzeit.")
    assert out == "Achtung! Noch 30 Sekunden Bildschirmzeit."


@pytest.mark.parametrize(
    "field_name",
    [
        "countdown_message",
        "reactivation_message_friendly",
        "reactivation_message_stern",
    ],
)
def test_v016_message_fields_accept_none_as_empty(field_name):
    assert api._validate_field(field_name, None) == ""


@pytest.mark.parametrize(
    "field_name",
    [
        "countdown_message",
        "reactivation_message_friendly",
        "reactivation_message_stern",
    ],
)
def test_v016_message_fields_reject_over_500_chars(field_name):
    too_long = "a" * 501
    with pytest.raises(api._LimitsValidationError):
        api._validate_field(field_name, too_long)


def test_v016_message_fields_round_trip_via_validate_limits_patch():
    body = {
        "countdown_message": "x",
        "reactivation_message_friendly": "y",
        "reactivation_message_stern": "z",
    }
    out = api._validate_limits_patch(body)
    assert out == body  # validator passes through unchanged


# ---------- v0.16.0 — notify_parent_target ----------


def test_notify_parent_target_accepts_normal_service_name():
    out = api._validate_field("notify_parent_target", "mobile_app_your_phone")
    assert out == "mobile_app_your_phone"


def test_notify_parent_target_strips_whitespace():
    out = api._validate_field("notify_parent_target", "  notify_target  ")
    assert out == "notify_target"


def test_notify_parent_target_accepts_empty_string_for_fallback():
    """Empty string means 'fall back to notify.notify' (HA's default)."""
    assert api._validate_field("notify_parent_target", "") == ""
    assert api._validate_field("notify_parent_target", None) == ""


def test_notify_parent_target_rejects_over_100_chars():
    too_long = "x" * 101
    with pytest.raises(api._LimitsValidationError):
        api._validate_field("notify_parent_target", too_long)


# ---------- _validate_limits_patch — unknown-field guard still works ----------


def test_v016_unknown_field_still_rejected():
    """Sanity-check: typo of a v0.16.0 field name surfaces a 422."""
    with pytest.raises(api._LimitsValidationError):
        api._validate_limits_patch({"countdownmessage": "x"})


def test_v016_all_four_fields_allowed_together():
    body = {
        "countdown_message": "30s warning",
        "reactivation_message_friendly": "friendly",
        "reactivation_message_stern": "stern",
        "notify_parent_target": "mobile_app_marc",
    }
    out = api._validate_limits_patch(body)
    assert set(out.keys()) == set(body.keys())


# ============================================================================
# v0.17.0 F-J — OpenAPI schema synced with validator (drift detection)
# ============================================================================


def test_v017_limits_patch_fields_covers_all_validator_allowed_keys():
    """v0.17.0 F-J — every field in the validator's `allowed` set MUST
    have an entry in LIMITS_PATCH_FIELDS (the OpenAPI source of truth),
    and vice versa. Pre-v0.17.0 the hand-rolled OpenAPI schema lagged
    by ~15 fields; clients respecting `additionalProperties: false`
    couldn't PATCH mode / voice templates / monitor-mode flags."""
    # Extract the allowed set by introspecting _validate_limits_patch.
    # The set is hard-coded inline; we mirror the source-of-truth by
    # importing the symbol from the module.
    src = open(api.__file__).read()
    # Pull the `allowed = { ... }` literal as a sanity probe.
    # (Crude but resilient: avoid running the entire validator path.)
    import re
    match = re.search(r"allowed = \{([^}]+)\}", src)
    assert match is not None, "couldn't locate _validate_limits_patch's allowed set"
    # Extract bare string literals between quotes.
    allowed = set(re.findall(r'"([a-z_]+)"', match.group(1)))
    schema_fields = set(api.LIMITS_PATCH_FIELDS.keys())
    missing_from_schema = allowed - schema_fields
    missing_from_validator = schema_fields - allowed
    assert not missing_from_schema, (
        f"OpenAPI schema is missing fields the validator accepts: "
        f"{sorted(missing_from_schema)} — add them to LIMITS_PATCH_FIELDS "
        f"or remove from the validator's allowed set."
    )
    assert not missing_from_validator, (
        f"LIMITS_PATCH_FIELDS has fields the validator does not accept: "
        f"{sorted(missing_from_validator)} — the schema would document "
        f"fields the API will reject with 422."
    )


def test_v017_openapi_limits_patch_integer_constraints_match_validator():
    """v0.17.0 F-J — for each integer-typed field with min/max bounds in
    LIMITS_PATCH_FIELDS, verify the validator rejects values outside
    those bounds. Catches the "added max=900 to validator, forgot
    OpenAPI max=600" drift the adversarial reviewer flagged as the
    failure mode a key-count test would miss.
    """
    integer_field_probes = [
        # (field_name, valid_value_at_min, valid_value_at_max, just_below, just_above)
        ("daily_budget_min", 0, 1440, None, 1441),
        ("grace_seconds", 0, 600, None, 601),
        ("idle_grace_minutes", 0, 60, None, 61),
        ("adult_mode_duration_min", 1, 1440, 0, 1441),
    ]
    for field, lo, hi, below, above in integer_field_probes:
        schema = api.LIMITS_PATCH_FIELDS[field]
        assert schema["type"] == "integer", f"{field}: OpenAPI type mismatch"
        assert schema["minimum"] == lo, (
            f"{field}: OpenAPI minimum={schema['minimum']!r} "
            f"vs validator lower bound={lo!r}"
        )
        assert schema["maximum"] == hi, (
            f"{field}: OpenAPI maximum={schema['maximum']!r} "
            f"vs validator upper bound={hi!r}"
        )
        # Probe the validator: edges accepted, out-of-bounds rejected.
        assert api._validate_field(field, lo) == lo, (
            f"{field}: validator rejected its own documented minimum {lo}"
        )
        assert api._validate_field(field, hi) == hi, (
            f"{field}: validator rejected its own documented maximum {hi}"
        )
        if above is not None:
            with pytest.raises((api._LimitsValidationError, ValueError)):
                api._validate_field(field, above)
        if below is not None:
            with pytest.raises((api._LimitsValidationError, ValueError)):
                api._validate_field(field, below)


def test_v017_decide_endpoint_caps_minutes_at_240():
    """v0.17.0 F-M — the /requests/{id}/decide endpoint now enforces the
    same ±240 cap as POST /extension. Pre-v0.17.0 it accepted unbounded
    int values, effectively disabling daily budgets for the day.
    Behaviour change documented in API.md.

    Unit-level: we exercise the validation branch by feeding a
    deliberately oversized minutes value through the dispatch path.
    Full integration test deferred to the live verification at the
    end of v0.17.0 (aiohttp test harness is heavier than needed here).
    """
    # No direct unit hook — the cap is enforced inline in
    # RequestDecideView.post. We pin the behavior by asserting the
    # constant matches the documented bound, mirroring the test pattern
    # used for the limits validators.
    src = open(api.__file__).read()
    assert "minutes must be -240..240 (matches POST /extension cap)" in src, (
        "F-M cap message not found in api.py — the /decide bound may "
        "have been silently removed."
    )


# ============================================================================
# v0.18.0 — apple_tv_entry_id (proactive pyatv reload target)
# ============================================================================


def test_apple_tv_entry_id_accepts_empty_string():
    """Empty string disables the feature — explicitly allowed."""
    assert api._validate_field("apple_tv_entry_id", "") == ""
    assert api._validate_field("apple_tv_entry_id", None) == ""


def test_apple_tv_entry_id_accepts_typical_32_char_hex():
    """HA ConfigEntry.entry_id is typically a 32-char lowercase hex string."""
    eid = "abcdef0123456789abcdef0123456789"
    assert api._validate_field("apple_tv_entry_id", eid) == eid


def test_apple_tv_entry_id_strips_whitespace():
    out = api._validate_field("apple_tv_entry_id", "  abc123  ")
    assert out == "abc123"


def test_apple_tv_entry_id_rejects_over_64_chars():
    too_long = "x" * 65
    with pytest.raises(api._LimitsValidationError):
        api._validate_field("apple_tv_entry_id", too_long)


def test_apple_tv_entry_id_round_trips_via_validate_limits_patch():
    """End-to-end through the PATCH /limits dispatcher."""
    body = {"apple_tv_entry_id": "abcdef0123456789abcdef0123456789"}
    out = api._validate_limits_patch(body)
    assert out == body


def test_apple_tv_entry_id_in_limits_patch_fields_schema():
    """Drift-protection: ensure the OpenAPI schema knows about the field
    (test_v017_limits_patch_fields_covers_all_validator_allowed_keys
    catches this too, but this asserts the concrete schema shape)."""
    schema = api.LIMITS_PATCH_FIELDS.get("apple_tv_entry_id")
    assert schema is not None, (
        "apple_tv_entry_id must be in LIMITS_PATCH_FIELDS or the OpenAPI "
        "schema will silently lag the validator."
    )
    assert schema["type"] == "string"
    assert schema["maxLength"] == 64


# ---------- v0.20.0 — secondary_devices (the unified-profile merge) ----------


def test_secondary_devices_valid_item_normalizes_and_defaults_bundle():
    """A valid one-item list passes and the bundle_id defaults to xbox.console
    for an xbox_presence device (so attribution lands in the gaming group)."""
    out = api._validate_field("secondary_devices", [
        {
            "entity_id": "device_tracker.xboxone",
            "device_kind": "xbox_presence",
            "enforcement_switch_entity_id": "switch.xboxone_internet_access",
        }
    ])
    assert out == [
        {
            "entity_id": "device_tracker.xboxone",
            "device_kind": "xbox_presence",
            "enforcement_switch_entity_id": "switch.xboxone_internet_access",
            "bundle_id": "xbox.console",
        }
    ]


def test_secondary_devices_none_and_empty_list_become_empty():
    assert api._validate_field("secondary_devices", None) == []
    assert api._validate_field("secondary_devices", []) == []


def test_secondary_devices_missing_entity_id_rejected():
    with pytest.raises(api._LimitsValidationError):
        api._validate_field("secondary_devices", [{"device_kind": "xbox_presence"}])


def test_secondary_devices_bad_device_kind_rejected():
    with pytest.raises(api._LimitsValidationError):
        api._validate_field("secondary_devices", [
            {"entity_id": "device_tracker.x", "device_kind": "nintendo"}
        ])


def test_secondary_devices_non_list_rejected():
    with pytest.raises(api._LimitsValidationError):
        api._validate_field("secondary_devices", {"entity_id": "x"})


def test_secondary_devices_accepted_by_validate_limits_patch_not_unknown():
    """The migration's POST /limits must NOT 422 with 'unknown field' — proves
    the field is in the allowed set AND has a validator branch (B7)."""
    body = {"secondary_devices": [
        {"entity_id": "device_tracker.xboxone", "device_kind": "xbox_presence",
         "enforcement_switch_entity_id": "switch.xboxone_internet_access"}
    ]}
    out = api._validate_limits_patch(body)
    assert out["secondary_devices"][0]["bundle_id"] == "xbox.console"


def test_secondary_devices_null_switch_preserved_as_none():
    out = api._validate_field("secondary_devices", [
        {"entity_id": "device_tracker.x", "device_kind": "xbox_presence",
         "enforcement_switch_entity_id": None}
    ])
    assert out[0]["enforcement_switch_entity_id"] is None


def test_secondary_devices_in_limits_patch_fields_openapi_schema():
    """Schema must be published so the OpenAPI surface doesn't lag the validator."""
    assert "secondary_devices" in api.LIMITS_PATCH_FIELDS
    assert api.LIMITS_PATCH_FIELDS["secondary_devices"]["type"] == "array"


# ---------------------------------------------------------------------------
# v0.21.0 — native TV watching ("Live TV") PATCH surface
# ---------------------------------------------------------------------------


def test_track_native_tv_coerces_to_bool():
    assert api._validate_field("track_native_tv", True) is True
    assert api._validate_field("track_native_tv", 0) is False


def test_native_tv_excluded_sources_normalizes_and_dedupes():
    out = api._validate_field(
        "native_tv_excluded_sources",
        ["HDMI1", " HDMI2/DVI ", "", "HDMI1"],  # blank dropped, trimmed, deduped
    )
    assert out == ["HDMI1", "HDMI2/DVI"]


def test_native_tv_excluded_sources_none_becomes_empty_list():
    assert api._validate_field("native_tv_excluded_sources", None) == []


def test_native_tv_excluded_sources_rejects_non_list():
    with pytest.raises(api._LimitsValidationError):
        api._validate_field("native_tv_excluded_sources", "HDMI1,HDMI2")


def test_native_tv_fields_round_trip_via_validate_limits_patch():
    body = {
        "track_native_tv": True,
        "native_tv_excluded_sources": ["HDMI1", "HDMI2/DVI"],
    }
    out = api._validate_limits_patch(body)
    assert out["track_native_tv"] is True
    assert out["native_tv_excluded_sources"] == ["HDMI1", "HDMI2/DVI"]


def test_native_tv_fields_in_limits_patch_fields_openapi_schema():
    assert api.LIMITS_PATCH_FIELDS["track_native_tv"]["type"] == "boolean"
    assert api.LIMITS_PATCH_FIELDS["native_tv_excluded_sources"]["type"] == "array"


def test_group_budgets_patch_accepts_linear_tv():
    """The linear_tv group is a first-class per-group budget key on PATCH."""
    out = api._validate_field("group_budgets", {"linear_tv": 30, "movies": 60})
    assert out == {"linear_tv": 30, "movies": 60}
    # And round-trips through the whole validator.
    rt = api._validate_limits_patch({"group_budgets": {"linear_tv": 45}})
    assert rt["group_budgets"] == {"linear_tv": 45}


# ---- v0.20.3: /groups rows fold in per-group extensions (live bug 2026-07-05) ----
# Repro: Disney+ (movies) sat at OK because a parent's "+60m movies" lifted the
# effective cap to 90m, but the card showed base 30m maxed-out (0m left, red).


def test_group_rows_fold_movies_extension_into_budget_and_remaining():
    # 80.6 min movies used, base cap 30, +60 tagged to movies -> eff 90, 9.4 left.
    rows = api._effective_group_rows(
        {"movies": 4836, "gaming": 426},
        {"movies": 30, "gaming": 30, "other": 60, "tv_shows": 30},
        {"movies": 60},
    )
    movies = next(r for r in rows if r["group"] == "movies")
    assert movies["budget_today_min"] == 90          # was 30 before the fix
    assert movies["base_budget_today_min"] == 30
    assert movies["extension_today_min"] == 60
    assert movies["remaining_today_min"] == 9.4       # was 0.0 before the fix
    # a group with no extension is unchanged
    gaming = next(r for r in rows if r["group"] == "gaming")
    assert gaming["budget_today_min"] == 30
    assert gaming["extension_today_min"] == 0
    assert gaming["remaining_today_min"] == 22.9


def test_group_rows_no_extension_is_backcompat():
    rows = api._effective_group_rows({"movies": 4836}, {"movies": 30}, {})
    movies = rows[0]
    assert movies["budget_today_min"] == 30
    assert movies["extension_today_min"] == 0
    assert movies["remaining_today_min"] == 0.0


def test_group_rows_zero_budget_stays_unlimited_despite_extension():
    # 0 = unlimited by convention; an extension must NOT convert it to a cap.
    rows = api._effective_group_rows({"other": 600}, {"other": 0}, {"other": 60})
    other = rows[0]
    assert other["budget_today_min"] == 0            # NOT 60
    assert other["base_budget_today_min"] == 0
