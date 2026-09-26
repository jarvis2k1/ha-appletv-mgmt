"""v0.22.0 — the REST API must require Home Assistant's own authentication.

WHY THESE ARE SOURCE-LEVEL TESTS
--------------------------------
The property under test is "no view opts out of authentication". That is a
property of the *source*, and the failure mode we are guarding against is a
future edit re-adding `requires_auth = False` to a class — which no
behavioural test would catch unless it happened to exercise that exact view.
Parsing the module with `ast` asserts it for EVERY view at once, including
views that do not exist yet.

Background (HACS review, hacs/default#9134, 2026-08-28): `_Base` set
`requires_auth = False`, so every endpoint was anonymous. The custom
`api_key` gate meant to compensate opened with `if not key: return None`,
making it a no-op on a fresh install — and the key was only offered in the
options flow, never at initial setup. The endpoints that lift a child's
limits were therefore reachable unauthenticated by anything on the LAN, and
beyond it wherever the instance is exposed.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

API_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components" / "appletv_mgmt" / "api.py"
)
TREE = ast.parse(API_PATH.read_text())

# The endpoints an attacker would actually want — each one either lifts a
# limit or reveals the shape of the API.
SENSITIVE_VIEWS = {
    "ProfileLimitsView",      # PATCH /limits — rewrite the daily budget
    "AdultModeView",          # POST/DELETE /adult_mode — suspend enforcement
    "ExtensionView",          # POST /extension — grant screen time
    "RequestDecideView",      # POST /requests/{id}/decide — self-approve
    "HealthView",             # leaks version + profile count
    "OpenAPIView",            # leaks the full endpoint schema
}


def _classes() -> dict[str, ast.ClassDef]:
    return {
        n.name: n for n in ast.walk(TREE) if isinstance(n, ast.ClassDef)
    }


def _assigns_requires_auth_false(node: ast.ClassDef) -> bool:
    for stmt in node.body:
        targets = (
            stmt.targets if isinstance(stmt, ast.Assign)
            else [stmt.target] if isinstance(stmt, ast.AnnAssign)
            else []
        )
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "requires_auth":
                val = getattr(stmt, "value", None)
                if isinstance(val, ast.Constant) and val.value is False:
                    return True
    return False


def test_no_view_disables_authentication():
    """The regression that caused the HACS rejection: any class turning
    `requires_auth` off re-opens every endpoint beneath it."""
    offenders = [
        name for name, node in _classes().items()
        if _assigns_requires_auth_false(node)
    ]
    assert offenders == [], (
        f"{offenders} set requires_auth = False, making their endpoints "
        "anonymous. HomeAssistantView defaults to requires_auth = True; do "
        "not override it."
    )


def test_requires_auth_false_absent_from_entire_module():
    """Belt-and-braces: catches the assignment wherever it hides — a
    module-level constant, a mixin, a conditional block."""
    # Anchored at line start so prose *discussing* the old bug (in this
    # module's docstrings) doesn't trip it — only a real assignment does.
    pattern = re.compile(r"^\s*requires_auth\s*(?::\s*\w+\s*)?=\s*False\b")
    offending = [
        ln.strip() for ln in API_PATH.read_text().splitlines()
        if pattern.match(ln)
    ]
    assert offending == [], f"requires_auth disabled somewhere: {offending}"


@pytest.mark.parametrize("view", sorted(SENSITIVE_VIEWS))
def test_sensitive_view_exists_and_inherits_shared_base(view: str):
    """Each sensitive view must still route through `_Base`, which is where
    authentication is inherited from. A view that stops inheriting it would
    silently fall back to HomeAssistantView's own default."""
    classes = _classes()
    assert view in classes, f"{view} disappeared — update this test deliberately"
    bases = {b.id for b in classes[view].bases if isinstance(b, ast.Name)}
    assert "_Base" in bases, f"{view} no longer inherits _Base (bases={bases})"


def test_custom_api_key_gate_is_gone():
    """The home-grown key mechanism is removed, not merely bypassed.

    Leaving it in place would reintroduce the confusion frenck flagged: a
    gate that looks like authentication but returns None when unconfigured.
    """
    src = API_PATH.read_text()
    for symbol in ("_check_api_key", "_resolved_api_key", "CONF_API_KEY"):
        assert symbol not in src, (
            f"{symbol} still present — HA's own bearer check is the only "
            "authentication path now."
        )


def test_health_reports_auth_required_true():
    """`/health` advertises the posture. It must not claim to be open."""
    src = API_PATH.read_text()
    assert '"auth_required": True' in src, (
        "health must report auth_required: True; it previously derived this "
        "from whether a custom key happened to be set."
    )


def test_health_and_openapi_are_not_special_cased():
    """These two leaked version, profile count and the endpoint schema to
    anonymous callers even when a key WAS configured, because they skipped
    the gate unconditionally."""
    classes = _classes()
    for name in ("HealthView", "OpenAPIView"):
        node = classes[name]
        assert not _assigns_requires_auth_false(node)
        # No leftover "# public" marker implying an intentional bypass.
        seg = ast.get_source_segment(API_PATH.read_text(), node) or ""
        assert "# public" not in seg, (
            f"{name} still marked '# public' — it is authenticated now."
        )


def test_adult_mode_accepts_post_minutes_zero_as_disable():
    """v0.22.0 — Supervisor's core proxy forwards only GET and POST, so an
    add-on can never reach the DELETE handler. Without a POST disable path,
    a parent can grant an adult-mode override from the panel but not revoke
    it early. `{"minutes": 0}` must route to the same delete logic."""
    src = API_PATH.read_text()
    cls = _classes()["AdultModeView"]
    seg = ast.get_source_segment(src, cls) or ""
    assert "self.delete(request, profile_id)" in seg, (
        "POST must delegate to the delete handler for minutes:0"
    )
    # DELETE stays available for direct (non-proxied) callers.
    methods = {n.name for n in cls.body if isinstance(n, ast.AsyncFunctionDef)}
    assert {"post", "delete"} <= methods, methods
