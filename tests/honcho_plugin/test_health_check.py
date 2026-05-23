"""Tests for ``HonchoMemoryProvider.health_check`` (#42 step 2b).

The override does a real ``get_honcho_client(cfg)`` handshake and
classifies the outcome with the RFC #42 reason-prefix taxonomy.
Doctor will eventually replace its provider-specific `elif` block
with a generic call to this method.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _import_provider():
    from plugins.memory.honcho import HonchoMemoryProvider
    return HonchoMemoryProvider


def _install_client_stubs(
    monkeypatch,
    *,
    config_path_exists: bool,
    enabled: bool = True,
    api_key: str = "k",
    base_url: str = "",
    get_client_behavior=None,
):
    """Install fakes for plugins.memory.honcho.client members the
    provider's health_check uses. Each parameter controls one
    branch of the dispatch."""

    class _FakeConfig:
        def __init__(self):
            self.enabled = enabled
            self.api_key = api_key
            self.base_url = base_url
            # Fields doctor would inspect, kept for future use.
            self.workspace_id = "ws"
            self.recall_mode = "hybrid"
            self.write_frequency = "after_each_turn"

        @classmethod
        def from_global_config(cls):
            return cls()

    class _ConfigPath:
        def exists(self):
            return config_path_exists

    def _resolve_config_path():
        return _ConfigPath()

    def _reset_honcho_client():
        return None

    def _get_honcho_client(cfg):
        if get_client_behavior is None:
            return SimpleNamespace()  # successful handshake
        return get_client_behavior(cfg)

    fake_client_module = SimpleNamespace(
        HonchoClientConfig=_FakeConfig,
        resolve_config_path=_resolve_config_path,
        reset_honcho_client=_reset_honcho_client,
        get_honcho_client=_get_honcho_client,
    )
    monkeypatch.setitem(
        sys.modules, "plugins.memory.honcho.client",
        fake_client_module)
    return fake_client_module


# ── Tests ──────────────────────────────────────────────────────


def test_returns_true_when_handshake_succeeds(monkeypatch):
    _install_client_stubs(monkeypatch, config_path_exists=True)
    provider = _import_provider()()
    healthy, reason = provider.health_check()
    assert healthy is True
    assert reason == ""


def test_no_config_when_path_absent(monkeypatch):
    _install_client_stubs(monkeypatch, config_path_exists=False)
    provider = _import_provider()()
    healthy, reason = provider.health_check()
    assert healthy is False
    assert reason == "no_config"


def test_disabled_returned_when_config_disabled(monkeypatch):
    _install_client_stubs(monkeypatch, config_path_exists=True,
                          enabled=False)
    provider = _import_provider()()
    healthy, reason = provider.health_check()
    assert healthy is False
    assert reason == "disabled"


def test_no_credentials_when_both_empty(monkeypatch):
    _install_client_stubs(monkeypatch, config_path_exists=True,
                          api_key="", base_url="")
    provider = _import_provider()()
    healthy, reason = provider.health_check()
    assert healthy is False
    assert reason == "no_credentials"


def test_base_url_only_counts_as_credentials(monkeypatch):
    """Port #2645: api_key OR base_url is sufficient — base_url
    alone (self-hosted) should NOT be classified as no_credentials."""
    _install_client_stubs(monkeypatch, config_path_exists=True,
                          api_key="", base_url="http://localhost:8080")
    provider = _import_provider()()
    healthy, reason = provider.health_check()
    assert healthy is True
    assert reason == ""


@pytest.mark.parametrize("err_msg", [
    "401 Unauthorized",
    "403 forbidden",
    "invalid api key",
    "authentication failed",
])
def test_auth_classified_correctly(monkeypatch, err_msg):
    def _raise_auth(cfg):
        raise RuntimeError(err_msg)

    _install_client_stubs(monkeypatch, config_path_exists=True,
                          get_client_behavior=_raise_auth)
    provider = _import_provider()()
    healthy, reason = provider.health_check()
    assert healthy is False
    assert reason.startswith("auth:")
    assert err_msg[:30] in reason


def test_network_error_classified_as_unreachable(monkeypatch):
    def _raise_net(cfg):
        raise ConnectionError("Connection refused")

    _install_client_stubs(monkeypatch, config_path_exists=True,
                          get_client_behavior=_raise_net)
    provider = _import_provider()()
    healthy, reason = provider.health_check()
    assert healthy is False
    assert reason.startswith("unreachable:")
    assert "Connection refused" in reason


def test_never_raises_on_config_error(monkeypatch):
    """RFC #42 contract: health_check must never raise. Even when
    HonchoClientConfig.from_global_config() itself throws, the
    method returns a tuple."""

    class _BadConfig:
        @classmethod
        def from_global_config(cls):
            raise RuntimeError("config malformed")

    class _ConfigPath:
        def exists(self):
            return True

    fake_client_module = SimpleNamespace(
        HonchoClientConfig=_BadConfig,
        resolve_config_path=lambda: _ConfigPath(),
        reset_honcho_client=lambda: None,
        get_honcho_client=lambda cfg: None,
    )
    monkeypatch.setitem(
        sys.modules, "plugins.memory.honcho.client",
        fake_client_module)
    provider = _import_provider()()
    healthy, reason = provider.health_check()
    assert healthy is False
    assert reason.startswith("config_error:")
    assert "config malformed" in reason


def test_truncates_long_error_messages(monkeypatch):
    long_msg = "x" * 500

    def _raise(cfg):
        raise RuntimeError(long_msg)

    _install_client_stubs(monkeypatch, config_path_exists=True,
                          get_client_behavior=_raise)
    provider = _import_provider()()
    healthy, reason = provider.health_check()
    assert healthy is False
    # prefix + ": " + ≤200 = ≤220
    assert len(reason) <= 220
