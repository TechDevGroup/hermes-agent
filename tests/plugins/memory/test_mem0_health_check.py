"""Tests for ``Mem0MemoryProvider.health_check`` (#42 step 2a).

The override does a real ``MemoryClient.get_all(user_id, limit=1)``
round-trip and classifies the outcome with reason-prefix
conventions from RFC #42 (auth: / unreachable: / sdk_missing /
no_api_key / config_error:). Doctor will eventually replace its
provider-specific elif blocks with a generic call to this method.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from plugins.memory.mem0 import Mem0MemoryProvider


# ── Helpers ────────────────────────────────────────────────────

def _install_mem0_sdk(monkeypatch, client_behavior):
    """Stub the `mem0` SDK module so `from mem0 import MemoryClient`
    in health_check resolves to a controlled fake."""

    class _StubMemoryClient:
        def __init__(self, api_key):
            self.api_key = api_key

        def get_all(self, **kwargs):
            return client_behavior(self, **kwargs)

    fake = SimpleNamespace(MemoryClient=_StubMemoryClient)
    monkeypatch.setitem(sys.modules, "mem0", fake)


def _set_api_key(monkeypatch, key: str):
    if key:
        monkeypatch.setenv("MEM0_API_KEY", key)
    else:
        monkeypatch.delenv("MEM0_API_KEY", raising=False)


# ── Tests ──────────────────────────────────────────────────────

def test_health_check_returns_true_on_success(monkeypatch):
    _set_api_key(monkeypatch, "real-key")
    _install_mem0_sdk(monkeypatch,
                      client_behavior=lambda self, **kw: [])
    healthy, reason = Mem0MemoryProvider().health_check()
    assert healthy is True
    assert reason == ""


def test_health_check_returns_no_api_key_when_unset(monkeypatch):
    _set_api_key(monkeypatch, "")
    healthy, reason = Mem0MemoryProvider().health_check()
    assert healthy is False
    assert reason == "no_api_key"


def test_health_check_returns_sdk_missing_when_mem0ai_absent(
        monkeypatch):
    _set_api_key(monkeypatch, "k")
    monkeypatch.setitem(sys.modules, "mem0", None)
    healthy, reason = Mem0MemoryProvider().health_check()
    assert healthy is False
    assert reason == "sdk_missing"


@pytest.mark.parametrize("err_msg", [
    "401 Unauthorized",
    "403 forbidden — quota exceeded",
    "HTTP 400: invalid api key supplied",
    "authentication failed: bad token",
])
def test_health_check_classifies_auth_errors(monkeypatch, err_msg):
    _set_api_key(monkeypatch, "wrong")

    def _raise(self, **kw):
        raise RuntimeError(err_msg)

    _install_mem0_sdk(monkeypatch, client_behavior=_raise)
    healthy, reason = Mem0MemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("auth:")
    assert err_msg[:50] in reason


def test_health_check_classifies_network_errors_as_unreachable(
        monkeypatch):
    _set_api_key(monkeypatch, "real")

    def _raise(self, **kw):
        raise ConnectionError("Connection refused")

    _install_mem0_sdk(monkeypatch, client_behavior=_raise)
    healthy, reason = Mem0MemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("unreachable:")
    assert "Connection refused" in reason


def test_health_check_truncates_long_error_messages(monkeypatch):
    _set_api_key(monkeypatch, "real")
    long_msg = "x" * 500

    def _raise(self, **kw):
        raise RuntimeError(long_msg)

    _install_mem0_sdk(monkeypatch, client_behavior=_raise)
    healthy, reason = Mem0MemoryProvider().health_check()
    assert healthy is False
    # Prefix + ": " + at most 200 chars = under 220.
    assert len(reason) <= 220


def test_health_check_never_raises(monkeypatch):
    """RFC #42 contract: health_check MUST NOT raise. Even if
    every internal step explodes, the method returns a tuple."""

    # Force _load_config to raise.
    monkeypatch.setattr(
        "plugins.memory.mem0._load_config",
        lambda: (_ for _ in ()).throw(RuntimeError("config broken")))

    # Method must not raise — even when _load_config itself fails.
    healthy, reason = Mem0MemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("config_error:")
    assert "config broken" in reason
