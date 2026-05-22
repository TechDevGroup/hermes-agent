"""Tests for the devagentic-docs MCP tools in mcp_serve.py (#24).

Mirrors test_mcp_canvas.py's structure — stub _resolve_docs_client
with a SimpleNamespace that records calls and returns canned
responses, drive create_mcp_server(), pull tools off the FastMCP
instance via their `.fn` attribute.

Skipped when the `mcp` package isn't installed.
"""
from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
from typing import Any

import pytest


mcp_sdk = pytest.importorskip("mcp")


@pytest.fixture
def mcp_module(monkeypatch):
    import mcp_serve as mod
    importlib.reload(mod)
    monkeypatch.setattr(
        mod, "EventBridge",
        lambda *a, **k: SimpleNamespace(
            list_pending_approvals=lambda: [],
            respond_to_approval=lambda *a, **k: {}))
    return mod


@pytest.fixture
def fake_docs_client():
    """SimpleNamespace stand-in for plugins/devagentic-docs/client.py.
    Each method records the call args and returns a canned response.
    `last_error_text` is implemented so the _reason() helper paths
    are exercised."""
    calls: list = []
    last_error: dict[str, str | None] = {"value": None}

    def _record(name, return_value):
        def _impl(*args, **kwargs):
            calls.append((name, args, kwargs))
            return return_value
        return _impl

    ns = SimpleNamespace(
        search_docs=_record(
            "search_docs",
            [{"id": "doc-1", "content": "hello", "tags": ["a"],
              "source": "s", "ts": "now"}]),
        write_doc=_record(
            "write_doc",
            {"id": "doc-NEW"}),
        get_doc=_record(
            "get_doc",
            {"id": "doc-1", "content": "body", "tags": []}),
        fork_context=_record(
            "fork_context",
            {"id": "ctx-1", "tags": [], "annotations": []}),
        decorate_context=_record(
            "decorate_context",
            {"id": "ctx-1", "annotations": [
                {"key": "pinned-doc", "value": "doc-1",
                 "weight": 1.0}]}),
        get_context=_record(
            "get_context",
            {"id": "ctx-1", "tags": ["t"], "annotations": []}),
        render_context=_record(
            "render_context",
            "rendered text"),
        last_error_text=lambda: last_error["value"],
    )
    ns._calls = calls
    ns._set_error = lambda msg: last_error.__setitem__("value", msg)
    return ns


def _get_tool(mcp_server, name: str):
    tools = getattr(mcp_server, "_tool_manager", None)
    if tools is None or not hasattr(tools, "list_tools"):
        registry = getattr(mcp_server, "_tools", {}) or {}
        return registry.get(name).fn
    for tool in tools.list_tools():
        if tool.name == name:
            return tool.fn
    raise AssertionError(f"MCP tool {name!r} not registered")


def _call_tool(mcp_server, name: str, **kwargs) -> Any:
    return _get_tool(mcp_server, name)(**kwargs)


def _decode(s: str) -> Any:
    return json.loads(s)


# ─── Tool registration ──────────────────────────────────────

def test_docs_tools_register(mcp_module, fake_docs_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_docs_client",
                        lambda: fake_docs_client)
    server = mcp_module.create_mcp_server()
    for name in ("doc_search", "doc_write", "doc_show",
                 "fork_open", "fork_decorate", "fork_get",
                 "fork_render"):
        _get_tool(server, name)


def test_docs_tools_plugin_missing_returns_error(
        mcp_module, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_docs_client",
                        lambda: None)
    server = mcp_module.create_mcp_server()
    for name in ("doc_search", "doc_write", "doc_show",
                 "fork_open", "fork_decorate", "fork_get",
                 "fork_render"):
        # Each tool has different required args; use minimal values.
        kwargs: dict[str, Any] = {}
        if name == "doc_search":
            kwargs = {"query": "hi"}
        elif name == "doc_write":
            kwargs = {"content": "body"}
        elif name == "doc_show":
            kwargs = {"doc_id": "x"}
        elif name == "fork_open":
            kwargs = {"parent_id": "x"}
        elif name == "fork_decorate":
            kwargs = {"ctx_id": "x", "key": "k", "value": "v"}
        elif name == "fork_get":
            kwargs = {"ctx_id": "x"}
        elif name == "fork_render":
            kwargs = {"ctx_id": "x"}
        out = _decode(_call_tool(server, name, **kwargs))
        assert "error" in out
        assert "docs plugin not available" in out["error"]


# ─── doc_search ─────────────────────────────────────────────

def test_doc_search_routes_args_and_returns_count(
        mcp_module, fake_docs_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_docs_client",
                        lambda: fake_docs_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(server, "doc_search",
                             query="hello", limit=5, tag="k:foo"))
    assert out["count"] == 1
    assert out["hits"][0]["id"] == "doc-1"
    name, _, kwargs = fake_docs_client._calls[-1]
    assert name == "search_docs"
    assert kwargs == {"query": "hello", "limit": 5, "tag": "k:foo"}


def test_doc_search_requires_query_or_tag(
        mcp_module, fake_docs_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_docs_client",
                        lambda: fake_docs_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(server, "doc_search"))
    assert "error" in out


def test_doc_search_limit_clamped(
        mcp_module, fake_docs_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_docs_client",
                        lambda: fake_docs_client)
    server = mcp_module.create_mcp_server()
    _call_tool(server, "doc_search", query="hi", limit=99999)
    _, _, kwargs = fake_docs_client._calls[-1]
    assert kwargs["limit"] == 100
    _call_tool(server, "doc_search", query="hi", limit=0)
    _, _, kwargs = fake_docs_client._calls[-1]
    assert kwargs["limit"] == 1


def test_doc_search_surfaces_last_error_reason(
        mcp_module, fake_docs_client, monkeypatch):
    fake_docs_client.search_docs = lambda **k: None
    fake_docs_client._set_error(
        "authentication failed — set DEVAGENTIC_API_KEY")
    monkeypatch.setattr(mcp_module, "_resolve_docs_client",
                        lambda: fake_docs_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(server, "doc_search", query="hi"))
    assert "error" in out
    assert "authentication failed" in out["error"]
    assert "DEVAGENTIC_API_KEY" in out["error"]


# ─── doc_write ──────────────────────────────────────────────

def test_doc_write_passes_through_args(
        mcp_module, fake_docs_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_docs_client",
                        lambda: fake_docs_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(
        server, "doc_write",
        content="hello", tags=["k:test"], source="claude-code"))
    assert out["id"] == "doc-NEW"
    _, _, kwargs = fake_docs_client._calls[-1]
    assert kwargs == {"content": "hello", "tags": ["k:test"],
                      "source": "claude-code"}


def test_doc_write_requires_content(
        mcp_module, fake_docs_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_docs_client",
                        lambda: fake_docs_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(server, "doc_write", content=""))
    assert "error" in out


# ─── doc_show ───────────────────────────────────────────────

def test_doc_show_returns_doc(
        mcp_module, fake_docs_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_docs_client",
                        lambda: fake_docs_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(server, "doc_show", doc_id="doc-1"))
    assert out["id"] == "doc-1"


# ─── fork_open ──────────────────────────────────────────────

def test_fork_open_auto_pins_parent_and_threads_goal(
        mcp_module, fake_docs_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_docs_client",
                        lambda: fake_docs_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(
        server, "fork_open",
        parent_id="doc-parent", goal="investigate",
        tags=["extra:t"]))
    assert out["id"] == "ctx-1"
    _, _, kwargs = fake_docs_client._calls[-1]
    assert kwargs["parent_id"] == "doc-parent"
    # Tags get a source:hermes-mcp marker so MCP-authored forks are
    # distinguishable from CLI-authored ones (source:hermes-cli).
    assert "source:hermes-mcp" in kwargs["tags"]
    assert "extra:t" in kwargs["tags"]
    annot_keys = [a["key"] for a in kwargs["annotations"]]
    assert "goal" in annot_keys
    assert "pinned-doc" in annot_keys


def test_fork_open_without_goal_still_pins_parent(
        mcp_module, fake_docs_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_docs_client",
                        lambda: fake_docs_client)
    server = mcp_module.create_mcp_server()
    _call_tool(server, "fork_open", parent_id="doc-parent")
    _, _, kwargs = fake_docs_client._calls[-1]
    annot_keys = [a["key"] for a in kwargs["annotations"]]
    assert annot_keys == ["pinned-doc"]


def test_fork_open_requires_parent_id(
        mcp_module, fake_docs_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_docs_client",
                        lambda: fake_docs_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(server, "fork_open", parent_id=""))
    assert "error" in out


# ─── fork_decorate ──────────────────────────────────────────

def test_fork_decorate_passes_through_args(
        mcp_module, fake_docs_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_docs_client",
                        lambda: fake_docs_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(
        server, "fork_decorate",
        ctx_id="ctx-1", key="pinned-doc", value="doc-7",
        weight=0.5))
    assert out["id"] == "ctx-1"
    _, _, kwargs = fake_docs_client._calls[-1]
    assert kwargs == {"ctx_id": "ctx-1", "key": "pinned-doc",
                      "value": "doc-7", "weight": 0.5}


def test_fork_decorate_requires_ctx_and_key(
        mcp_module, fake_docs_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_docs_client",
                        lambda: fake_docs_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(server, "fork_decorate", ctx_id="",
                              key="", value="v"))
    assert "error" in out


# ─── fork_get / fork_render ─────────────────────────────────

def test_fork_get_returns_context(
        mcp_module, fake_docs_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_docs_client",
                        lambda: fake_docs_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(server, "fork_get", ctx_id="ctx-1"))
    assert out["id"] == "ctx-1"


def test_fork_render_wraps_in_envelope(
        mcp_module, fake_docs_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_docs_client",
                        lambda: fake_docs_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(server, "fork_render", ctx_id="ctx-1"))
    assert out == {"ctx_id": "ctx-1", "rendered": "rendered text"}


def test_fork_render_surfaces_404_reason(
        mcp_module, fake_docs_client, monkeypatch):
    """When devagentic doesn't expose /graphql (the #21 gap), the
    underlying client returns None and last_error_text reads
    'not found at <url>/graphql'. The MCP tool must surface this
    Reason so federated agents see the same actionable hint."""
    fake_docs_client.render_context = lambda *a, **k: None
    fake_docs_client._set_error(
        "not found at http://devbox:6070/graphql")
    monkeypatch.setattr(mcp_module, "_resolve_docs_client",
                        lambda: fake_docs_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(server, "fork_render", ctx_id="ctx-1"))
    assert "error" in out
    assert "not found at" in out["error"]


# ─── _resolve_docs_client real-import smoke ─────────────────

def test_resolve_docs_client_loads_real_module(mcp_module):
    """The file-path importer should successfully load the
    devagentic-docs plugin's client.py from the repo layout."""
    mod = mcp_module._resolve_docs_client()
    assert mod is not None
    assert callable(getattr(mod, "search_docs", None))
    assert callable(getattr(mod, "write_doc", None))
    assert callable(getattr(mod, "fork_context", None))
    assert callable(getattr(mod, "render_context", None))
