"""Tests for ``OpenVikingMemoryProvider.health_check`` (#42 step 2e).

Override hits ``<endpoint>/health`` via httpx and classifies the
status code (200 → ok; 401/403 → auth; 404 → not_found; other →
http; exception → unreachable). Reasons follow the RFC #42
prefix taxonomy.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import plugins.memory.openviking as ov
from plugins.memory.openviking import OpenVikingMemoryProvider


def _stub_httpx(monkeypatch, *, status_code=None, raises=None):
    """Patch the httpx accessor with a fake that returns a response
    of the requested status_code OR raises on .get()."""

    captured = {}

    def _get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        if raises is not None:
            raise raises
        return SimpleNamespace(status_code=status_code)

    fake = SimpleNamespace(get=_get)
    monkeypatch.setattr(ov, "_get_httpx", lambda: fake)
    return captured


def _set_endpoint(monkeypatch, val: str):
    if val:
        monkeypatch.setenv("OPENVIKING_ENDPOINT", val)
    else:
        monkeypatch.delenv("OPENVIKING_ENDPOINT", raising=False)


# ── Tests ──────────────────────────────────────────────────────


def test_returns_true_on_200(monkeypatch):
    _set_endpoint(monkeypatch, "http://ov:1933")
    cap = _stub_httpx(monkeypatch, status_code=200)
    healthy, reason = OpenVikingMemoryProvider().health_check()
    assert healthy is True
    assert reason == ""
    assert cap["url"].endswith("/health")
    assert cap["timeout"] == 3.0


def test_no_endpoint_when_env_unset(monkeypatch):
    _set_endpoint(monkeypatch, "")
    healthy, reason = OpenVikingMemoryProvider().health_check()
    assert healthy is False
    assert reason == "no_endpoint"


def test_sdk_missing_when_httpx_unavailable(monkeypatch):
    _set_endpoint(monkeypatch, "http://ov:1933")
    monkeypatch.setattr(ov, "_get_httpx", lambda: None)
    healthy, reason = OpenVikingMemoryProvider().health_check()
    assert healthy is False
    assert reason == "sdk_missing"


@pytest.mark.parametrize("code", [401, 403])
def test_auth_classified_for_401_403(monkeypatch, code):
    _set_endpoint(monkeypatch, "http://ov:1933")
    _stub_httpx(monkeypatch, status_code=code)
    healthy, reason = OpenVikingMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("auth:")
    assert str(code) in reason


def test_not_found_for_404(monkeypatch):
    _set_endpoint(monkeypatch, "http://ov:1933")
    _stub_httpx(monkeypatch, status_code=404)
    healthy, reason = OpenVikingMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("not_found:")
    assert "404" in reason
    assert "/health route missing" in reason


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_generic_http_for_5xx(monkeypatch, code):
    _set_endpoint(monkeypatch, "http://ov:1933")
    _stub_httpx(monkeypatch, status_code=code)
    healthy, reason = OpenVikingMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("http:")
    assert str(code) in reason


def test_unreachable_on_exception(monkeypatch):
    _set_endpoint(monkeypatch, "http://ov:1933")
    _stub_httpx(monkeypatch, raises=ConnectionError("Connection refused"))
    healthy, reason = OpenVikingMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("unreachable:")
    assert "Connection refused" in reason


def test_endpoint_trailing_slash_normalized(monkeypatch):
    """Operators sometimes set OPENVIKING_ENDPOINT with a trailing
    slash; probe must still build the right URL."""
    _set_endpoint(monkeypatch, "http://ov:1933/")
    cap = _stub_httpx(monkeypatch, status_code=200)
    OpenVikingMemoryProvider().health_check()
    assert cap["url"] == "http://ov:1933/health"  # not "//health"


def test_api_key_forwarded_when_set(monkeypatch):
    _set_endpoint(monkeypatch, "http://ov:1933")
    monkeypatch.setenv("OPENVIKING_API_KEY", "ovk-real")
    cap = _stub_httpx(monkeypatch, status_code=200)
    OpenVikingMemoryProvider().health_check()
    assert cap["headers"].get("Authorization") == "Bearer ovk-real"
    assert cap["headers"].get("X-API-Key") == "ovk-real"


def test_tenant_headers_default(monkeypatch):
    """ROOT keys need X-OpenViking-Account / X-OpenViking-User —
    the probe should include them with the documented defaults."""
    _set_endpoint(monkeypatch, "http://ov:1933")
    cap = _stub_httpx(monkeypatch, status_code=200)
    OpenVikingMemoryProvider().health_check()
    assert cap["headers"].get("X-OpenViking-Account") == "default"
    assert cap["headers"].get("X-OpenViking-User") == "default"
    assert cap["headers"].get("X-OpenViking-Agent") == "hermes"
