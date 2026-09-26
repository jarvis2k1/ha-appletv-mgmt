"""Quiet windows — time-of-day shutdown ranges.

Pure module (no Home Assistant imports) so the parsing and the
"is the current local time inside any of these windows?" check are
fully unit-testable in isolation.

A `QuietWindow` is a `(start, end)` pair of `datetime.time` objects plus
an optional label. The window can cross midnight (e.g. 20:30 → 07:00):
when `start > end`, "inside" means `[start, 24:00) ∪ [00:00, end)`.

Windows serialize as a comma-separated string for storage / config-form
round-tripping. Format per window:

    HH:MM-HH:MM            e.g.  "20:30-07:00"
    HH:MM-HH:MM:Label      e.g.  "20:30-07:00:Bedtime"

The whole config-flow value is the comma-joined list:

    "12:00-14:00:Lunch, 20:30-07:00:Bedtime"
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Iterable


@dataclass(frozen=True)
class QuietWindow:
    start: time
    end: time
    label: str = ""

    def contains(self, t: time) -> bool:
        """Does this local-time fall inside the window?

        - `start == end` is treated as an empty window (never contains).
        - `start < end` is a same-day window: `[start, end)`.
        - `start > end` is a crosses-midnight window: `[start, 24:00) ∪ [00:00, end)`.
        """
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end

    def format(self) -> str:
        s = f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')}"
        return f"{s}:{self.label}" if self.label else s

    @classmethod
    def parse(cls, raw: str) -> "QuietWindow":
        """Parse one window. Raises ValueError on bad input."""
        s = raw.strip()
        if not s:
            raise ValueError("empty quiet window")
        if "-" not in s:
            raise ValueError(f"missing '-' in window: {raw!r}")
        # Split at the FIRST dash so "20:30-07:00:Bedtime" works.
        start_raw, _, rest = s.partition("-")
        start_raw = start_raw.strip()
        rest = rest.strip()
        # `rest` is "HH:MM" or "HH:MM:Label". The label may itself contain
        # colons; we split at the first colon that's after the HH:MM.
        # HH:MM has exactly one colon → if there are more, everything after
        # the second colon is the label.
        first_colon = rest.find(":")
        if first_colon == -1:
            raise ValueError(f"missing ':' in window end time: {raw!r}")
        second_colon = rest.find(":", first_colon + 1)
        if second_colon == -1:
            end_raw, label = rest, ""
        else:
            end_raw = rest[:second_colon]
            label = rest[second_colon + 1 :].strip()
        try:
            start = time.fromisoformat(start_raw.strip())
            end = time.fromisoformat(end_raw.strip())
        except ValueError as err:
            raise ValueError(f"bad time in window {raw!r}: {err}") from err
        return cls(start=start, end=end, label=label)


def parse_windows(raw: str | None) -> list[QuietWindow]:
    """Parse a comma-separated list of windows. Empty or None → []."""
    if not raw or not raw.strip():
        return []
    return [QuietWindow.parse(part) for part in raw.split(",") if part.strip()]


def windows_to_string(windows: Iterable[QuietWindow]) -> str:
    """Inverse of parse_windows — round-trips through the config form."""
    return ", ".join(w.format() for w in windows)


def find_active_window(
    windows: Iterable[QuietWindow], now_local: datetime
) -> QuietWindow | None:
    """Return the first window that contains the local time `now_local`, or None.

    Caller is responsible for converting UTC → local (e.g. via
    `homeassistant.util.dt.as_local`).
    """
    t = now_local.time()
    for w in windows:
        if w.contains(t):
            return w
    return None


def validate_windows_string(raw: str) -> None:
    """Raise ValueError if `raw` doesn't parse cleanly. Used by config_flow."""
    parse_windows(raw)
