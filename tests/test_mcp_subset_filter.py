"""Contract tests for hermes-agent#86 — HERMES_TOOLS_SUBSET filtering
of MCP-discovered tools.

Validates:
- Shared helper `get_subset_allow()` correctly parses the env var
  across unset/empty/whitespace/single/multi/messy cases
- `is_tool_allowed()` honors the allow-list (None = pass-through)
- Source-level: `_register_server_tools` in tools/mcp_tool.py applies
  the filter inside both the main tool loop AND the utility-tools
  loop (regression catcher for "fix the main loop, forget utility
  tools" class of bug)
- Source-level: agent_init.py uses the shared helper rather than
  reimplementing the parse (catches drift between the two sites)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli.tool_subset import (
    ENV_VAR,
    get_subset_allow,
    is_tool_allowed,
)


REPO = Path(__file__).resolve().parents[1]


# ─── get_subset_allow ──────────────────────────────────────────────────

def test_unset_env_returns_none(monkeypatch):
    """Unset env var → None (no filtering)."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert get_subset_allow() is None


def test_empty_env_returns_none(monkeypatch):
    """Empty string → None (treated same as unset)."""
    monkeypatch.setenv(ENV_VAR, "")
    assert get_subset_allow() is None


def test_whitespace_only_env_returns_none(monkeypatch):
    """Just whitespace → None."""
    monkeypatch.setenv(ENV_VAR, "   ,   ,  ")
    assert get_subset_allow() is None


def test_single_tool(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "foo")
    assert get_subset_allow() == {"foo"}


def test_multiple_tools(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "foo,bar,baz")
    assert get_subset_allow() == {"foo", "bar", "baz"}


def test_whitespace_padding_stripped(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "  foo , bar,  baz  ")
    assert get_subset_allow() == {"foo", "bar", "baz"}


def test_mcp_prefixed_names_allowed(monkeypatch):
    """MCP-prefixed names should parse same as built-in names."""
    monkeypatch.setenv(
        ENV_VAR,
        "doc_view,mcp_hermes-internal_grafted_context_fetch,mcp_linear_create_issue",
    )
    assert get_subset_allow() == {
        "doc_view",
        "mcp_hermes-internal_grafted_context_fetch",
        "mcp_linear_create_issue",
    }


# ─── is_tool_allowed ───────────────────────────────────────────────────

def test_none_subset_allows_everything():
    """None means no filter — every name passes."""
    assert is_tool_allowed("anything", None) is True
    assert is_tool_allowed("mcp_x_y", None) is True
    assert is_tool_allowed("", None) is True


def test_allow_list_exact_match():
    subset = {"foo", "mcp_x_y"}
    assert is_tool_allowed("foo", subset) is True
    assert is_tool_allowed("mcp_x_y", subset) is True


def test_allow_list_excludes_others():
    subset = {"foo"}
    assert is_tool_allowed("bar", subset) is False
    assert is_tool_allowed("mcp_x_foo", subset) is False  # substring no-match
    assert is_tool_allowed("", subset) is False


def test_filter_uses_prefixed_name_for_mcp():
    """The subset compares against the MCP-prefixed registry name
    (mcp_<server>_<tool>), not the unprefixed tool name. Document this
    contract in a test so the predictable-behavior decision survives
    future fuzzy-matching feature requests."""
    subset = {"mcp_hermes-internal_grafted_context_fetch"}
    # Prefixed name in subset → allowed
    assert is_tool_allowed(
        "mcp_hermes-internal_grafted_context_fetch", subset
    ) is True
    # Bare unprefixed name NOT in subset → not allowed (by design)
    assert is_tool_allowed("grafted_context_fetch", subset) is False


# ─── Source-level: filter wired into both MCP registration loops ──────

def test_register_server_tools_filters_main_loop():
    """The per-tool loop in _register_server_tools must invoke
    _is_tool_allowed before registry.register — otherwise filtered
    tools enter the registry and the fix is partial."""
    src = (REPO / "tools" / "mcp_tool.py").read_text()
    # Find the body of _register_server_tools.
    body_start = src.find("def _register_server_tools(")
    assert body_start > 0
    body_end = src.find("\ndef ", body_start + 1)
    body = src[body_start:body_end]

    # The body must mention is_tool_allowed.
    assert "_is_tool_allowed" in body, (
        "_register_server_tools must call the subset filter; "
        "otherwise MCP tools bypass HERMES_TOOLS_SUBSET (#86)"
    )

    # The body must call get_subset_allow once per invocation.
    assert "_get_subset_allow" in body or "get_subset_allow()" in body, (
        "_register_server_tools must compute the subset once at the "
        "top, not per-tool, to avoid O(N) env-parse cost"
    )


def test_register_server_tools_filters_utility_loop():
    """The utility-tools loop (list_resources, read_resource, etc.)
    must also apply the filter. Regression catcher for 'fix the main
    loop, forget utility tools' — utility tools are MCP-provided too
    and should obey HERMES_TOOLS_SUBSET."""
    src = (REPO / "tools" / "mcp_tool.py").read_text()
    # The utility-tools loop iterates over `_select_utility_schemas`.
    util_loop_pos = src.find("for entry in _select_utility_schemas(")
    assert util_loop_pos > 0, "utility-tools loop marker not found"
    # Tail of the file from that point must contain the filter call
    # inside the loop body (before registry.register for util_name).
    util_tail = src[util_loop_pos: util_loop_pos + 3000]
    # Filter call exists somewhere before the next registry.register.
    register_pos = util_tail.find("registry.register(\n            name=util_name")
    assert register_pos > 0, "utility-tools registry.register not found"
    filter_pos = util_tail.find("_is_tool_allowed(util_name")
    assert 0 < filter_pos < register_pos, (
        "utility-tools loop missing subset filter — utility MCP tools "
        "bypass HERMES_TOOLS_SUBSET. Regression class: 'fix the main "
        "loop, forget utility tools'."
    )


# ─── Source-level: agent_init refactored to shared helper ─────────────

def test_agent_init_uses_shared_subset_helper():
    """agent/agent_init.py (the original #75 filter site) must import
    from hermes_cli.tool_subset rather than reimplementing the env
    parse. Catches drift: if #86 changes ENV_VAR semantics later,
    both sites pick it up uniformly."""
    src = (REPO / "agent" / "agent_init.py").read_text()
    assert "from hermes_cli.tool_subset import" in src, (
        "agent_init.py must import from hermes_cli.tool_subset; "
        "inline reimplementation drifts from the MCP-side filter"
    )
    # Spot-check that the inline parse pattern is gone.
    assert "_subset_raw = (os.environ.get(\"HERMES_TOOLS_SUBSET\")" not in src, (
        "stale inline parser still present — should be replaced by "
        "get_subset_allow() helper call"
    )
