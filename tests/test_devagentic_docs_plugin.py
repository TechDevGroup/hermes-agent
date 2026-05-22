"""Unit tests for the devagentic-docs plugin (hermes #12).
Mirrors the structure of test_devagentic_canvas_plugin.py — same
synthetic-package fixture, same stubbing pattern."""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest
import yaml


PLUGIN_DIR = (Path(__file__).resolve().parents[1]
              / "plugins" / "devagentic-docs")


@pytest.fixture
def plugin_pkg(tmp_path, monkeypatch):
    """Load the devagentic-docs plugin modules as a synthetic
    package so relative imports resolve."""
    pkg_name = "_devagentic_docs_under_test"
    spec = importlib.util.spec_from_file_location(
        pkg_name, PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = pkg

    def _load(name):
        sub_spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.{name}", PLUGIN_DIR / f"{name}.py")
        mod = importlib.util.module_from_spec(sub_spec)
        sys.modules[f"{pkg_name}.{name}"] = mod
        sub_spec.loader.exec_module(mod)
        return mod

    client = _load("client")
    commands = _load("commands")

    assert spec.loader is not None
    spec.loader.exec_module(pkg)

    from types import SimpleNamespace
    return SimpleNamespace(pkg=pkg, client=client, commands=commands)


# ─── Manifest ───────────────────────────────────────────────

def test_manifest_parses_and_declares_expected_fields():
    manifest = yaml.safe_load(
        (PLUGIN_DIR / "plugin.yaml").read_text())
    assert manifest["name"] == "devagentic-docs"
    assert "version" in manifest
    assert "description" in manifest
    assert manifest.get("kind") == "standalone"
    # MVP doesn't register a pre_llm_call hook.
    assert not manifest.get("hooks")


# ─── Base URL normalization ─────────────────────────────────

def test_base_url_strips_v1_for_graphql(plugin_pkg, monkeypatch):
    """The /v1 suffix in DEVAGENTIC_BASE_URL is stripped because
    GraphQL lives at <root>/graphql, not <root>/v1/graphql."""
    monkeypatch.setenv("DEVAGENTIC_BASE_URL", "http://devbox:6070/v1")
    assert plugin_pkg.client._base_url() == "http://devbox:6070"


def test_base_url_passthrough_when_no_v1(plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_BASE_URL", "http://devbox:6070")
    assert plugin_pkg.client._base_url() == "http://devbox:6070"


# ─── Client failure-loudness (mirrors canvas #15) ───────────

def test_last_error_unresolved_user_id(plugin_pkg, monkeypatch):
    monkeypatch.delenv("DEVAGENTIC_USER_ID", raising=False)
    fake = type("F", (), {"get_active_profile_name": staticmethod(
        lambda: None)})()
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", fake)
    assert plugin_pkg.client.search_docs("anything") is None
    assert "DEVAGENTIC_USER_ID" in (
        plugin_pkg.client.last_error_text() or "")


def test_last_error_auth_failed(plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")

    def _raise(*a, **k):
        raise urllib.error.HTTPError(
            "http://x/graphql", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(plugin_pkg.client.urllib.request,
                        "urlopen", _raise)
    assert plugin_pkg.client.search_docs("anything") is None
    err = plugin_pkg.client.last_error_text() or ""
    assert "authentication failed" in err
    assert "DEVAGENTIC_API_KEY" in err


def test_last_error_unreachable(plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")

    def _raise(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(plugin_pkg.client.urllib.request,
                        "urlopen", _raise)
    assert plugin_pkg.client.write_doc("body") is None
    err = plugin_pkg.client.last_error_text() or ""
    assert "unreachable at" in err


def test_last_error_graphql_errors(plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {"errors": [{"message": "schema mismatch"}]}).encode("utf-8")

    monkeypatch.setattr(plugin_pkg.client.urllib.request,
                        "urlopen", lambda *a, **k: _Resp())
    assert plugin_pkg.client.search_docs("x") is None
    assert "schema mismatch" in (plugin_pkg.client.last_error_text() or "")


# ─── search_docs ────────────────────────────────────────────

def test_search_docs_returns_parsed_hits(plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    hits = [{"id": "doc-1", "content": "hello", "tags": ["a"],
             "score": 0.9}]
    monkeypatch.setattr(
        plugin_pkg.client, "_post_graphql",
        lambda q, v, **k: {"searchDocs": hits})
    out = plugin_pkg.client.search_docs("hi", limit=5, tag="a")
    assert out == hits


def test_search_docs_empty_query_short_circuits(plugin_pkg):
    assert plugin_pkg.client.search_docs("") is None


# ─── write_doc ──────────────────────────────────────────────

def test_write_doc_returns_id(plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    monkeypatch.setattr(
        plugin_pkg.client, "_post_graphql",
        lambda q, v, **k: {"writeDoc": {"id": "doc-abc"}})
    out = plugin_pkg.client.write_doc("body", tags=["x"])
    assert out == {"id": "doc-abc"}


def test_write_doc_empty_body_short_circuits(plugin_pkg):
    assert plugin_pkg.client.write_doc("") is None


# ─── /doc search command ────────────────────────────────────

def test_handle_search_usage_when_empty(plugin_pkg):
    out = plugin_pkg.commands._handle_search("")
    assert "Usage:" in out


def test_handle_search_renders_hits(plugin_pkg, monkeypatch):
    monkeypatch.setattr(
        plugin_pkg.client, "search_docs",
        lambda **k: [{"id": "doc-1", "content": "line 1\nline 2",
                      "score": 0.8},
                     {"id": "doc-2", "content": "another", "score": 0.5}])
    out = plugin_pkg.commands._handle_search("hello")
    assert "doc-1" in out and "doc-2" in out
    assert "Top 2" in out
    # First line only of multi-line content is shown.
    assert "line 1" in out and "line 2" not in out


def test_handle_search_appends_failure_detail(plugin_pkg, monkeypatch):
    monkeypatch.setattr(plugin_pkg.client, "search_docs",
                        lambda **k: None)
    monkeypatch.setattr(plugin_pkg.client, "last_error_text",
                        lambda: "auth failed")
    out = plugin_pkg.commands._handle_search("hello")
    assert "Reason: auth failed" in out


def test_handle_search_no_hits(plugin_pkg, monkeypatch):
    monkeypatch.setattr(plugin_pkg.client, "search_docs",
                        lambda **k: [])
    out = plugin_pkg.commands._handle_search("hello --tag k")
    assert "No docs matched" in out
    assert "tag=`k`" in out


def test_parse_search_args_extracts_flags(plugin_pkg):
    q, limit, tag = plugin_pkg.commands._parse_search_args(
        "find something --tag k:foo --limit 25")
    assert q == "find something"
    assert limit == 25
    assert tag == "k:foo"


def test_parse_search_args_limit_clamped(plugin_pkg):
    _, limit, _ = plugin_pkg.commands._parse_search_args(
        "x --limit 999999")
    assert limit == 100
    _, limit, _ = plugin_pkg.commands._parse_search_args(
        "x --limit 0")
    assert limit == 1


# ─── /doc write command ────────────────────────────────────

def test_handle_write_usage_when_empty(plugin_pkg):
    out = plugin_pkg.commands._handle_write("")
    assert "Usage:" in out


def test_handle_write_auto_tags_source(plugin_pkg, monkeypatch):
    captured: dict = {}

    def _stub(content, tags, source, **k):
        captured["content"] = content
        captured["tags"] = list(tags or [])
        captured["source"] = source
        return {"id": "doc-x"}

    monkeypatch.setattr(plugin_pkg.client, "write_doc", _stub)
    out = plugin_pkg.commands._handle_write(
        "hello world --tags k:test,user:duplex")
    assert "doc-x" in out
    assert "source:hermes-cli" in captured["tags"]
    assert "k:test" in captured["tags"]
    assert "user:duplex" in captured["tags"]
    assert captured["source"] == "hermes-cli"
    assert captured["content"] == "hello world"


def test_handle_write_appends_failure_detail(plugin_pkg, monkeypatch):
    monkeypatch.setattr(plugin_pkg.client, "write_doc",
                        lambda **k: None)
    monkeypatch.setattr(plugin_pkg.client, "last_error_text",
                        lambda: "unreachable at http://x/graphql")
    out = plugin_pkg.commands._handle_write("a body")
    assert "Reason:" in out
    assert "unreachable" in out


# ─── /doc show command ──────────────────────────────────────

def test_handle_show_usage_when_empty(plugin_pkg):
    out = plugin_pkg.commands._handle_show("")
    assert "Usage:" in out


def test_handle_show_renders_doc(plugin_pkg, monkeypatch):
    monkeypatch.setattr(
        plugin_pkg.client, "get_doc",
        lambda doc_id, **k: {"id": doc_id, "content": "body",
                              "tags": ["a", "b"]})
    out = plugin_pkg.commands._handle_show("doc-abc")
    assert "doc-abc" in out
    assert "body" in out
    assert "`a`" in out and "`b`" in out


# ─── dispatcher ────────────────────────────────────────────

def test_doc_command_dispatcher_usage(plugin_pkg):
    out = plugin_pkg.commands.doc_command("")
    assert "Usage:" in out


def test_doc_command_dispatcher_unknown_sub(plugin_pkg):
    out = plugin_pkg.commands.doc_command("nope hi")
    assert "Unknown" in out


def test_doc_command_dispatcher_routes_search(plugin_pkg, monkeypatch):
    monkeypatch.setattr(plugin_pkg.client, "search_docs",
                        lambda **k: [])
    out = plugin_pkg.commands.doc_command("search anything")
    assert "No docs matched" in out
