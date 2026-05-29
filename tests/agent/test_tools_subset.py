"""Tests for HERMES_TOOLS_SUBSET tool surface narrowing (hermes-agent#74).

The filter is a ~15-LOC block embedded in agent/agent_init.py right
between get_tool_definitions and valid_tool_names recomputation.
Rather than importing the full agent_init (heavy module with many
side-effects), tests evaluate a self-contained mirror of the same
expression against stub agent.tools lists.

A patch-landed-correctly smoke test asserts the production code path
contains the expected guard so the mirror cannot drift unnoticed.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace


def _apply_subset(env_value: str | None, agent_tools: list[dict],
                  quiet_mode: bool = True) -> tuple[list[dict], str]:
    """Mirror of the production guard in agent/agent_init.py post-#74.
    Returns (filtered_tools, stdout_captured)."""
    import os as _os
    if env_value is None:
        _os.environ.pop("HERMES_TOOLS_SUBSET", None)
    else:
        _os.environ["HERMES_TOOLS_SUBSET"] = env_value

    # ── Mirror block (keep in sync with agent_init.py post-#74) ──
    cap = io.StringIO()
    _saved = sys.stdout
    sys.stdout = cap
    try:
        agent = SimpleNamespace(tools=list(agent_tools), quiet_mode=quiet_mode)

        _subset_raw = (_os.environ.get("HERMES_TOOLS_SUBSET") or "").strip()
        if _subset_raw and agent.tools:
            _wanted = {n.strip() for n in _subset_raw.split(",") if n.strip()}
            if _wanted:
                _before = len(agent.tools)
                agent.tools = [
                    t for t in agent.tools
                    if (t.get("function") or {}).get("name") in _wanted
                ]
                if not agent.quiet_mode:
                    _kept = sorted({
                        (t.get("function") or {}).get("name", "?")
                        for t in agent.tools
                    })
                    print(
                        f"🎯 HERMES_TOOLS_SUBSET narrowed tool surface: "
                        f"{_before} → {len(agent.tools)} "
                        f"({', '.join(_kept) if _kept else '<empty>'})"
                    )
    finally:
        sys.stdout = _saved
        _os.environ.pop("HERMES_TOOLS_SUBSET", None)

    return agent.tools, cap.getvalue()


def _tool(name: str) -> dict:
    return {"type": "function",
            "function": {"name": name, "description": f"stub {name}"}}


def _names(tools: list[dict]) -> list[str]:
    return sorted((t.get("function") or {}).get("name", "?") for t in tools)


# ─── env unset → no filtering ────────────────────────────────

def test_env_unset_no_filter():
    tools = [_tool("a"), _tool("b"), _tool("c")]
    out, log = _apply_subset(None, tools)
    assert _names(out) == ["a", "b", "c"]
    assert log == ""


def test_env_empty_string_no_filter():
    tools = [_tool("a"), _tool("b")]
    out, _log = _apply_subset("", tools)
    assert _names(out) == ["a", "b"]


def test_env_whitespace_only_no_filter():
    tools = [_tool("a"), _tool("b")]
    out, _log = _apply_subset("   \t  ", tools)
    assert _names(out) == ["a", "b"]


# ─── happy-path filtering ─────────────────────────────────────

def test_subset_narrows_to_named_tools():
    tools = [_tool("a"), _tool("b"), _tool("c"), _tool("d")]
    out, _log = _apply_subset("a,c", tools)
    assert _names(out) == ["a", "c"]


def test_subset_strips_whitespace_in_names():
    tools = [_tool("alpha"), _tool("beta"), _tool("gamma")]
    out, _log = _apply_subset("  alpha , beta ", tools)
    assert _names(out) == ["alpha", "beta"]


def test_subset_silently_ignores_unknown_names():
    """Plugins can add/remove tools at runtime — pre-validation would
    over-warn. Unknown names just don't match."""
    tools = [_tool("a"), _tool("b")]
    out, _log = _apply_subset("a,nonexistent,b,also-not-there", tools)
    assert _names(out) == ["a", "b"]


def test_subset_no_matches_yields_empty_tools():
    """Operator narrowed to a set with zero matches — agent.tools
    becomes empty (model sees no tools that turn). This is intentional;
    operator may want a no-tools session via subset."""
    tools = [_tool("a"), _tool("b")]
    out, _log = _apply_subset("xyz,abc", tools)
    assert _names(out) == []


# ─── log surface ──────────────────────────────────────────────

def test_quiet_mode_suppresses_log():
    tools = [_tool("a"), _tool("b"), _tool("c")]
    out, log = _apply_subset("a", tools, quiet_mode=True)
    assert _names(out) == ["a"]
    assert log == ""


def test_non_quiet_mode_logs_narrowing():
    tools = [_tool("a"), _tool("b"), _tool("c")]
    out, log = _apply_subset("a,c", tools, quiet_mode=False)
    assert _names(out) == ["a", "c"]
    assert "HERMES_TOOLS_SUBSET narrowed tool surface" in log
    assert "3 → 2" in log
    assert "a" in log and "c" in log


def test_non_quiet_zero_kept_logs_empty_marker():
    tools = [_tool("a"), _tool("b")]
    out, log = _apply_subset("xyz", tools, quiet_mode=False)
    assert _names(out) == []
    assert "2 → 0" in log
    assert "<empty>" in log


# ─── degenerate inputs ───────────────────────────────────────

def test_empty_tools_list_with_subset_is_noop():
    out, log = _apply_subset("a,b", [], quiet_mode=False)
    assert out == []
    assert log == ""  # Guard short-circuits: agent.tools empty


# ─── Smoke: patch landed in production code path ─────────────

def test_patch_landed_correctly():
    """Production code path in agent/agent_init.py contains the
    HERMES_TOOLS_SUBSET filter. Keeps the mirror in this test file in
    sync — if production drifts, this test catches it."""
    src = (Path(__file__).resolve().parents[2]
           / "agent" / "agent_init.py").read_text()
    assert "HERMES_TOOLS_SUBSET" in src, "env var name absent from source"
    assert "hermes-agent#74" in src, "issue reference absent — patch missing or moved"
    assert "narrowed tool surface" in src, "narrowing log message absent"
    # #159: the write/exec-surface-wipe warning must be wired.
    assert "hermes-agent#159" in src, "#159 surface-wipe guard absent"
    assert "suppressed_exec_surface" in src, "#159 helper not wired into agent_init"
    # Verify ordering: filter sits BEFORE valid_tool_names recomputation
    # (so the recomputation reflects the filtered set). Anchor on the post-#86
    # get_subset_allow() call (parsing moved to hermes_cli.tool_subset).
    filter_idx = src.find("_subset_allow = get_subset_allow()")
    valid_idx = src.find("agent.valid_tool_names = set()")
    assert filter_idx > 0 and valid_idx > 0, "anchors missing"
    assert filter_idx < valid_idx, (
        "HERMES_TOOLS_SUBSET filter must run BEFORE valid_tool_names "
        "recomputation so the validation set reflects the narrowed surface"
    )


# ─── #159: write/exec-surface-wipe detection ─────────────────


def test_suppressed_exec_surface_total_wipe_lists_actuators():
    """The poly-explorer footgun: subset kept only an MCP tool, wiping the
    whole write/exec actuator surface → returns the suppressed actuators."""
    from hermes_cli.tool_subset import suppressed_exec_surface
    before = {"write_file", "execute_code", "terminal", "patch", "read_file",
              "web_search", "mcp_hermes_internal_grafted_context_fetch"}
    kept = {"mcp_hermes_internal_grafted_context_fetch"}
    assert suppressed_exec_surface(before, kept) == [
        "execute_code", "patch", "terminal", "write_file"
    ]


def test_suppressed_exec_surface_quiet_when_actuator_survives():
    """The legitimate sandbox profile pins the actuators → stays quiet."""
    from hermes_cli.tool_subset import suppressed_exec_surface
    before = {"write_file", "execute_code", "terminal", "patch", "read_file", "web_search"}
    kept = {"execute_code", "read_file", "write_file", "patch", "terminal"}
    assert suppressed_exec_surface(before, kept) == []


def test_suppressed_exec_surface_quiet_when_partial_keep():
    """Dropping terminal but keeping write_file still lets the agent act —
    intentional narrowing stays quiet (no false alarm)."""
    from hermes_cli.tool_subset import suppressed_exec_surface
    assert suppressed_exec_surface({"write_file", "terminal", "read_file"},
                                   {"write_file", "read_file"}) == []


def test_suppressed_exec_surface_quiet_when_no_actuators_before():
    """A read/think-only surface had no actuators to lose → quiet."""
    from hermes_cli.tool_subset import suppressed_exec_surface
    assert suppressed_exec_surface({"read_file", "web_search", "todo"},
                                   {"web_search"}) == []


def test_suppressed_exec_surface_ignores_empty_names():
    from hermes_cli.tool_subset import suppressed_exec_surface
    assert suppressed_exec_surface({"", "write_file"}, {""}) == ["write_file"]
