"""Tests for plugins/devagentic-lane-h/client.py (G4 / hermes-agent#58)."""
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
        "devagentic_lane_h_client_under_test", _CLIENT_PATH)
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


# ─── User-id resolution ───────────────────────────────────────

def test_resolve_user_id_env_override(client_mod, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "override-u")
    assert client_mod.resolve_user_id() == "override-u"


# ─── list_reasoning_grafts ────────────────────────────────────

def test_list_returns_none_when_no_user_id(client_mod, monkeypatch):
    monkeypatch.delenv("DEVAGENTIC_USER_ID", raising=False)
    import builtins
    real_import = builtins.__import__

    def _block_profile(name, *a, **k):
        if name == "hermes_cli.profiles":
            raise ImportError("blocked")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _block_profile)
    assert client_mod.list_reasoning_grafts() is None
    assert "user_id" in (client_mod.last_error_text() or "")


def test_list_zero_limit_returns_empty(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    assert client_mod.list_reasoning_grafts(limit=0) == []


def test_list_happy_path(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    rows = [
        {"id": "doc-1", "userId": "alice", "content": "c1",
         "ts": "2026-05-24T01:00:00+00:00",
         "conferResultRef": "cr-1", "candidateId": "cand-1",
         "conferConfidence": 0.91,
         "scaffoldMatchStatus": "not_evaluated",
         "h7GateStatus": "not_evaluated"},
        {"id": "doc-2", "userId": "alice", "content": "c2",
         "ts": "2026-05-24T00:30:00+00:00",
         "conferResultRef": "cr-2", "candidateId": "cand-2",
         "conferConfidence": 0.85,
         "scaffoldMatchStatus": "not_evaluated",
         "h7GateStatus": "not_evaluated"},
    ]
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["x_user_id"] = req.get_header("X-user-id")
        return _FakeResp(_ok("reasoningGraftCandidates", rows))

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _fake_urlopen)
    out = client_mod.list_reasoning_grafts(limit=10)
    assert out is not None
    assert len(out) == 2
    assert out[0]["id"] == "doc-1"
    assert out[0]["conferConfidence"] == 0.91
    assert captured["body"]["variables"]["u"] == "alice"
    assert captured["body"]["variables"]["l"] == 10
    assert captured["x_user_id"] == "alice"


def test_list_user_id_param_overrides_profile(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["x_user_id"] = req.get_header("X-user-id")
        return _FakeResp(_ok("reasoningGraftCandidates", []))

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _fake_urlopen)
    out = client_mod.list_reasoning_grafts(user_id="bob", limit=5)
    assert out == []
    assert captured["body"]["variables"]["u"] == "bob"
    # NOTE: X-User-Id header still uses the resolved profile (alice). The
    # per-request user_id ARG is what scopes the query result; the header
    # is just the transport-level auth scope. This is intentional — the
    # operator may want to query bob's grafts from alice's hermes profile.
    assert captured["x_user_id"] == "alice"


def test_list_network_error_returns_none(client_mod, monkeypatch):
    import urllib.error
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    monkeypatch.setattr(client_mod.urllib.request, "urlopen",
        lambda r, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("down")))
    assert client_mod.list_reasoning_grafts(limit=5) is None
    assert "unreachable" in (client_mod.last_error_text() or "")


def test_list_graphql_errors_return_none(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    monkeypatch.setattr(client_mod.urllib.request, "urlopen",
        lambda r, timeout=None: _FakeResp(
            json.dumps({"errors": [{"message": "boom"}]}).encode("utf-8")))
    assert client_mod.list_reasoning_grafts(limit=5) is None
    assert "graphql" in (client_mod.last_error_text() or "")


# ─── fetch_reasoning_graft ────────────────────────────────────

def test_fetch_empty_id_returns_none(client_mod):
    assert client_mod.fetch_reasoning_graft("") is None
    assert "graft_id" in (client_mod.last_error_text() or "")


def test_fetch_happy_path(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    doc = {"id": "graft-xyz", "content": "lift result here",
           "tags": ["kind:reasoning-graft-candidate", "user:alice",
                    "confer:cr-1", "candidate:cand-1"],
           "source": "lane-h-auto-trigger",
           "ts": "2026-05-24T01:00:00+00:00"}
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(_ok("searchDocs", [doc]))

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _fake_urlopen)
    out = client_mod.fetch_reasoning_graft("graft-xyz")
    assert out is not None
    assert out["id"] == "graft-xyz"
    assert "kind:reasoning-graft-candidate" in out["tags"]
    assert captured["body"]["variables"]["q"] == "graft-xyz"


def test_fetch_no_match_returns_none(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    monkeypatch.setattr(client_mod.urllib.request, "urlopen",
        lambda r, timeout=None: _FakeResp(_ok("searchDocs", [])))
    assert client_mod.fetch_reasoning_graft("doc-missing") is None
    assert "no doc matched" in (client_mod.last_error_text() or "")


def test_fetch_top_id_mismatch_returns_none(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    doc = {"id": "doc-different", "content": "x",
           "tags": ["kind:reasoning-graft-candidate"],
           "source": "x", "ts": "x"}
    monkeypatch.setattr(client_mod.urllib.request, "urlopen",
        lambda r, timeout=None: _FakeResp(_ok("searchDocs", [doc])))
    assert client_mod.fetch_reasoning_graft("doc-requested") is None
    assert "top match" in (client_mod.last_error_text() or "")


def test_fetch_wrong_kind_returns_none(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    doc = {"id": "doc-not-lane-h", "content": "x",
           "tags": ["kind:scaffold", "user:alice"],
           "source": "x", "ts": "x"}
    monkeypatch.setattr(client_mod.urllib.request, "urlopen",
        lambda r, timeout=None: _FakeResp(_ok("searchDocs", [doc])))
    assert client_mod.fetch_reasoning_graft("doc-not-lane-h") is None
    err = client_mod.last_error_text() or ""
    assert "not a kind:reasoning-graft-candidate" in err
