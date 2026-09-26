"""v0.23.2 — PATCH-able limits fields must survive an HA restart.

THE BUG: `async_setup_entry` rebuilds the Profile from `entry.data`/`entry.options`
on every setup, then overlays a hand-maintained list of fields from the store.
v0.23.0 added `day_rollover_hour` to the PATCH-able set in `api.py` but not to
that overlay list, so every HA restart silently reset it to the dataclass
default of 0.

Live-observed: set to 5 on 2026-09-02, found back at 0 on 2026-09-12 with **no
audit-log entry**, because nothing wrote it — a restart simply rebuilt the
Profile. The v0.15.9 comment in `__init__.py` documents the identical failure
for `daily_budget_min`; the list is the kind that gets forgotten, so this test
pins it.
"""
from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "appletv_mgmt"


def _patchable_fields() -> set[str]:
    """The `allowed = {...}` set guarding PATCH /limits in api.py."""
    tree = ast.parse((PKG / "api.py").read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "allowed" for t in node.targets)
            and isinstance(node.value, ast.Set)
        ):
            return {
                e.value for e in node.value.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            }
    raise AssertionError("could not locate the `allowed` set in api.py")


def _restart_survival_fields() -> set[str]:
    """Every string in a `for attr in (...)` overlay loop in __init__.py."""
    tree = ast.parse((PKG / "__init__.py").read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.For)
            and getattr(node.target, "id", None) == "attr"
            and isinstance(node.iter, ast.Tuple)
        ):
            out |= {
                e.value for e in node.iter.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            }
    assert out, "could not locate the restart-survival loops in __init__.py"
    return out


def test_day_rollover_hour_survives_restart():
    """THE regression. Without this, a 05:00 household silently returns to
    midnight accounting at the next restart."""
    assert "day_rollover_hour" in _restart_survival_fields()


# Fields that are PATCH-able but deliberately NOT in the overlay list, each
# because something else already makes them restart-safe or they must not be
# store-primary. Documented so the canary below stays meaningful.
KNOWN_NOT_OVERLAID = {
    # Resolved explicitly earlier in async_setup_entry (store-primary already).
    "mode", "enforcement_enabled", "tv_shutdown_target",
    # Config-flow owned; changing these belongs to the entry, not the store.
    "tv_entity_id", "tv_shutdown_enabled", "track_native_tv",
    # Weekday overrides + assorted scalars: not yet overlaid. See v0.23.2 notes
    # — these are the NEXT candidates if a parent reports one reverting.
    "weekday_budgets_min", "weekday_group_budgets_min", "weekday_quiet_windows",
    "stale_session_minutes", "warn_in_monitor_mode", "voice_on_mode_change",
    "notify_volume", "notify_tts_language",
    # Voice/message strings are overlaid by the separate non-empty-string loop,
    # which this extractor already folds in; any left here are intentional.
}


def test_no_new_patchable_field_forgets_the_overlay_list():
    """Canary. A newly PATCH-able field that forgets the overlay list shows up
    here, instead of as a parent's setting silently reverting weeks later."""
    gap = _patchable_fields() - _restart_survival_fields() - KNOWN_NOT_OVERLAID
    assert not gap, (
        "PATCH-able but will NOT survive an HA restart: "
        + ", ".join(sorted(gap))
        + " — add to the overlay list in __init__.py, or to KNOWN_NOT_OVERLAID "
          "with a reason."
    )
