"""Defensive-hardening + contract tests for ``create_mcp_server``
(issue #88).

The original #88 triage uncovered three asks once the false-alarm was
ruled out:

1. One failing registrar should not crash peer registrars — wrap each
   call in try/except + WARN log.
2. Boot-line log of registered tools so deployed-container stderr
   shows EXACTLY which tools are present in that wheel (without the
   operator having to truncate-enumerate).
3. Contract test asserting the G2/G3/G4 tool families are present, so
   a registrar being dropped or its decorator path breaking surfaces
   as a CI failure instead of in production.
"""
from __future__ import annotations

import logging

import pytest


pytest.importorskip("mcp", reason="MCP SDK not installed")


# ---------------------------------------------------------------------------
# (3) contract — G1/G2/G3/G4 tool families
# ---------------------------------------------------------------------------

# Canvas (G2 pre-cascade), docs (canvas sibling), devagentic-mutations
# (G2), github (G3), lane-h + grafted-context (G4) tool families. Pulled
# out by name so a dropped registrar surfaces as a precise diff.
_REQUIRED_DEVAGENTIC_TOOLS = {
    # G2 — devagentic-mutations
    "silo_query",
    "confer_run",
    "assert_output",  # G2b (#60)
    "read_artifact",  # G2c (#61) — companion read tool
    "preview_patch",  # G2c (#61) — supplies confirm_token
    "patch_artifact",  # G2c (#61) — in-place find/replace
    "fetch_url",  # G2d (#62) — localhost-only HTTP fetch
    # G3 — hermes-github
    "file_issue",
    # G4 — devagentic-lane-h
    "lane_h_list",
    "lane_h_fetch",
    "grafted_context_fetch",
    # canvas family (pre-G2 cascade, devagentic#203 G2 sibling)
    "canvas_list",
    "canvas_open",
    "canvas_add_node",
    "canvas_search",
    # docs family (canvas sibling)
    "doc_search",
    "doc_write",
    "doc_show",
    "fork_open",
}


def test_devagentic_tools_all_registered():
    """All G1–G4 + canvas + docs tools surface on a fresh server.

    If a registrar gets dropped (e.g., a future refactor removes the
    call from create_mcp_server) or its decorator path breaks
    (e.g., the @mcp.tool() decorator import path changes), this test
    fails with a precise "Missing: {...}" diff naming the dropped
    tools.
    """
    import mcp_serve

    server = mcp_serve.create_mcp_server()
    tools = server._tool_manager.list_tools()
    tool_names = {t.name for t in tools}

    missing = _REQUIRED_DEVAGENTIC_TOOLS - tool_names
    assert not missing, (
        f"Missing devagentic tools: {missing}. "
        f"Registered: {sorted(tool_names)}"
    )


# ---------------------------------------------------------------------------
# (1) defensive — one failing registrar does not kill peers
# ---------------------------------------------------------------------------

def test_failing_registrar_does_not_crash_server(monkeypatch, caplog):
    """If one registrar raises during create_mcp_server, the others
    still register and the failure surfaces as a WARN log line."""
    import mcp_serve

    def _broken_registrar(_mcp):
        raise RuntimeError("synthetic registrar failure (test-only)")

    # Replace _register_lane_h_tools with the broken stand-in.
    monkeypatch.setattr(mcp_serve, "_register_lane_h_tools", _broken_registrar)

    with caplog.at_level(logging.WARNING, logger="hermes.mcp_serve"):
        server = mcp_serve.create_mcp_server()

    # Peer registrars still ran — canvas + docs + mutations + github
    # should be present even though lane-h blew up.
    tools = server._tool_manager.list_tools()
    tool_names = {t.name for t in tools}
    assert "canvas_list" in tool_names
    assert "doc_search" in tool_names
    assert "silo_query" in tool_names
    assert "file_issue" in tool_names

    # And lane-h tools are NOT there (the broken registrar didn't run).
    assert "lane_h_list" not in tool_names

    # WARN log surfaces the failure.
    warn_msgs = [r.getMessage() for r in caplog.records
                 if r.levelno == logging.WARNING]
    assert any("lane-h" in m and "synthetic registrar failure" in m
               for m in warn_msgs), warn_msgs


def test_failing_registrar_warn_names_the_label(monkeypatch, caplog):
    """Each registrar carries a human label; the WARN line uses it so
    operators can identify which family failed without grepping
    function names."""
    import mcp_serve

    def _broken(_mcp):
        raise ValueError("kaboom")

    monkeypatch.setattr(mcp_serve, "_register_github_tools", _broken)

    with caplog.at_level(logging.WARNING, logger="hermes.mcp_serve"):
        mcp_serve.create_mcp_server()

    warn_msgs = [r.getMessage() for r in caplog.records
                 if r.levelno == logging.WARNING]
    # Label appears verbatim in the WARN line.
    assert any("github" in m for m in warn_msgs), warn_msgs


# ---------------------------------------------------------------------------
# (2) boot-line log surfaces the registered tool inventory
# ---------------------------------------------------------------------------

def test_boot_line_lists_registered_tools(caplog):
    """A single INFO-level log line at create_mcp_server completion
    shows the count + sorted tool names. Operators rely on this to
    confirm what is present in a deployed wheel."""
    import mcp_serve

    with caplog.at_level(logging.INFO, logger="hermes.mcp_serve"):
        mcp_serve.create_mcp_server()

    info_msgs = [r.getMessage() for r in caplog.records
                 if r.levelno == logging.INFO]
    boot_lines = [m for m in info_msgs if "MCP server boot:" in m]
    assert boot_lines, f"No boot-line log emitted. INFO log: {info_msgs}"

    line = boot_lines[-1]
    # Tool count present.
    assert "registered " in line and " tools:" in line
    # Sample of expected names visible in the line (sorted, so each
    # appears once in alpha order).
    for name in ("canvas_list", "doc_search", "lane_h_list",
                 "silo_query", "file_issue"):
        assert name in line, f"Tool {name!r} missing from boot line: {line}"


def test_boot_line_runs_even_when_a_registrar_fails(monkeypatch, caplog):
    """The boot line still emits when one registrar fails — operator
    sees both the WARN (which family failed) and the INFO (what's
    actually present) on the same boot."""
    import mcp_serve

    def _broken(_mcp):
        raise RuntimeError("test failure")
    monkeypatch.setattr(mcp_serve, "_register_canvas_tools", _broken)

    with caplog.at_level(logging.INFO, logger="hermes.mcp_serve"):
        mcp_serve.create_mcp_server()

    info_msgs = [r.getMessage() for r in caplog.records
                 if r.levelno == logging.INFO]
    boot_lines = [m for m in info_msgs if "MCP server boot:" in m]
    assert boot_lines, "Boot line must emit even when a registrar fails"
    # Canvas tools are NOT in the inventory (broken registrar).
    assert "canvas_list" not in boot_lines[-1]
    # Peer family tools ARE in the inventory.
    assert "lane_h_list" in boot_lines[-1]
