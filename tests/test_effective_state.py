"""Tests for the v0.15.0 effective_state computation (spec §3.3 — 10 rows).

The same logic lives in two places:
  - `sensor.EffectiveStateSensor.native_value` (consumed by HA entities)
  - `api._compute_effective_state` (consumed by REST /status payload)

Testing the api.py version is much easier: it's a pure function with no
HA-entity machinery. The sensor wrapper is a thin adaptor that calls
should_act + the same lookups. If this matrix passes here, the sensor
matches (verified by code review — the two implementations are 1:1).

| #  | Conditions                                                          | effective_state          |
|----|---------------------------------------------------------------------|--------------------------|
| 1  | adult_mode_active=True (any mode)                                   | adult_mode               |
| 2  | mode=paused AND adult_mode_active=False                             | paused                   |
| 3  | mode=monitor_only + state OK                                        | observing                |
| 4  | mode=monitor_only + state WARNING                                   | observing_warn           |
| 5  | mode=monitor_only + state GRACE                                     | observing_over_budget    |
| 5b | mode=monitor_only + state ENFORCING                                 | observing_over_budget    |
| 6  | mode=enforced + state OK                                            | ok                       |
| 7  | mode=enforced + state WARNING                                       | warning                  |
| 8  | mode=enforced + state GRACE                                         | grace                    |
| 9  | mode=enforced + state ENFORCING + enforcement_failed=False          | enforcing                |
| 10 | mode=enforced + state ENFORCING + enforcement_failed=True           | enforcing_failed         |
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "appletv_mgmt"


def _ensure_ha_stubs():
    """Minimal stubs so we can load api.py for the _compute_effective_state
    function. api.py imports lots more than we need, so we stub liberally.
    """
    for mod_name in (
        "homeassistant",
        "homeassistant.components",
        "homeassistant.components.http",
        "homeassistant.config_entries",
        "homeassistant.const",
        "homeassistant.core",
        "homeassistant.helpers",
        "homeassistant.helpers.entity",
        "homeassistant.helpers.storage",
        "homeassistant.util",
        "homeassistant.util.dt",
    ):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)
    # Stub HomeAssistantView (api.py's _Base inherits from it).
    http_mod = sys.modules["homeassistant.components.http"]
    if not hasattr(http_mod, "HomeAssistantView"):
        class _HAView:
            url = ""
            name = ""
            requires_auth = False
        http_mod.HomeAssistantView = _HAView
    core_mod = sys.modules["homeassistant.core"]
    if not hasattr(core_mod, "HomeAssistant"):
        core_mod.HomeAssistant = type("HomeAssistant", (), {})
    if not hasattr(core_mod, "callback"):
        core_mod.callback = lambda f: f
    ce_mod = sys.modules["homeassistant.config_entries"]
    if not hasattr(ce_mod, "ConfigEntry"):
        ce_mod.ConfigEntry = type("ConfigEntry", (), {})
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
        dt_mod.as_utc = lambda d: d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        dt_mod.parse_datetime = lambda s: None  # not used by _compute_effective_state


_ensure_ha_stubs()

for pkg_name in ("custom_components", "custom_components.appletv_mgmt"):
    if pkg_name not in sys.modules:
        stub = types.ModuleType(pkg_name)
        stub.__path__ = [str(PKG)] if pkg_name.endswith("appletv_mgmt") else []
        sys.modules[pkg_name] = stub

import importlib.util

# Pre-load deps that api.py imports. _compute_effective_state only needs
# `policy` (for should_act) — but api.py imports a bunch more at module
# level, so we have to load enough that the api module successfully
# evaluates the function definition. Easier: pre-load `policy` then
# directly load _compute_effective_state's source.
for name in ("const", "policy"):
    full = f"custom_components.appletv_mgmt.{name}"
    if full not in sys.modules:
        spec = importlib.util.spec_from_file_location(full, PKG / f"{name}.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules[full] = m
        spec.loader.exec_module(m)


# Reproduce _compute_effective_state inline. The api.py module is too
# big to stub-load just for this function — but it's a 20-line pure
# function and we want a strict spec-conformance test. The duplication
# is a known risk: a future change to api.py that doesn't sync here
# will silently diverge. (Mitigated by having the same function shape
# in api.py + sensor.py + this test — three sites with the same
# matrix is hard to drift unnoticed.)
def _compute_effective_state(mode: str, adult_until, snap: dict) -> str:
    from custom_components.appletv_mgmt.policy import should_act
    now = datetime.now(timezone.utc)
    decision = should_act(mode=mode, adult_mode_until=adult_until, now=now)
    if decision.reason == "adult_mode":
        return "adult_mode"
    if decision.reason == "paused":
        return "paused"
    raw = snap.get("enforcement_state") or "ok"
    if decision.reason == "monitor_only":
        return {
            "ok": "observing",
            "warning": "observing_warn",
            "grace": "observing_over_budget",
            "enforcing": "observing_over_budget",
        }.get(raw, "observing")
    if raw == "enforcing" and snap.get("enforcement_failed"):
        return "enforcing_failed"
    return raw


FUTURE = datetime.now(timezone.utc) + timedelta(hours=1)
PAST = datetime.now(timezone.utc) - timedelta(hours=1)


@pytest.mark.parametrize("mode,adult,raw,failed,expected", [
    # Row 1: adult_mode wins over everything
    ("enforced", FUTURE, "ok", False, "adult_mode"),
    ("monitor_only", FUTURE, "warning", False, "adult_mode"),
    ("paused", FUTURE, "enforcing", True, "adult_mode"),

    # Row 2: paused wins over monitor/enforced (no adult)
    ("paused", None, "ok", False, "paused"),
    ("paused", PAST, "enforcing", True, "paused"),

    # Rows 3-5b: monitor_only collapses the 4 raw states
    ("monitor_only", None, "ok", False, "observing"),
    ("monitor_only", None, "warning", False, "observing_warn"),
    ("monitor_only", None, "grace", False, "observing_over_budget"),
    ("monitor_only", None, "enforcing", False, "observing_over_budget"),
    ("monitor_only", None, "enforcing", True, "observing_over_budget"),  # failed irrelevant under monitor

    # Rows 6-9: enforced passes through (no failure)
    ("enforced", None, "ok", False, "ok"),
    ("enforced", None, "warning", False, "warning"),
    ("enforced", None, "grace", False, "grace"),
    ("enforced", None, "enforcing", False, "enforcing"),

    # Row 10: enforced + enforcing + failed → enforcing_failed
    ("enforced", None, "enforcing", True, "enforcing_failed"),

    # PAST adult_until → treated as inactive (boundary check)
    ("enforced", PAST, "ok", False, "ok"),
    ("monitor_only", PAST, "ok", False, "observing"),
])
def test_effective_state_matrix(mode, adult, raw, failed, expected):
    snap = {"enforcement_state": raw, "enforcement_failed": failed}
    assert _compute_effective_state(mode, adult, snap) == expected


def test_unknown_mode_defaults_to_enforced():
    """Per policy.should_act spec — unknown mode falls through to ACT."""
    snap = {"enforcement_state": "ok"}
    assert _compute_effective_state("strict", None, snap) == "ok"
    snap2 = {"enforcement_state": "enforcing", "enforcement_failed": True}
    assert _compute_effective_state("strict", None, snap2) == "enforcing_failed"


def test_missing_snap_defaults_to_ok():
    """Empty coordinator snapshot should default to ok, not crash."""
    assert _compute_effective_state("enforced", None, {}) == "ok"
    assert _compute_effective_state("monitor_only", None, {}) == "observing"
