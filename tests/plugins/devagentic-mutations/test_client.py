"""Tests for plugins/devagentic-mutations/client.py.

Verifies the GraphQL transport for ``query_silo`` + ``run_confer_loop``:
  * fail-soft: returns None on missing user_id, network exception,
    parse error, ``errors`` in payload, empty args.
  * happy path: returns the parsed sub-dict on a well-formed
    GraphQL response.
  * env-override user_id resolution.
  * last_error_text() populated on failures.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


_CLIENT_PATH = (Path(__file__).resolve().parents[3]
                / "plugins" / "devagentic-mutations" / "client.py")


@pytest.fixture
def client_mod():
    spec = importlib.util.spec_from_file_location(
        "devagentic_mutations_client_under_test", _CLIENT_PATH)
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


def _ok_payload(field: str, value):
    return json.dumps({"data": {field: value}}).encode("utf-8")


# ─── User-id resolution ───────────────────────────────────────

def test_resolve_user_id_env_override(client_mod, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "override-user")
    assert client_mod.resolve_user_id() == "override-user"


def test_resolve_user_id_missing(client_mod, monkeypatch):
    monkeypatch.delenv("DEVAGENTIC_USER_ID", raising=False)
    import builtins
    real_import = builtins.__import__

    def _fake_import(name, *a, **k):
        if name == "hermes_cli.profiles":
            raise ImportError("blocked for test")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _fake_import)
    assert client_mod.resolve_user_id() is None


# ─── query_silo ───────────────────────────────────────────────

def test_query_silo_empty_args_return_none(client_mod):
    assert client_mod.query_silo("", "prompt") is None
    assert client_mod.query_silo("name", "") is None
    assert "required" in (client_mod.last_error_text() or "")


def test_query_silo_no_user_id_returns_none(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: None)
    assert client_mod.query_silo("silo", "prompt") is None
    assert "user_id" in (client_mod.last_error_text() or "")


def test_query_silo_happy_path(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    reply = {
        "siloId": "silo-1", "siloName": "silo-foo", "text": "answer",
        "cachedTokenCount": 12, "promptTokenCount": 34,
        "totalTokenCount": 80,
    }
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(_ok_payload("querySilo", reply))

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _fake_urlopen)
    out = client_mod.query_silo("silo-foo", "p", role_override="aider")
    assert out is not None
    assert out["siloName"] == "silo-foo"
    assert out["text"] == "answer"
    assert captured["body"]["variables"]["n"] == "silo-foo"
    assert captured["body"]["variables"]["p"] == "p"
    assert captured["body"]["variables"]["r"] == "aider"


def test_query_silo_network_error_returns_none(client_mod, monkeypatch):
    import urllib.error
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    monkeypatch.setattr(client_mod.urllib.request, "urlopen",
        lambda r, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("down")))
    assert client_mod.query_silo("s", "p") is None
    assert "unreachable" in (client_mod.last_error_text() or "")


def test_query_silo_graphql_errors_returns_none(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    payload = json.dumps({"errors": [{"message": "boom"}], "data": None}).encode("utf-8")
    monkeypatch.setattr(client_mod.urllib.request, "urlopen",
        lambda r, timeout=None: _FakeResp(payload))
    assert client_mod.query_silo("s", "p") is None
    assert "graphql" in (client_mod.last_error_text() or "")


def test_query_silo_invalid_json_returns_none(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    monkeypatch.setattr(client_mod.urllib.request, "urlopen",
        lambda r, timeout=None: _FakeResp(b"not-json"))
    assert client_mod.query_silo("s", "p") is None


# ─── run_confer_loop ──────────────────────────────────────────

def test_run_confer_loop_empty_args_return_none(client_mod):
    assert client_mod.run_confer_loop("", "cand") is None
    assert client_mod.run_confer_loop("uid", "") is None


def test_run_confer_loop_happy_path(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    rollup = {
        "confer_result_id": "cr-1",
        "candidate_id": "cand-9",
        "consensus_action": "promote",
        "confidence": 0.83,
        "silos_consulted": ["silo-a", "silo-b", "silo-c"],
    }
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(_ok_payload("runConferLoop", rollup))

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _fake_urlopen)
    out = client_mod.run_confer_loop("alice", "cand-9")
    assert out is not None
    assert out["consensus_action"] == "promote"
    assert out["confidence"] == 0.83
    assert captured["body"]["variables"]["u"] == "alice"
    assert captured["body"]["variables"]["c"] == "cand-9"


def test_run_confer_loop_returns_null_data_returns_none(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    payload = json.dumps({"data": {"runConferLoop": None}}).encode("utf-8")
    monkeypatch.setattr(client_mod.urllib.request, "urlopen",
        lambda r, timeout=None: _FakeResp(payload))
    assert client_mod.run_confer_loop("uid", "cand") is None
    assert "null" in (client_mod.last_error_text() or "")


def test_run_confer_loop_no_user_id_returns_none(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: None)
    assert client_mod.run_confer_loop("uid", "cand") is None


# ─── assert_output (G2b, #60) ─────────────────────────────────

def test_assert_output_empty_args_return_none(client_mod):
    """Both call_id and fragment are required — empty either
    short-circuits before any network call."""
    assert client_mod.assert_output("", "content") is None
    assert client_mod.assert_output("call-1", "") is None


def test_assert_output_no_user_id_returns_none(client_mod, monkeypatch):
    """The transport short-circuits when X-User-Id can't be resolved
    (same fail-soft contract as the other mutations)."""
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: None)
    assert client_mod.assert_output("call-1", "content") is None


def test_assert_output_happy_path(client_mod, monkeypatch):
    """Verdict round-trips through the GraphQL response unchanged."""
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    verdict = {
        "id": "verdict-9",
        "ts": "2026-05-25T01:50:00Z",
        "callId": "call-7",
        "passed": True,
        "violations": [],
    }
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(_ok_payload("assertOutput", verdict))

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _fake_urlopen)
    out = client_mod.assert_output(
        "call-7", "content", predicate="len > 0")
    assert out is not None
    assert out["id"] == "verdict-9"
    assert out["passed"] is True
    # Variables threaded into ExpectationInput shape.
    assert captured["body"]["variables"]["cid"] == "call-7"
    assert captured["body"]["variables"]["ef"] == "content"
    assert captured["body"]["variables"]["ep"] == "len > 0"


def test_assert_output_predicate_omitted_sends_null(client_mod, monkeypatch):
    """Devagentic-side _stiffen_predicate substitutes a tool floor
    when the predicate is None — verify the client passes None
    (not the literal string "None" or empty string) so the resolver
    sees an absent predicate."""
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    verdict = {"id": "v-1", "ts": "t", "callId": "c-1",
               "passed": False, "violations": ["coerced floor"]}
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(_ok_payload("assertOutput", verdict))

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _fake_urlopen)
    out = client_mod.assert_output("c-1", "content")
    assert out is not None
    assert captured["body"]["variables"]["ep"] is None


def test_assert_output_empty_predicate_sends_null(client_mod, monkeypatch):
    """An empty-string predicate is equivalent to omission — same
    transport behavior so the devagentic resolver's stiffener fires."""
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    verdict = {"id": "v-2", "ts": "t", "callId": "c-2",
               "passed": True, "violations": []}
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(_ok_payload("assertOutput", verdict))

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _fake_urlopen)
    client_mod.assert_output("c-2", "content", predicate="")
    assert captured["body"]["variables"]["ep"] is None


def test_assert_output_returns_null_data_returns_none(
        client_mod, monkeypatch):
    """Devagentic-side resolver raises ValueError for unknown call_id;
    GraphQL wraps that as ``errors`` — _post_graphql returns None
    and last_error_text() carries the surface."""
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    payload = json.dumps({"data": {"assertOutput": None}}).encode("utf-8")
    monkeypatch.setattr(client_mod.urllib.request, "urlopen",
        lambda r, timeout=None: _FakeResp(payload))
    assert client_mod.assert_output("missing-call", "content") is None


def test_assert_output_non_dict_response_returns_none(
        client_mod, monkeypatch):
    monkeypatch.setattr(client_mod, "resolve_user_id", lambda: "alice")
    payload = json.dumps(
        {"data": {"assertOutput": "not-a-dict"}}).encode("utf-8")
    monkeypatch.setattr(client_mod.urllib.request, "urlopen",
        lambda r, timeout=None: _FakeResp(payload))
    assert client_mod.assert_output("c", "f") is None
    assert "non-dict" in (client_mod.last_error_text() or "")
