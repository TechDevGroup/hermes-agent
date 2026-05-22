"""Tests for the devagentic-local provider's hermes-profile binding.

Phase G (devagentic issue #50). Verifies that:

  * `_resolve_user_id()` returns the active hermes profile name when
    no override is set.
  * `DEVAGENTIC_USER_ID` env beats the profile-derived value.
  * Resolution falls back to None when `hermes_cli.profiles` isn't
    importable (e.g. test contexts that bypass the CLI).
  * `DevagenticLocalProfile.build_api_kwargs_extras` injects
    `extra_headers={"X-User-Id": <resolved>}` into the top-level
    kwargs half of the tuple.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


# The plugin module sits in a hyphenated directory. Resolve it by path
# so the import is robust against import-system quirks around hyphens.
@pytest.fixture
def plugin():
    repo_root = Path(__file__).resolve().parents[1]
    plugin_path = (repo_root / "plugins" / "model-providers"
                   / "devagentic-local" / "__init__.py")
    spec = importlib.util.spec_from_file_location(
        "devagentic_local_profile_under_test", plugin_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_resolve_user_id_env_override_wins(plugin, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "carol")
    assert plugin._resolve_user_id() == "carol"


def test_resolve_user_id_env_override_strips_whitespace(plugin, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "  carol  ")
    assert plugin._resolve_user_id() == "carol"


def test_resolve_user_id_falls_back_to_active_profile(plugin, monkeypatch):
    monkeypatch.delenv("DEVAGENTIC_USER_ID", raising=False)
    # Stub hermes_cli.profiles.get_active_profile_name without polluting
    # the real module's namespace.
    fake_mod = type(sys)("hermes_cli.profiles")
    fake_mod.get_active_profile_name = lambda: "alice"
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", fake_mod)
    assert plugin._resolve_user_id() == "alice"


def test_resolve_user_id_handles_default_profile(plugin, monkeypatch):
    """`get_active_profile_name()` returns `"default"` for the bare
    ~/.hermes root. v0 propagates that verbatim — it's a valid user_id
    on the devagentic side."""
    monkeypatch.delenv("DEVAGENTIC_USER_ID", raising=False)
    fake_mod = type(sys)("hermes_cli.profiles")
    fake_mod.get_active_profile_name = lambda: "default"
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", fake_mod)
    assert plugin._resolve_user_id() == "default"


def test_resolve_user_id_returns_none_on_blank(plugin, monkeypatch):
    monkeypatch.delenv("DEVAGENTIC_USER_ID", raising=False)
    fake_mod = type(sys)("hermes_cli.profiles")
    fake_mod.get_active_profile_name = lambda: ""
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", fake_mod)
    assert plugin._resolve_user_id() is None


def test_resolve_user_id_handles_import_failure(plugin, monkeypatch):
    monkeypatch.delenv("DEVAGENTIC_USER_ID", raising=False)
    # Force the import to fail by injecting a sentinel that raises on
    # attribute access.
    class _Boom:
        def __getattr__(self, name):
            raise RuntimeError("simulated profile resolution failure")
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", _Boom())
    assert plugin._resolve_user_id() is None


def test_build_api_kwargs_extras_injects_x_user_id(plugin, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    profile = plugin.DevagenticLocalProfile(
        name="test-devagentic-local",
        env_vars=("DEVAGENTIC_API_KEY",),
        display_name="Devagentic (test)",
        base_url="http://127.0.0.1:6071/v1",
    )
    extra_body, top_level = profile.build_api_kwargs_extras()
    assert extra_body == {}, "no body-level additions"
    assert top_level == {"extra_headers": {"X-User-Id": "alice"}}


def test_build_api_kwargs_extras_omits_header_when_unresolved(
        plugin, monkeypatch):
    monkeypatch.delenv("DEVAGENTIC_USER_ID", raising=False)
    fake_mod = type(sys)("hermes_cli.profiles")
    fake_mod.get_active_profile_name = lambda: ""
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", fake_mod)
    profile = plugin.DevagenticLocalProfile(
        name="test-devagentic-local",
        env_vars=("DEVAGENTIC_API_KEY",),
        display_name="Devagentic (test)",
        base_url="http://127.0.0.1:6071/v1",
    )
    extra_body, top_level = profile.build_api_kwargs_extras()
    assert extra_body == {}
    assert top_level == {}, "no X-User-Id when unresolved"


def test_build_api_kwargs_extras_propagates_env_change_per_request(
        plugin, monkeypatch):
    """Hermes processes that switch profiles mid-session should pick
    up the change on the next completion — verified by re-resolving
    on each call rather than caching at construction time."""
    profile = plugin.DevagenticLocalProfile(
        name="test-devagentic-local",
        env_vars=("DEVAGENTIC_API_KEY",),
        display_name="Devagentic (test)",
        base_url="http://127.0.0.1:6071/v1",
    )
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    _, top1 = profile.build_api_kwargs_extras()
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "bob")
    _, top2 = profile.build_api_kwargs_extras()
    assert top1["extra_headers"]["X-User-Id"] == "alice"
    assert top2["extra_headers"]["X-User-Id"] == "bob"
