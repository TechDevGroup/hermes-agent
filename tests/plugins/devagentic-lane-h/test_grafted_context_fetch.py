"""Tests for fetch_grafted_context client (hermes-agent#71 PR2)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


_CLIENT_PATH = (Path(__file__).resolve().parents[3]
                / "plugins" / "devagentic-lane-h" / "client.py")


@pytest.fixture
def client_mod():
    spec = importlib.util.spec_from_file_location(
        "lane_h_client_grafted_test", _CLIENT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _ok(field: str, value) -> bytes:
    return json.dumps({"data": {field: value}}).encode("utf-8")


# ─── empty / missing inputs ───────────────────────────────────

def test_empty_graft_id_returns_none(client_mod):
    assert client_mod.fetch_grafted_context("") is None
    assert "graft_id is required" in (client_mod.last_error_text() or "")


def test_no_user_id_returns_none(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: None)
    assert client_mod.fetch_grafted_context("doc-x") is None
    err = client_mod.last_error_text() or ""
    assert "user_id" in err


# ─── happy path ───────────────────────────────────────────────

def test_happy_path(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    doc = {
        "id": "doc-graft-1",
        "userId": "alice",
        "source": "file:///workspace/rnspak",
        "ref": "main",
        "sha": "abc12345",
        "path": "concepts/crt.md",
        "content": "Chinese Remainder Theorem: residue tuple encoding...",
        "ts": "2026-05-24T03:00:00+00:00",
    }
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(_ok("graftedContextById", doc))

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _fake_urlopen)
    out = client_mod.fetch_grafted_context("doc-graft-1")
    assert out is not None
    assert out["content"].startswith("Chinese Remainder")
    assert out["path"] == "concepts/crt.md"
    assert captured["body"]["variables"]["g"] == "doc-graft-1"
    assert captured["body"]["variables"]["u"] == "alice"


def test_user_id_param_overrides_profile(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(_ok("graftedContextById", None))

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _fake_urlopen)
    client_mod.fetch_grafted_context("doc-x", user_id="bob")
    # When user_id arg passed, that overrides the profile-resolved id.
    assert captured["body"]["variables"]["u"] == "bob"


# ─── failure modes ───────────────────────────────────────────

def test_null_doc_returns_none_with_error(client_mod, monkeypatch):
    """Server-side returns null when graft id missing OR cross-user
    mismatch OR wrong kind; client surfaces an actionable error."""
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    monkeypatch.setattr(client_mod.urllib.request, "urlopen",
        lambda r, timeout=None: _FakeResp(_ok("graftedContextById", None)))
    assert client_mod.fetch_grafted_context("doc-missing") is None
    err = client_mod.last_error_text() or ""
    assert "no kind:grafted-context doc with id=" in err
    assert "cross-user" in err  # mention the cross-user possibility


def test_network_error_returns_none(client_mod, monkeypatch):
    import urllib.error
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    monkeypatch.setattr(client_mod.urllib.request, "urlopen",
        lambda r, timeout=None: (_ for _ in ()).throw(
            urllib.error.URLError("down")))
    assert client_mod.fetch_grafted_context("doc-x") is None
    assert "unreachable" in (client_mod.last_error_text() or "")


def test_graphql_errors_return_none(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    monkeypatch.setattr(client_mod.urllib.request, "urlopen",
        lambda r, timeout=None: _FakeResp(
            json.dumps({"errors": [{"message": "boom"}]}).encode("utf-8")))
    assert client_mod.fetch_grafted_context("doc-x") is None
    assert "graphql" in (client_mod.last_error_text() or "")


def test_non_dict_response_returns_none(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    monkeypatch.setattr(client_mod.urllib.request, "urlopen",
        lambda r, timeout=None: _FakeResp(
            _ok("graftedContextById", "not-a-dict")))
    assert client_mod.fetch_grafted_context("doc-x") is None
    assert "non-dict" in (client_mod.last_error_text() or "")
