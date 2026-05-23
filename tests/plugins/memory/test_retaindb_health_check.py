"""Tests for ``RetainDBMemoryProvider.health_check`` (#42 step 2f).

The override does a GET /v1/memory/profile/hermes-doctor-probe
round-trip with the configured key, classifying the outcome with
the RFC #42 reason-prefix taxonomy (auth: / not_found: /
unreachable: / no_api_key / sdk_missing).
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import plugins.memory.retaindb as rdb
from plugins.memory.retaindb import RetainDBMemoryProvider


# ── Helpers ────────────────────────────────────────────────────


def _set_env(monkeypatch, *, api_key="real-key", base_url=None,
             project=None):
    if api_key:
        monkeypatch.setenv("RETAINDB_API_KEY", api_key)
    else:
        monkeypatch.delenv("RETAINDB_API_KEY", raising=False)
    if base_url is not None:
        monkeypatch.setenv("RETAINDB_BASE_URL", base_url)
    else:
        monkeypatch.delenv("RETAINDB_BASE_URL", raising=False)
    if project is not None:
        monkeypatch.setenv("RETAINDB_PROJECT", project)
    else:
        monkeypatch.delenv("RETAINDB_PROJECT", raising=False)


def _stub_client(monkeypatch, *, get_profile_raises=None,
                 get_profile_returns=None,
                 init_raises=None):
    """Patch `_Client` so its constructor + get_profile follow the
    test's chosen behavior."""

    class _FakeClient:
        def __init__(self, api_key, base_url, project):
            if init_raises is not None:
                raise init_raises
            self.api_key = api_key
            self.base_url = base_url
            self.project = project

        def get_profile(self, user_id):
            if get_profile_raises is not None:
                raise get_profile_raises
            return get_profile_returns or {}

    monkeypatch.setattr(rdb, "_Client", _FakeClient)


# ── Tests ──────────────────────────────────────────────────────


def test_returns_true_on_successful_probe(monkeypatch):
    _set_env(monkeypatch)
    _stub_client(monkeypatch, get_profile_returns={"memories": []})
    healthy, reason = RetainDBMemoryProvider().health_check()
    assert healthy is True
    assert reason == ""


def test_no_api_key_when_env_unset(monkeypatch):
    _set_env(monkeypatch, api_key="")
    healthy, reason = RetainDBMemoryProvider().health_check()
    assert healthy is False
    assert reason == "no_api_key"


def test_sdk_missing_when_requests_unavailable(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setitem(sys.modules, "requests", None)
    healthy, reason = RetainDBMemoryProvider().health_check()
    assert healthy is False
    assert reason == "sdk_missing"


@pytest.mark.parametrize("err_msg", [
    "RetainDB GET /v1/memory/profile/probe failed (401): unauthorized",
    "RetainDB GET /v1/memory/profile/probe failed (403): forbidden",
    "Invalid API key supplied",
    "authentication failed",
])
def test_auth_classified_correctly(monkeypatch, err_msg):
    _set_env(monkeypatch)
    _stub_client(monkeypatch,
                 get_profile_raises=RuntimeError(err_msg))
    healthy, reason = RetainDBMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("auth:"), reason
    assert err_msg[:40] in reason


def test_404_classified_as_not_found(monkeypatch):
    _set_env(monkeypatch, base_url="https://api.retaindb.invalid")
    err = "RetainDB GET /v1/memory/profile/probe failed (404): not found"
    _stub_client(monkeypatch, get_profile_raises=RuntimeError(err))
    healthy, reason = RetainDBMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("not_found:")
    assert "404" in reason


def test_network_error_classified_as_unreachable(monkeypatch):
    _set_env(monkeypatch)
    _stub_client(monkeypatch,
                 get_profile_raises=ConnectionError(
                     "Connection refused"))
    healthy, reason = RetainDBMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("unreachable:")
    assert "Connection refused" in reason


def test_client_init_failure_classified_as_unreachable(monkeypatch):
    """When _Client(api_key=...) itself raises (e.g. bad base_url
    parsing), the override surfaces it as unreachable rather than
    propagating."""
    _set_env(monkeypatch)
    _stub_client(monkeypatch,
                 init_raises=RuntimeError("bad base_url"))
    healthy, reason = RetainDBMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("unreachable:")
    assert "bad base_url" in reason


def test_truncates_long_errors(monkeypatch):
    _set_env(monkeypatch)
    long_msg = "x" * 500
    _stub_client(monkeypatch,
                 get_profile_raises=RuntimeError(long_msg))
    healthy, reason = RetainDBMemoryProvider().health_check()
    assert healthy is False
    # prefix ("unreachable: ") + 200 = 213
    assert len(reason) <= 220


def test_uses_custom_base_url_when_set(monkeypatch):
    """Operators with self-hosted RetainDB set RETAINDB_BASE_URL;
    the probe must pass that through to the client (not the default)."""
    captured = {}

    class _CapturingClient:
        def __init__(self, api_key, base_url, project):
            captured["base_url"] = base_url

        def get_profile(self, user_id):
            return {}

    monkeypatch.setattr(rdb, "_Client", _CapturingClient)
    _set_env(monkeypatch, base_url="https://retaindb.self-hosted.test")
    RetainDBMemoryProvider().health_check()
    assert captured["base_url"] == "https://retaindb.self-hosted.test"


def test_trailing_slash_base_url_normalized(monkeypatch):
    captured = {}

    class _CapturingClient:
        def __init__(self, api_key, base_url, project):
            captured["base_url"] = base_url

        def get_profile(self, user_id):
            return {}

    monkeypatch.setattr(rdb, "_Client", _CapturingClient)
    _set_env(monkeypatch,
             base_url="https://api.retaindb.com//")
    RetainDBMemoryProvider().health_check()
    assert captured["base_url"] == "https://api.retaindb.com"
