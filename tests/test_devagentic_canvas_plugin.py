"""Tests for the devagentic-canvas plugin (hermes issue #55).

Covers:

  Manifest parse:
    * plugin.yaml is valid YAML, declares the expected fields.

  Client (HTTP shape, with network mocked):
    * list_canvases / get_canvas / create_canvas issue the right
      requests + returns parsed dicts on success.
    * Network failure / no user_id → all return None.

  Active-canvas marker:
    * set/get/clear round-trip via a tmp HERMES_HOME.
    * get_active_canvas_id returns None when file missing / empty.

  Slash command dispatch:
    * `/canvas list` with mocked canvases prints them + flags the
      active one.
    * `/canvas open <id>` writes the marker; subsequent
      `get_active_canvas_id()` returns it.
    * `/canvas close` clears the marker.
    * `/canvas show` summarizes the active canvas (with mocked
      get_canvas).
    * `/canvas new <name>` calls create_canvas + returns the id.
    * Unknown subcommands return a usage error.

  Preamble:
    * `on_pre_llm_call` returns None when no marker.
    * Returns `{"context": "..."}` when canvas is active +
      get_canvas returns a dict.
    * Caps nodes / edges in the rendered output.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml


PLUGIN_DIR = (Path(__file__).resolve().parents[1]
              / "plugins" / "devagentic-canvas")


@pytest.fixture
def plugin_pkg(tmp_path, monkeypatch):
    """Load the devagentic-canvas plugin modules as a synthetic
    package so the relative imports (`from . import canvas_client`)
    resolve. Returns a SimpleNamespace with `commands`, `client`,
    `preamble`, `register_module` attrs."""
    pkg_name = "_devagentic_canvas_under_test"
    # Build a synthetic package rooted at PLUGIN_DIR.
    spec = importlib.util.spec_from_file_location(
        pkg_name, PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = pkg

    # Load submodules under the synthetic package name.
    def _load(name):
        sub_spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.{name}", PLUGIN_DIR / f"{name}.py")
        mod = importlib.util.module_from_spec(sub_spec)
        sys.modules[f"{pkg_name}.{name}"] = mod
        sub_spec.loader.exec_module(mod)
        return mod

    client = _load("client")
    commands = _load("commands")
    preamble = _load("preamble")

    # Exec the package's __init__.py last (it imports the submodules
    # via relative imports — they're already in sys.modules under
    # the synthetic name).
    assert spec.loader is not None
    spec.loader.exec_module(pkg)

    # Point HERMES_HOME at tmp_path so marker writes don't leak.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from types import SimpleNamespace
    return SimpleNamespace(
        pkg=pkg, client=client, commands=commands,
        preamble=preamble,
    )


# ─── Manifest ───────────────────────────────────────────────

def test_manifest_parses_and_declares_expected_fields():
    manifest = yaml.safe_load(
        (PLUGIN_DIR / "plugin.yaml").read_text())
    assert manifest["name"] == "devagentic-canvas"
    assert "version" in manifest
    assert "description" in manifest
    assert manifest.get("kind") == "standalone"
    assert "pre_llm_call" in (manifest.get("hooks") or [])


# ─── Client ─────────────────────────────────────────────────

def test_base_url_appends_v1_when_missing(plugin_pkg, monkeypatch):
    """Operators may set DEVAGENTIC_BASE_URL=$DUPLEX_SERVICE_URL
    (host[:port] only). Plugin must auto-append /v1 so it doesn't
    silently 404 against the real surface (see #13)."""
    monkeypatch.setenv("DEVAGENTIC_BASE_URL", "http://devbox:6070")
    assert plugin_pkg.client._base_url() == "http://devbox:6070/v1"


def test_base_url_respects_explicit_v1(plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_BASE_URL", "http://devbox:6070/v1")
    assert plugin_pkg.client._base_url() == "http://devbox:6070/v1"


def test_base_url_respects_explicit_version(plugin_pkg, monkeypatch):
    """An explicit /vN must not be rewritten to /v1."""
    monkeypatch.setenv("DEVAGENTIC_BASE_URL", "http://devbox:6070/v2")
    assert plugin_pkg.client._base_url() == "http://devbox:6070/v2"


def test_base_url_strips_trailing_slash_before_append(
        plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_BASE_URL", "http://devbox:6070/")
    assert plugin_pkg.client._base_url() == "http://devbox:6070/v1"


def test_client_list_canvases_returns_parsed_list(
        plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    monkeypatch.setattr(
        plugin_pkg.client, "_request",
        lambda method, path, body=None, **k:
            {"canvases": [{"id": "c1", "name": "n1"},
                          {"id": "c2", "name": "n2"}]})
    out = plugin_pkg.client.list_canvases()
    assert out == [{"id": "c1", "name": "n1"},
                   {"id": "c2", "name": "n2"}]


def test_client_list_canvases_returns_none_on_failure(
        plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    monkeypatch.setattr(plugin_pkg.client, "_request",
                        lambda *a, **k: None)
    assert plugin_pkg.client.list_canvases() is None


def test_client_get_canvas_short_circuits_on_empty_id(
        plugin_pkg):
    assert plugin_pkg.client.get_canvas("") is None


def test_client_no_user_id_returns_none(plugin_pkg, monkeypatch):
    monkeypatch.delenv("DEVAGENTIC_USER_ID", raising=False)
    fake = type(sys)("hermes_cli.profiles")
    fake.get_active_profile_name = lambda: ""
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", fake)
    # _request bails before issuing any HTTP when there's no user_id.
    assert plugin_pkg.client._request("GET", "es") is None


# ─── Client mutations (issue #56 MCP surface) ───────────────

def test_client_add_node_posts_to_nodes_route(plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    captured: dict = {}

    def _capture(method, path, body=None, **k):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {"id": "node-NEW", "node_type": body.get("node_type")}
    monkeypatch.setattr(plugin_pkg.client, "_request", _capture)
    node = plugin_pkg.client.add_node(
        "canvas-123", "decision",
        position={"x": 12.0, "y": 34.0})
    assert node["id"] == "node-NEW"
    assert captured["method"] == "POST"
    assert captured["path"] == "/canvas-123/nodes"
    assert captured["body"]["node_type"] == "decision"
    assert captured["body"]["position"] == {"x": 12.0, "y": 34.0}


def test_client_add_node_requires_canvas_and_type(plugin_pkg):
    assert plugin_pkg.client.add_node("", "decision") is None
    assert plugin_pkg.client.add_node("canvas-x", "") is None


def test_client_update_node_patches_with_fields(plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    captured: dict = {}

    def _capture(method, path, body=None, **k):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {"id": "n1", "node_type": "decision"}
    monkeypatch.setattr(plugin_pkg.client, "_request", _capture)
    out = plugin_pkg.client.update_node(
        "c1", "n1", {"node_type": "decision"})
    assert out["node_type"] == "decision"
    assert captured["method"] == "PATCH"
    assert captured["path"] == "/c1/nodes/n1"
    assert captured["body"] == {"node_type": "decision"}


def test_client_move_node_wraps_update(plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    captured: dict = {}

    def _capture(method, path, body=None, **k):
        captured["body"] = body
        return {"id": "n1", "position": body["position"]}
    monkeypatch.setattr(plugin_pkg.client, "_request", _capture)
    out = plugin_pkg.client.move_node("c1", "n1", 5, 7)
    assert out["position"] == {"x": 5.0, "y": 7.0}
    assert captured["body"]["position"] == {"x": 5.0, "y": 7.0}


def test_client_delete_node(plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    monkeypatch.setattr(
        plugin_pkg.client, "_request",
        lambda method, path, body=None, **k:
            {"deleted": True, "method": method, "path": path})
    out = plugin_pkg.client.delete_node("c1", "n1")
    assert out["deleted"] is True
    assert out["method"] == "DELETE"
    assert out["path"] == "/c1/nodes/n1"


def test_client_link_nodes(plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    captured: dict = {}

    def _capture(method, path, body=None, **k):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {"id": "edge-NEW", **body}
    monkeypatch.setattr(plugin_pkg.client, "_request", _capture)
    edge = plugin_pkg.client.link_nodes(
        "c1", "src", "dst", edge_type="depends-on")
    assert edge["id"] == "edge-NEW"
    assert captured["method"] == "POST"
    assert captured["path"] == "/c1/edges"
    assert captured["body"] == {
        "source": "src", "target": "dst",
        "edge_type": "depends-on",
    }


def test_client_delete_edge(plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    monkeypatch.setattr(
        plugin_pkg.client, "_request",
        lambda method, path, body=None, **k:
            {"deleted": True, "path": path})
    out = plugin_pkg.client.delete_edge("c1", "e1")
    assert out["deleted"] is True
    assert out["path"] == "/c1/edges/e1"


def test_client_search_filters_nodes(plugin_pkg, monkeypatch):
    monkeypatch.setattr(
        plugin_pkg.client, "get_canvas",
        lambda cid, **k: {
            "nodes": [
                {"id": "n1", "node_type": "doc",
                 "body": "refactor the auth flow"},
                {"id": "n2", "node_type": "doc",
                 "body": "unrelated note about deploys"},
                {"id": "n3", "node_type": "decision",
                 "name": "auth-rule"},
            ],
        })
    matches = plugin_pkg.client.search_canvas("c1", "auth")
    ids = sorted(m["id"] for m in matches)
    assert ids == ["n1", "n3"]
    # Empty query / no canvas id → None.
    assert plugin_pkg.client.search_canvas("c1", "") is None
    assert plugin_pkg.client.search_canvas("", "x") is None


def test_client_search_returns_none_on_canvas_miss(
        plugin_pkg, monkeypatch):
    monkeypatch.setattr(plugin_pkg.client, "get_canvas",
                        lambda cid, **k: None)
    assert plugin_pkg.client.search_canvas("c1", "x") is None


# ─── Active-canvas marker ───────────────────────────────────

def test_active_canvas_marker_roundtrip(plugin_pkg):
    assert plugin_pkg.commands.get_active_canvas_id() is None
    assert plugin_pkg.commands.set_active_canvas_id("canvas-abc")
    assert plugin_pkg.commands.get_active_canvas_id() == "canvas-abc"
    assert plugin_pkg.commands.clear_active_canvas()
    assert plugin_pkg.commands.get_active_canvas_id() is None


def test_active_canvas_marker_handles_empty_file(plugin_pkg):
    p = plugin_pkg.commands._active_canvas_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("   \n")
    assert plugin_pkg.commands.get_active_canvas_id() is None


# ─── Slash command dispatch ─────────────────────────────────

def test_canvas_list_command(plugin_pkg, monkeypatch):
    monkeypatch.setattr(plugin_pkg.client, "list_canvases",
                        lambda **k: [
                            {"id": "c1", "name": "first",
                             "description": "desc1"},
                            {"id": "c2", "name": "second"},
                        ])
    out = plugin_pkg.commands.canvas_command("list")
    assert "first" in out
    assert "second" in out
    assert "c1" in out and "c2" in out


def test_canvas_list_marks_active(plugin_pkg, monkeypatch):
    plugin_pkg.commands.set_active_canvas_id("c2")
    monkeypatch.setattr(plugin_pkg.client, "list_canvases",
                        lambda **k: [
                            {"id": "c1", "name": "first"},
                            {"id": "c2", "name": "second"},
                        ])
    out = plugin_pkg.commands.canvas_command("list")
    assert "active" in out


def test_canvas_list_empty(plugin_pkg, monkeypatch):
    monkeypatch.setattr(plugin_pkg.client, "list_canvases",
                        lambda **k: [])
    out = plugin_pkg.commands.canvas_command("list")
    assert "No canvases" in out


def test_canvas_open_writes_marker_and_show_summarizes(
        plugin_pkg, monkeypatch):
    monkeypatch.setattr(
        plugin_pkg.client, "get_canvas",
        lambda cid, **k: {
            "canvas": {"name": "the canvas",
                       "description": "an inline desc"},
            "nodes": [{"id": "n1", "node_type": "doc"},
                      {"id": "n2", "node_type": "doc"}],
            "edges": [],
        })
    out_open = plugin_pkg.commands.canvas_command("open canvas-id-1")
    assert "Opened canvas" in out_open
    assert plugin_pkg.commands.get_active_canvas_id() == "canvas-id-1"
    out_show = plugin_pkg.commands.canvas_command("show")
    assert "the canvas" in out_show
    assert "2 nodes" in out_show


def test_canvas_open_requires_id(plugin_pkg):
    out = plugin_pkg.commands.canvas_command("open")
    assert "Usage" in out


def test_canvas_open_unreachable(plugin_pkg, monkeypatch):
    monkeypatch.setattr(plugin_pkg.client, "get_canvas",
                        lambda cid, **k: None)
    out = plugin_pkg.commands.canvas_command("open canvas-X")
    assert "not found" in out or "unreachable" in out


def test_canvas_close_clears_marker(plugin_pkg):
    plugin_pkg.commands.set_active_canvas_id("canvas-Z")
    out = plugin_pkg.commands.canvas_command("close")
    assert "Closed canvas" in out
    assert plugin_pkg.commands.get_active_canvas_id() is None


def test_canvas_close_when_none_active(plugin_pkg):
    out = plugin_pkg.commands.canvas_command("close")
    assert "No canvas was active" in out


def test_canvas_new_creates(plugin_pkg, monkeypatch):
    monkeypatch.setattr(
        plugin_pkg.client, "create_canvas",
        lambda name, **k: {"id": "canvas-new-1", "name": name})
    out = plugin_pkg.commands.canvas_command("new design-doc")
    assert "Created canvas" in out
    assert "canvas-new-1" in out


def test_canvas_new_requires_name(plugin_pkg):
    out = plugin_pkg.commands.canvas_command("new")
    assert "Usage" in out


def test_canvas_unknown_subcommand(plugin_pkg):
    out = plugin_pkg.commands.canvas_command("explode")
    assert "Unknown" in out


def test_canvas_bare_falls_through_to_list(plugin_pkg, monkeypatch):
    monkeypatch.setattr(plugin_pkg.client, "list_canvases",
                        lambda **k: [])
    out = plugin_pkg.commands.canvas_command("")
    # bare /canvas → falls through to list behavior
    assert "No canvases" in out or "canvases" in out.lower()


# ─── Preamble ───────────────────────────────────────────────

def test_preamble_inert_when_no_marker(plugin_pkg):
    assert plugin_pkg.commands.get_active_canvas_id() is None
    result = plugin_pkg.preamble.on_pre_llm_call()
    assert result is None


def test_preamble_inert_when_devagentic_unreachable(
        plugin_pkg, monkeypatch):
    plugin_pkg.commands.set_active_canvas_id("canvas-down")
    monkeypatch.setattr(plugin_pkg.client, "get_canvas",
                        lambda cid, **k: None)
    assert plugin_pkg.preamble.on_pre_llm_call() is None


def test_preamble_injects_context_when_active(
        plugin_pkg, monkeypatch):
    plugin_pkg.commands.set_active_canvas_id("canvas-active-1")
    monkeypatch.setattr(
        plugin_pkg.client, "get_canvas",
        lambda cid, **k: {
            "canvas": {"name": "design", "description": "wip"},
            "nodes": [{"id": "n1", "node_type": "doc"},
                      {"id": "n2", "node_type": "doc"}],
            "edges": [{"source": "n1", "target": "n2",
                       "edge_type": "links"}],
        })
    result = plugin_pkg.preamble.on_pre_llm_call()
    assert isinstance(result, dict)
    ctx = result.get("context", "")
    assert "design" in ctx
    assert "canvas-active-1" in ctx
    assert "n1" in ctx and "n2" in ctx
    assert "links" in ctx


def test_preamble_caps_nodes(plugin_pkg, monkeypatch):
    plugin_pkg.commands.set_active_canvas_id("c-big")
    # 50 nodes; preamble should cap at the constant.
    nodes = [{"id": f"n{i}", "node_type": "doc"} for i in range(50)]
    monkeypatch.setattr(
        plugin_pkg.client, "get_canvas",
        lambda cid, **k: {
            "canvas": {"name": "big"}, "nodes": nodes, "edges": []})
    result = plugin_pkg.preamble.on_pre_llm_call()
    ctx = result["context"]
    cap = plugin_pkg.preamble._MAX_NODES_IN_PREAMBLE
    # The (cap)-th node id is `n<cap-1>` (zero-indexed); the
    # (cap+1)-th onwards should be omitted.
    assert f"`n{cap - 1}`" in ctx
    assert f"`n{cap}`" not in ctx
    assert "showing first" in ctx
