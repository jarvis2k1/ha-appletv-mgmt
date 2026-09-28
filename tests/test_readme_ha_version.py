"""v0.23.3 — the README must advertise the same minimum Home Assistant version
that `hacs.json` enforces.

Flagged by the HACS reviewer on approval (hacs/default#9134, 2026-09-02): the
README said "Home Assistant 2025.1+" in two places while `hacs.json` required
`2026.3.0`. A user on 2025.x follows the docs, tries to install, and HACS
refuses with a message that contradicts them. The two drift independently, so
this pins them together.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HA_VERSION = re.compile(r"(20\d\d)\.(\d+)")


def _hacs_minimum() -> tuple[str, str]:
    raw = json.loads((ROOT / "hacs.json").read_text())["homeassistant"]
    m = HA_VERSION.match(raw)
    assert m, f"unparseable hacs.json homeassistant: {raw!r}"
    return m.group(1), m.group(2)


def _readme_ha_claims() -> list[tuple[int, str, str]]:
    """Every YYYY.N on a README line that is about Home Assistant itself."""
    claims = []
    for n, line in enumerate((ROOT / "README.md").read_text().splitlines(), 1):
        if "Home Assistant" not in line and "Home%20Assistant" not in line:
            continue
        for m in HA_VERSION.finditer(line):
            claims.append((n, m.group(1), m.group(2)))
    return claims


def test_readme_advertises_a_minimum_at_all():
    assert _readme_ha_claims(), "README no longer states a minimum HA version"


def test_readme_minimum_matches_hacs_json():
    want = _hacs_minimum()
    wrong = [f"line {n}: {y}.{m}" for n, y, m in _readme_ha_claims() if (y, m) != want]
    assert not wrong, (
        f"README advertises a different minimum than hacs.json ({'.'.join(want)}): "
        + ", ".join(wrong)
    )
