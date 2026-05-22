"""Tests for the canvas MCP tools in mcp_serve.py (issue #56).

The MCP tools are thin adapters around the devagentic-canvas plugin
client. These tests:

  1. Mock _resolve_canvas_client() to return a SimpleNamespace with
     the methods the tools call, captures requests, returns stub
     responses.
  2. Drive create_mcp_server() to register all tools.
  3. Pull the registered FunctionTool objects off the FastMCP
     instance and invoke them via their `.fn` attribute (FastMCP
     wraps the user function with metadata).
  4. Assert each tool routes to the right client method + parses
     responses correctly.

Skip the entire file when the `mcp` package isn't installed (the
tools are gated on FastMCP availability anyway).
"""
from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
from typing import Any

import pytest


# Skip everything in this file when MCP isn't available.
mcp_sdk = pytest.importorskip("mcp")


@pytest.fixture
def mcp_module(monkeypatch):
    """Import mcp_serve fresh, mock its event bridge, and patch the
    canvas-client resolver to return a controlled SimpleNamespace."""
    import mcp_serve as mod
    importlib.reload(mod)
    # Stub the EventBridge constructor so create_mcp_server doesn't
    # try to start real platform connections.
    monkeypatch.setattr(mod, "EventBridge",
                        lambda *a, **k: SimpleNamespace(
                            list_pending_approvals=lambda: [],
                            respond_to_approval=lambda *a, **k: {}))
    return mod


@pytest.fixture
def fake_client():
    """Build a stand-in for the canvas client module. Each method
    captures call args + returns a configurable response."""
    calls: list = []

    def _record(name, return_value):
        def _impl(*args, **kwargs):
            calls.append((name, args, kwargs))
            return return_value
        return _impl

    ns = SimpleNamespace(
        list_canvases=_record(
            "list_canvases",
            [{"id": "c1", "name": "first"},
             {"id": "c2", "name": "second"}]),
        get_canvas=_record(
            "get_canvas",
            {"canvas": {"name": "the canvas"},
             "nodes": [{"id": "n1", "node_type": "doc"}],
             "edges": []}),
        add_node=_record(
            "add_node",
            {"id": "node-NEW", "node_type": "decision"}),
        move_node=_record(
            "move_node",
            {"id": "n1", "position": {"x": 5.0, "y": 7.0}}),
        update_node=_record(
            "update_node",
            {"id": "n1", "body": {"content": "updated"}}),
        delete_node=_record(
            "delete_node",
            {"deleted": True, "id": "n1"}),
        link_nodes=_record(
            "link_nodes",
            {"id": "edge-NEW", "source": "n1", "target": "n2"}),
        delete_edge=_record(
            "delete_edge",
            {"deleted": True, "id": "e1"}),
        search_canvas=_record(
            "search_canvas",
            [{"id": "n1", "node_type": "doc"}]),
    )
    ns._calls = calls
    return ns


def _get_tool(mcp_server, name: str):
    """Pull the registered tool callable off a FastMCP server.

    FastMCP wraps registered functions in a Tool object whose `.fn`
    attribute is the user-supplied callable. We invoke `.fn`
    directly so the tests don't depend on the MCP transport
    machinery."""
    tools = getattr(mcp_server, "_tool_manager", None)
    if tools is None or not hasattr(tools, "list_tools"):
        # Fallback for older FastMCP layouts that expose tools
        # via `_tools` dict directly.
        registry = getattr(mcp_server, "_tools", {}) or {}
        return registry.get(name).fn
    for tool in tools.list_tools():
        if tool.name == name:
            return tool.fn
    raise AssertionError(f"MCP tool {name!r} not registered")


def _call_tool(mcp_server, name: str, **kwargs) -> Any:
    fn = _get_tool(mcp_server, name)
    return fn(**kwargs)


def _decode(s: str) -> Any:
    return json.loads(s)


# ─── Tool registration ──────────────────────────────────────

def test_canvas_tools_register(mcp_module, fake_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_canvas_client",
                        lambda: fake_client)
    server = mcp_module.create_mcp_server()
    # Each tool registers a callable under the expected name.
    for name in ("canvas_list", "canvas_open", "canvas_add_node",
                 "canvas_move_node", "canvas_update_node",
                 "canvas_delete_node", "canvas_link_nodes",
                 "canvas_delete_edge", "canvas_search"):
        _get_tool(server, name)  # raises AssertionError if missing


# ─── canvas_list ────────────────────────────────────────────

def test_canvas_list_returns_count_and_canvases(
        mcp_module, fake_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_canvas_client",
                        lambda: fake_client)
    server = mcp_module.create_mcp_server()
    raw = _call_tool(server, "canvas_list")
    out = _decode(raw)
    assert out["count"] == 2
    assert out["canvases"][0]["id"] == "c1"


def test_canvas_list_plugin_missing_returns_error(
        mcp_module, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_canvas_client",
                        lambda: None)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(server, "canvas_list"))
    assert "error" in out
    assert "plugin not available" in out["error"]


def test_canvas_list_devagentic_unreachable_returns_error(
        mcp_module, fake_client, monkeypatch):
    fake_client.list_canvases = lambda: None
    monkeypatch.setattr(mcp_module, "_resolve_canvas_client",
                        lambda: fake_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(server, "canvas_list"))
    assert "error" in out


# ─── canvas_open ────────────────────────────────────────────

def test_canvas_open_returns_state(
        mcp_module, fake_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_canvas_client",
                        lambda: fake_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(server, "canvas_open", canvas_id="c1"))
    assert out["canvas"]["name"] == "the canvas"
    assert len(out["nodes"]) == 1


def test_canvas_open_requires_id(
        mcp_module, fake_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_canvas_client",
                        lambda: fake_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(server, "canvas_open", canvas_id=""))
    assert "error" in out


# ─── canvas_add_node ────────────────────────────────────────

def test_canvas_add_node_routes_args(
        mcp_module, fake_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_canvas_client",
                        lambda: fake_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(
        server, "canvas_add_node",
        canvas_id="c1", node_type="decision",
        position_x=10, position_y=20))
    assert out["id"] == "node-NEW"
    # Inspect the captured call to the fake client.
    name, args, kwargs = fake_client._calls[-1]
    assert name == "add_node"
    assert args[0] == "c1"
    assert args[1] == "decision"
    assert kwargs["position"] == {"x": 10.0, "y": 20.0}


def test_canvas_add_node_optional_position(
        mcp_module, fake_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_canvas_client",
                        lambda: fake_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(
        server, "canvas_add_node",
        canvas_id="c1", node_type="doc"))
    assert out["id"] == "node-NEW"
    _, args, kwargs = fake_client._calls[-1]
    assert kwargs["position"] is None


def test_canvas_add_node_requires_args(
        mcp_module, fake_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_canvas_client",
                        lambda: fake_client)
    server = mcp_module.create_mcp_server()
    err = _decode(_call_tool(
        server, "canvas_add_node",
        canvas_id="", node_type="doc"))
    assert "error" in err


# ─── canvas_move_node + update + delete ─────────────────────

def test_canvas_move_node(mcp_module, fake_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_canvas_client",
                        lambda: fake_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(
        server, "canvas_move_node",
        canvas_id="c1", node_id="n1", x=5, y=7))
    assert out["position"] == {"x": 5.0, "y": 7.0}


def test_canvas_update_node_parses_fields_json(
        mcp_module, fake_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_canvas_client",
                        lambda: fake_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(
        server, "canvas_update_node",
        canvas_id="c1", node_id="n1",
        fields_json='{"body": {"content": "updated"}}'))
    assert out["body"]["content"] == "updated"
    _, args, _ = fake_client._calls[-1]
    assert args[2] == {"body": {"content": "updated"}}


def test_canvas_update_node_rejects_invalid_json(
        mcp_module, fake_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_canvas_client",
                        lambda: fake_client)
    server = mcp_module.create_mcp_server()
    err = _decode(_call_tool(
        server, "canvas_update_node",
        canvas_id="c1", node_id="n1",
        fields_json="not-json{{"))
    assert "error" in err
    assert "not valid JSON" in err["error"]


def test_canvas_update_node_rejects_non_object(
        mcp_module, fake_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_canvas_client",
                        lambda: fake_client)
    server = mcp_module.create_mcp_server()
    err = _decode(_call_tool(
        server, "canvas_update_node",
        canvas_id="c1", node_id="n1",
        fields_json='"a string"'))
    assert "error" in err


def test_canvas_delete_node(mcp_module, fake_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_canvas_client",
                        lambda: fake_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(
        server, "canvas_delete_node",
        canvas_id="c1", node_id="n1"))
    assert out["deleted"] is True


# ─── canvas_link_nodes + canvas_delete_edge ─────────────────

def test_canvas_link_nodes(mcp_module, fake_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_canvas_client",
                        lambda: fake_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(
        server, "canvas_link_nodes",
        canvas_id="c1", source_id="n1", target_id="n2",
        edge_type="depends-on"))
    assert out["id"] == "edge-NEW"
    _, args, kwargs = fake_client._calls[-1]
    assert args == ("c1", "n1", "n2")
    assert kwargs["edge_type"] == "depends-on"


def test_canvas_delete_edge(mcp_module, fake_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_canvas_client",
                        lambda: fake_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(
        server, "canvas_delete_edge",
        canvas_id="c1", edge_id="e1"))
    assert out["deleted"] is True


# ─── canvas_search ──────────────────────────────────────────

def test_canvas_search(mcp_module, fake_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_canvas_client",
                        lambda: fake_client)
    server = mcp_module.create_mcp_server()
    out = _decode(_call_tool(
        server, "canvas_search",
        canvas_id="c1", query="doc"))
    assert out["count"] == 1
    assert out["matches"][0]["id"] == "n1"


def test_canvas_search_requires_query(
        mcp_module, fake_client, monkeypatch):
    monkeypatch.setattr(mcp_module, "_resolve_canvas_client",
                        lambda: fake_client)
    server = mcp_module.create_mcp_server()
    err = _decode(_call_tool(
        server, "canvas_search", canvas_id="c1", query=""))
    assert "error" in err
