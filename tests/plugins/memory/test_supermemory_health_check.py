"""Tests for ``SupermemoryMemoryProvider.health_check`` (#42 step 2d).

Override does a real ``Supermemory(api_key).profile(container_tag=
"hermes-doctor-probe")`` round-trip — validates auth +
reachability without modifying any container state. Reasons
follow the RFC #42 prefix taxonomy.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from plugins.memory.supermemory import SupermemoryMemoryProvider


def _install_sdk(monkeypatch, *, profile_behavior=None,
                 init_raises=None):
    """Stub the `supermemory` SDK module with a controlled fake."""

    class _StubClient:
        def __init__(self, api_key, timeout=5.0, max_retries=0):
            if init_raises is not None:
                raise init_raises
            self.api_key = api_key

        def profile(self, **kwargs):
            if profile_behavior is None:
                return SimpleNamespace(profile=None, search_results=None)
            return profile_behavior(self, **kwargs)

    fake = SimpleNamespace(Supermemory=_StubClient)
    monkeypatch.setitem(sys.modules, "supermemory", fake)


def _set_key(monkeypatch, key: str):
    if key:
        monkeypatch.setenv("SUPERMEMORY_API_KEY", key)
    else:
        monkeypatch.delenv("SUPERMEMORY_API_KEY", raising=False)


# ── Tests ──────────────────────────────────────────────────────


def test_returns_true_on_successful_profile(monkeypatch):
    _set_key(monkeypatch, "real-key")
    _install_sdk(monkeypatch)
    healthy, reason = SupermemoryMemoryProvider().health_check()
    assert healthy is True
    assert reason == ""


def test_no_api_key_when_env_unset(monkeypatch):
    _set_key(monkeypatch, "")
    healthy, reason = SupermemoryMemoryProvider().health_check()
    assert healthy is False
    assert reason == "no_api_key"


def test_sdk_missing_when_supermemory_absent(monkeypatch):
    _set_key(monkeypatch, "k")
    monkeypatch.setitem(sys.modules, "supermemory", None)
    healthy, reason = SupermemoryMemoryProvider().health_check()
    assert healthy is False
    assert reason == "sdk_missing"


@pytest.mark.parametrize("err_msg", [
    "401 Unauthorized",
    "403 forbidden",
    "Invalid API key supplied",
    "authentication failed",
])
def test_auth_classified_correctly(monkeypatch, err_msg):
    _set_key(monkeypatch, "wrong")

    def _raise(self, **kwargs):
        raise RuntimeError(err_msg)

    _install_sdk(monkeypatch, profile_behavior=_raise)
    healthy, reason = SupermemoryMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("auth:")
    assert err_msg[:30] in reason


def test_network_error_classified_as_unreachable(monkeypatch):
    _set_key(monkeypatch, "real")

    def _raise(self, **kwargs):
        raise ConnectionError("Connection refused")

    _install_sdk(monkeypatch, profile_behavior=_raise)
    healthy, reason = SupermemoryMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("unreachable:")
    assert "Connection refused" in reason


def test_client_init_failure_classified_as_unreachable(monkeypatch):
    """When Supermemory(api_key=...) itself raises (e.g. SDK version
    mismatch, malformed key), the override surfaces it as unreachable
    rather than letting the exception propagate."""
    _set_key(monkeypatch, "k")
    _install_sdk(monkeypatch,
                 init_raises=RuntimeError("SDK init exploded"))
    healthy, reason = SupermemoryMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("unreachable:")
    assert "SDK init exploded" in reason


def test_truncates_long_errors(monkeypatch):
    _set_key(monkeypatch, "k")
    long_msg = "x" * 500

    def _raise(self, **kwargs):
        raise RuntimeError(long_msg)

    _install_sdk(monkeypatch, profile_behavior=_raise)
    healthy, reason = SupermemoryMemoryProvider().health_check()
    assert healthy is False
    # prefix ("unreachable: ") + 200 = 213
    assert len(reason) <= 220
