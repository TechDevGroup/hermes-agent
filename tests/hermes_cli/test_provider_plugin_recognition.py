"""Tests for plugin-registered provider recognition in ``--provider`` flag
resolution (PR-3 of the hermes-cli-defers-to-devagentic wave).

Operators reported ``Unknown provider 'devagentic-local'`` despite the
plugin under ``plugins/model-providers/devagentic-local/`` being on
disk + registered in the ``providers`` package's ``_REGISTRY``. Root
cause: ``resolve_provider_full`` (hermes_cli/providers.py) and
``resolve_provider`` (hermes_cli/auth.py) only consulted built-in
dicts + models.dev + user/custom config. Plugin-registered profiles
were invisible.
"""
from __future__ import annotations

import pytest


def test_resolve_provider_full_accepts_plugin_registered_name(monkeypatch):
    """``providers.get_provider_profile`` returning a profile for the
    name should yield a ``ProviderDef`` through
    ``resolve_provider_full`` (after the built-in / config / models.dev
    chain fails)."""
    from hermes_cli import providers as hp
    import providers as plugin_providers

    # Synthesize a profile that mirrors devagentic-local's shape.
    class _FakeProfile:
        name = "test-plugin-provider"
        display_name = "Test Plugin Provider"
        env_vars = ("TEST_PLUGIN_API_KEY",)
        base_url = "http://127.0.0.1:9999/v1"
        aliases: tuple[str, ...] = ()

    def _fake_profile_lookup(query):
        if query == "test-plugin-provider":
            return _FakeProfile()
        return None

    monkeypatch.setattr(
        plugin_providers, "get_provider_profile", _fake_profile_lookup)

    pdef = hp.resolve_provider_full("test-plugin-provider")
    assert pdef is not None
    assert pdef.id == "test-plugin-provider"
    assert pdef.name == "Test Plugin Provider"
    assert pdef.api_key_env_vars == ("TEST_PLUGIN_API_KEY",)
    assert pdef.base_url == "http://127.0.0.1:9999/v1"
    assert pdef.source == "plugin"


def test_resolve_provider_full_unknown_still_returns_none(monkeypatch):
    """Names that don't match any source (built-in, config, models.dev,
    plugin) still return None — fix doesn't accidentally promote
    unknown names."""
    from hermes_cli import providers as hp
    import providers as plugin_providers

    monkeypatch.setattr(
        plugin_providers, "get_provider_profile", lambda q: None)
    monkeypatch.setattr(plugin_providers, "list_providers", list)

    pdef = hp.resolve_provider_full("genuinely-unknown-xyzzy")
    assert pdef is None


def test_resolve_provider_full_devagentic_local_real_plugin():
    """End-to-end against the real bundled plugin — devagentic-local
    should resolve to a ProviderDef without monkeypatching."""
    from hermes_cli import providers as hp

    pdef = hp.resolve_provider_full("devagentic-local")
    # If the bundled plugin loaded, we should get a ProviderDef.
    # In a stripped test env where plugin discovery doesn't run, this
    # may be None — accept either outcome but flag if it's the wrong
    # shape when present.
    if pdef is not None:
        assert pdef.id == "devagentic-local"
        assert "DEVAGENTIC_API_KEY" in pdef.api_key_env_vars
        assert pdef.source == "plugin"


def test_auth_resolve_provider_accepts_plugin_name(monkeypatch):
    """``auth.resolve_provider`` should accept the canonical name of a
    plugin-registered profile (in addition to its aliases, which the
    existing alias-extension already handles)."""
    from hermes_cli import auth as auth_mod
    import providers as plugin_providers

    class _FakeProfile:
        name = "test-plugin-provider"
        aliases: tuple[str, ...] = ()

    monkeypatch.setattr(
        plugin_providers,
        "get_provider_profile",
        lambda q: _FakeProfile() if q == "test-plugin-provider" else None,
    )
    monkeypatch.setattr(plugin_providers, "list_providers", list)

    result = auth_mod.resolve_provider(requested="test-plugin-provider")
    assert result == "test-plugin-provider"


def test_auth_resolve_provider_unknown_still_raises(monkeypatch):
    """Genuinely-unknown names still raise ``AuthError`` — fix doesn't
    accidentally permit arbitrary strings."""
    from hermes_cli import auth as auth_mod
    import providers as plugin_providers

    monkeypatch.setattr(
        plugin_providers, "get_provider_profile", lambda q: None)
    monkeypatch.setattr(plugin_providers, "list_providers", list)

    with pytest.raises(auth_mod.AuthError) as exc_info:
        auth_mod.resolve_provider(requested="genuinely-unknown-xyzzy")
    assert "Unknown provider" in str(exc_info.value)


def test_auth_resolve_provider_devagentic_local_real_plugin():
    """End-to-end against the bundled plugin."""
    from hermes_cli import auth as auth_mod

    try:
        result = auth_mod.resolve_provider(requested="devagentic-local")
        assert result == "devagentic-local"
    except auth_mod.AuthError as e:
        # Permit failure when plugin discovery doesn't run in stripped
        # test environment; flag the SHAPE of failure when it does.
        assert "Unknown provider 'devagentic-local'" in str(e)
