"""Per-weekday schedule resolution.

Added in v0.11.0 to support "weekend is different" + "active hours" +
"per-category-per-day" parental controls. Designed so the existing
EnforcementController doesn't need to know about weekdays — the
coordinator resolves the effective values once per tick and passes them
in.

Pure module — no HA / no aiohttp imports — so it's trivially testable.

Design philosophy:
- The base `Profile.daily_budget_min` / `group_budgets` / `quiet_windows`
  stay as the DEFAULT. Optional per-weekday dicts (`weekday_budgets_min`,
  `weekday_group_budgets_min`, `weekday_quiet_windows`) OVERRIDE the
  default for individual days.
- Empty override dicts = backward-compatible behavior identical to v0.10.
- Validation here is intentionally lenient (clamping rather than raising)
  so a bad value in storage doesn't crash the integration. The PATCH API
  endpoint does strict validation before persisting.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

# Stable order; index 0 = Monday matches Python's datetime.weekday().
WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_SET = frozenset(WEEKDAYS)
WEEKEND: frozenset[str] = frozenset({"sat", "sun"})
WEEKDAYS_ONLY: frozenset[str] = frozenset({"mon", "tue", "wed", "thu", "fri"})


def weekday_key(d: date | datetime) -> str:
    """Return the canonical weekday key ('mon'..'sun') for a date.

    Uses Python's `weekday()` (Monday = 0). Works on naïve or aware
    datetimes — caller is responsible for converting to local time
    before passing here (so a Friday-23:30-Europe/Berlin event is
    classified as Friday, not Saturday-UTC).
    """
    if isinstance(d, datetime):
        d = d.date()
    return WEEKDAYS[d.weekday()]


# ---------- effective-value resolvers ---------------------------------------


def effective_daily_budget(
    *,
    base_min: int,
    weekday_overrides: Mapping[str, int],
    on: date | datetime,
) -> int:
    """Resolve the daily budget (in minutes) for a given date."""
    wd = weekday_key(on)
    raw = weekday_overrides.get(wd) if weekday_overrides else None
    if raw is None:
        return int(base_min)
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return int(base_min)
    return max(0, v)


def effective_group_budgets(
    *,
    base_groups: Mapping[str, int],
    weekday_group_overrides: Mapping[str, Mapping[str, int]],
    on: date | datetime,
) -> dict[str, int]:
    """Resolve the per-group budget map for a given date.

    Override semantics: per-group overrides are MERGED on top of the
    base map. A group present in `base_groups` with no override keeps
    the base value. A group present only in the override is added.
    """
    wd = weekday_key(on)
    overrides = (weekday_group_overrides or {}).get(wd, {})
    out: dict[str, int] = {}
    for g, v in (base_groups or {}).items():
        try:
            out[g] = max(0, int(v))
        except (TypeError, ValueError):
            continue
    for g, v in (overrides or {}).items():
        try:
            out[g] = max(0, int(v))
        except (TypeError, ValueError):
            continue
    return out


def effective_quiet_windows_string(
    *,
    base_string: str,
    weekday_overrides: Mapping[str, str],
    on: date | datetime,
) -> str:
    """Resolve the quiet-windows string for a given date.

    Override semantics: a per-day override REPLACES the base entirely
    for that day. To remove all quiet windows on a specific day,
    override with an empty string.
    """
    wd = weekday_key(on)
    if weekday_overrides and wd in weekday_overrides:
        return str(weekday_overrides[wd] or "")
    return str(base_string or "")


# ---------- validation helpers ----------------------------------------------


def normalize_weekday_dict(
    raw: Any,
    *,
    value_decoder=lambda v: int(v),
) -> dict[str, Any]:
    """Validate + normalize a per-weekday dict.

    - Keys are lower-cased + checked against WEEKDAY_SET (unknown keys raise).
    - Values are passed through `value_decoder` (raises ValueError on bad).
    - Returns a fresh dict, sorted in WEEKDAYS order for deterministic storage.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"expected a mapping of weekday -> value, got {type(raw)!r}")
    out: dict[str, Any] = {}
    for k, v in raw.items():
        key = str(k).strip().lower()
        if key not in WEEKDAY_SET:
            raise ValueError(
                f"unknown weekday {k!r} (allowed: {sorted(WEEKDAYS)})"
            )
        out[key] = value_decoder(v)
    # Re-sort by weekday order for stable on-disk shape.
    return {wd: out[wd] for wd in WEEKDAYS if wd in out}


def normalize_weekday_group_dict(raw: Any) -> dict[str, dict[str, int]]:
    """Validate a nested per-weekday per-group budget dict."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("expected mapping of weekday -> {group: minutes}")

    def _decode_inner(v: Any) -> dict[str, int]:
        if not isinstance(v, Mapping):
            raise ValueError(f"per-day group budgets must be a mapping, got {type(v)!r}")
        inner: dict[str, int] = {}
        for g, m in v.items():
            try:
                minutes = int(m)
            except (TypeError, ValueError) as err:
                raise ValueError(f"group {g!r} minutes must be int") from err
            if minutes < 0 or minutes > 24 * 60:
                raise ValueError(
                    f"group {g!r} minutes out of range [0..1440]: {minutes}"
                )
            inner[str(g)] = minutes
        return inner

    return normalize_weekday_dict(raw, value_decoder=_decode_inner)
