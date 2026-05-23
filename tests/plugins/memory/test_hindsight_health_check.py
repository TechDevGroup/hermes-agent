"""Tests for ``HindsightMemoryProvider.health_check`` (#42 step 2g).

The override dispatches by config `mode` — local modes do an
import probe; cloud / local_external modes hit `<api_url>/version`.
Reasons follow the RFC #42 prefix taxonomy.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from types import SimpleNamespace

import pytest

import plugins.memory.hindsight as hs
from plugins.memory.hindsight import HindsightMemoryProvider


# ── Helpers ────────────────────────────────────────────────────


def _stub_config(monkeypatch, **cfg):
    monkeypatch.setattr(hs, "_load_config", lambda: cfg)


def _stub_local_runtime(monkeypatch, *, available: bool,
                        error: str | None = None):
    monkeypatch.setattr(
        hs, "_check_local_runtime",
        lambda: (available, error))


def _stub_urlopen(monkeypatch, *, response_payload=None,
                   raises=None):
    """Replace urllib.request.urlopen with a fake. response_payload
    is bytes returned by .read(); raises is an exception to throw."""
    import urllib.request as _ur

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self._payload

    def _urlopen(req, timeout=None):
        if raises is not None:
            raise raises
        return _Resp(
            response_payload if response_payload is not None
            else json.dumps({"version": "0.5.6"}).encode("utf-8"))

    monkeypatch.setattr(_ur, "urlopen", _urlopen)


# ── Local modes ────────────────────────────────────────────────


def test_local_returns_true_when_runtime_imports(monkeypatch):
    _stub_config(monkeypatch, mode="local")
    _stub_local_runtime(monkeypatch, available=True)
    healthy, reason = HindsightMemoryProvider().health_check()
    assert healthy is True
    assert reason == ""


def test_local_embedded_returns_true_when_runtime_imports(monkeypatch):
    _stub_config(monkeypatch, mode="local_embedded")
    _stub_local_runtime(monkeypatch, available=True)
    healthy, reason = HindsightMemoryProvider().health_check()
    assert healthy is True
    assert reason == ""


def test_local_returns_sdk_missing_when_import_fails(monkeypatch):
    _stub_config(monkeypatch, mode="local")
    _stub_local_runtime(monkeypatch, available=False,
                        error="numpy ABI mismatch on this CPU")
    healthy, reason = HindsightMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("sdk_missing:")
    assert "numpy ABI mismatch" in reason


# ── local_external (URL but no key required) ───────────────────


def test_local_external_no_url_returns_no_url(monkeypatch):
    _stub_config(monkeypatch, mode="local_external")
    monkeypatch.delenv("HINDSIGHT_API_URL", raising=False)
    healthy, reason = HindsightMemoryProvider().health_check()
    assert healthy is False
    assert reason == "no_url"


def test_local_external_with_url_probes_version(monkeypatch):
    _stub_config(monkeypatch, mode="local_external",
                 api_url="http://hindsight-local:9999")
    _stub_urlopen(monkeypatch)
    healthy, reason = HindsightMemoryProvider().health_check()
    assert healthy is True


# ── Cloud mode ─────────────────────────────────────────────────


def test_cloud_no_credentials_when_both_empty(monkeypatch):
    _stub_config(monkeypatch, mode="cloud")
    monkeypatch.delenv("HINDSIGHT_API_KEY", raising=False)
    monkeypatch.delenv("HINDSIGHT_API_URL", raising=False)
    healthy, reason = HindsightMemoryProvider().health_check()
    assert healthy is False
    assert reason == "no_credentials"


def test_cloud_no_url_when_only_key(monkeypatch):
    _stub_config(monkeypatch, mode="cloud", apiKey="real-key")
    monkeypatch.delenv("HINDSIGHT_API_URL", raising=False)
    healthy, reason = HindsightMemoryProvider().health_check()
    assert healthy is False
    assert reason == "no_url"


def test_cloud_success(monkeypatch):
    _stub_config(monkeypatch, mode="cloud", apiKey="real",
                 api_url="https://api.hindsight.test")
    _stub_urlopen(monkeypatch)
    healthy, reason = HindsightMemoryProvider().health_check()
    assert healthy is True


def test_cloud_auth_401(monkeypatch):
    _stub_config(monkeypatch, mode="cloud", apiKey="wrong",
                 api_url="https://api.hindsight.test")
    _stub_urlopen(monkeypatch,
                   raises=urllib.error.HTTPError(
                       "http://x/version", 401, "Unauthorized",
                       {}, None))
    healthy, reason = HindsightMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("auth:")
    assert "401" in reason


def test_cloud_404_is_not_found(monkeypatch):
    _stub_config(monkeypatch, mode="cloud", apiKey="k",
                 api_url="https://api.hindsight.test")
    _stub_urlopen(monkeypatch,
                   raises=urllib.error.HTTPError(
                       "http://x/version", 404, "NF", {}, None))
    healthy, reason = HindsightMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("not_found:")
    assert "404" in reason


def test_cloud_other_http_classified_as_http(monkeypatch):
    _stub_config(monkeypatch, mode="cloud", apiKey="k",
                 api_url="https://api.hindsight.test")
    _stub_urlopen(monkeypatch,
                   raises=urllib.error.HTTPError(
                       "http://x/version", 503, "Down", {}, None))
    healthy, reason = HindsightMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("http:")
    assert "503" in reason


def test_cloud_connection_refused_classified_as_unreachable(monkeypatch):
    _stub_config(monkeypatch, mode="cloud", apiKey="k",
                 api_url="https://api.hindsight.test")
    _stub_urlopen(monkeypatch,
                   raises=urllib.error.URLError("connection refused"))
    healthy, reason = HindsightMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("unreachable:")
    assert "connection refused" in reason


def test_cloud_malformed_response_classified_as_unreachable(
        monkeypatch):
    _stub_config(monkeypatch, mode="cloud", apiKey="k",
                 api_url="https://api.hindsight.test")
    _stub_urlopen(monkeypatch, response_payload=b"not json")
    healthy, reason = HindsightMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("unreachable:")


def test_cloud_non_dict_json_classified_as_unreachable(monkeypatch):
    _stub_config(monkeypatch, mode="cloud", apiKey="k",
                 api_url="https://api.hindsight.test")
    _stub_urlopen(monkeypatch, response_payload=b'["array", "not", "dict"]')
    healthy, reason = HindsightMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("unreachable:")
    assert "non-dict" in reason


# ── Top-level config failure ──────────────────────────────────


def test_config_error_when_load_config_raises(monkeypatch):
    monkeypatch.setattr(
        hs, "_load_config",
        lambda: (_ for _ in ()).throw(RuntimeError("config corrupted")))
    healthy, reason = HindsightMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("config_error:")
    assert "config corrupted" in reason


# ── Env-var fallback ──


def test_env_vars_used_when_config_missing_keys(monkeypatch):
    _stub_config(monkeypatch, mode="cloud")  # No api_url/apiKey in cfg
    monkeypatch.setenv("HINDSIGHT_API_KEY", "env-key")
    monkeypatch.setenv("HINDSIGHT_API_URL", "https://env.test")
    _stub_urlopen(monkeypatch)
    healthy, reason = HindsightMemoryProvider().health_check()
    assert healthy is True
