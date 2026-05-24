"""Tests for plugins/hermes-github/client.py (G3 / hermes-agent#57)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


_CLIENT_PATH = (Path(__file__).resolve().parents[3]
                / "plugins" / "hermes-github" / "client.py")


@pytest.fixture
def client_mod(monkeypatch):
    # Fresh import per test so the module-level _last_error doesn't bleed.
    spec = importlib.util.spec_from_file_location(
        "hermes_github_client_under_test", _CLIENT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    # Wipe any env that would skew token resolution.
    for var in ("HERMES_GH_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return mod


class _FakeResp:
    def __init__(self, body: bytes, status: int = 201):
        self._body = body
        self.status = status
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


# ─── Allowed-repos restriction ────────────────────────────────

def test_repo_must_be_allowed(client_mod):
    out = client_mod.file_issue("private-repo", "t", "b")
    assert out is None
    err = client_mod.last_error_text() or ""
    assert "repo must be one of" in err
    assert "devagentic" in err
    assert "hermes-agent" in err


def test_repo_empty_rejected(client_mod):
    assert client_mod.file_issue("", "t", "b") is None
    assert "repo must be one of" in (client_mod.last_error_text() or "")


def test_allowed_repos_returns_frozenset(client_mod):
    s = client_mod.allowed_repos()
    assert isinstance(s, frozenset)
    assert s == frozenset({"devagentic", "hermes-agent"})


# ─── Arg validation ───────────────────────────────────────────

def test_empty_title_rejected(client_mod, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    assert client_mod.file_issue("devagentic", "", "body") is None
    assert "title" in (client_mod.last_error_text() or "")


def test_whitespace_title_rejected(client_mod, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    assert client_mod.file_issue("devagentic", "   ", "body") is None


def test_empty_body_rejected(client_mod, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    assert client_mod.file_issue("devagentic", "t", "") is None
    assert "body" in (client_mod.last_error_text() or "")


# ─── Token resolution ────────────────────────────────────────

def test_no_token_returns_none(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod.shutil, "which", lambda _: None)
    assert client_mod.file_issue("devagentic", "t", "b") is None
    err = client_mod.last_error_text() or ""
    assert "no GitHub token" in err
    assert "HERMES_GH_TOKEN" in err


def test_hermes_gh_token_takes_priority(client_mod, monkeypatch):
    monkeypatch.setenv("HERMES_GH_TOKEN", "ghp_hermes")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_pat")
    monkeypatch.setenv("GH_TOKEN", "ghp_cli")
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["auth"] = req.get_header("Authorization")
        body = json.dumps({"number": 999, "url": "u", "html_url": "h",
                           "title": "t", "state": "open"}).encode("utf-8")
        return _FakeResp(body, status=201)
    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _fake_urlopen)
    out = client_mod.file_issue("devagentic", "t", "b")
    assert out is not None
    assert captured["auth"] == "Bearer ghp_hermes"


def test_github_token_used_when_hermes_unset(client_mod, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_pat")
    monkeypatch.setattr(client_mod.shutil, "which", lambda _: None)
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["auth"] = req.get_header("Authorization")
        body = json.dumps({"number": 1, "url": "u", "html_url": "h",
                           "title": "t", "state": "open"}).encode("utf-8")
        return _FakeResp(body, status=201)
    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _fake_urlopen)
    assert client_mod.file_issue("devagentic", "t", "b") is not None
    assert captured["auth"] == "Bearer ghp_pat"


def test_gh_cli_fallback(client_mod, monkeypatch):
    monkeypatch.setattr(client_mod.shutil, "which", lambda n: "/usr/bin/gh" if n == "gh" else None)

    class _FakeProc:
        returncode = 0
        stdout = "ghp_from_cli\n"

    def _fake_run(*a, **k):
        return _FakeProc()
    monkeypatch.setattr(client_mod.subprocess, "run", _fake_run)

    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["auth"] = req.get_header("Authorization")
        body = json.dumps({"number": 7, "url": "u", "html_url": "h",
                           "title": "t", "state": "open"}).encode("utf-8")
        return _FakeResp(body, status=201)
    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _fake_urlopen)
    out = client_mod.file_issue("hermes-agent", "t", "b")
    assert out is not None and out["number"] == 7
    assert captured["auth"] == "Bearer ghp_from_cli"


# ─── HTTP behavior ────────────────────────────────────────────

def test_happy_path_returns_normalized_dict(client_mod, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        captured["accept"] = req.get_header("Accept")
        body = json.dumps({
            "number": 42, "url": "https://api.github.com/.../42",
            "html_url": "https://github.com/.../42",
            "title": "filed by worker", "state": "open",
            "extra_field": "ignored",
        }).encode("utf-8")
        return _FakeResp(body, status=201)
    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _fake_urlopen)

    out = client_mod.file_issue(
        "devagentic", "filed by worker", "body of issue",
        labels=["bug", "important"],
    )
    assert out is not None
    assert out["number"] == 42
    assert "extra_field" not in out  # normalized to a fixed shape
    assert captured["url"].endswith(
        "/repos/TechDevGroup/devagentic/issues")
    assert captured["payload"]["title"] == "filed by worker"
    assert captured["payload"]["labels"] == ["bug", "important"]
    assert "vnd.github" in captured["accept"]


def test_empty_labels_omitted_from_payload(client_mod, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        body = json.dumps({"number": 1, "url": "u", "html_url": "h",
                           "title": "t", "state": "open"}).encode("utf-8")
        return _FakeResp(body, status=201)
    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _fake_urlopen)

    client_mod.file_issue("devagentic", "t", "b", labels=[])
    assert "labels" not in captured["payload"]

    client_mod.file_issue("devagentic", "t", "b", labels=["", "  "])
    assert "labels" not in captured["payload"]


def test_http_error_surfaces_response_body(client_mod, monkeypatch):
    import urllib.error
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_bad")

    class _FakeHTTPError(urllib.error.HTTPError):
        def __init__(self):
            super().__init__(
                "u", 401, "Unauthorized", {},
                io_body=None,
            ) if False else None
            # simpler — just set attrs directly
        def read(self):
            return b'{"message":"Bad credentials"}'

    err = urllib.error.HTTPError(
        "https://api.github.com/.../issues", 401, "Unauthorized",
        hdrs={}, fp=None,
    )
    err.read = lambda: b'{"message":"Bad credentials"}'

    def _fake_urlopen(req, timeout=None):
        raise err
    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _fake_urlopen)

    assert client_mod.file_issue("devagentic", "t", "b") is None
    msg = client_mod.last_error_text() or ""
    assert "401" in msg
    assert "Bad credentials" in msg


def test_network_error_surfaces_kind(client_mod, monkeypatch):
    import urllib.error
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")

    def _fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("dns lookup failed")
    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _fake_urlopen)

    assert client_mod.file_issue("devagentic", "t", "b") is None
    msg = client_mod.last_error_text() or ""
    assert "unreachable" in msg


def test_invalid_json_response_returns_none(client_mod, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    monkeypatch.setattr(client_mod.urllib.request, "urlopen",
        lambda r, timeout=None: _FakeResp(b"not-json", status=201))
    assert client_mod.file_issue("devagentic", "t", "b") is None
    assert "not JSON" in (client_mod.last_error_text() or "")


def test_response_missing_number_returns_none(client_mod, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    monkeypatch.setattr(client_mod.urllib.request, "urlopen",
        lambda r, timeout=None: _FakeResp(b'{"foo":"bar"}', status=201))
    assert client_mod.file_issue("devagentic", "t", "b") is None
    assert "number" in (client_mod.last_error_text() or "")
