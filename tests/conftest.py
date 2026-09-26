"""Test setup.

The integration's `__init__.py` imports Home Assistant (voluptuous, etc.).
For Phase 1 unit tests we only exercise pure modules (`state`, `adguard`,
plus `const`), so we load those by file path and register them under
synthetic module names — without ever executing the package `__init__.py`.

This keeps the dev install tiny (no HA in requirements_dev.txt) and the
tests fast.
"""
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "appletv_mgmt"


def _load(module_name: str, file_path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# Create stub parent packages so `from custom_components.appletv_mgmt.X import ...`
# resolves without executing the real package __init__.py modules.
for pkg_name in ("custom_components", "custom_components.appletv_mgmt"):
    if pkg_name not in sys.modules:
        stub = types.ModuleType(pkg_name)
        stub.__path__ = []  # mark as namespace package
        sys.modules[pkg_name] = stub

# Pre-load the modules the tests need, in dependency order.
_load("custom_components.appletv_mgmt.const", PKG / "const.py")
_load("custom_components.appletv_mgmt.state", PKG / "state.py")
_load("custom_components.appletv_mgmt.adguard", PKG / "adguard.py")
_load("custom_components.appletv_mgmt.quiet", PKG / "quiet.py")
_load("custom_components.appletv_mgmt.categorize", PKG / "categorize.py")
_load("custom_components.appletv_mgmt.schedule", PKG / "schedule.py")
_load("custom_components.appletv_mgmt.media_attribution", PKG / "media_attribution.py")
# voice_notifier imports homeassistant.* (only for type hints + service calls);
# loaded lazily by tests that need it via the same HA-stub pattern as storage_helpers.
