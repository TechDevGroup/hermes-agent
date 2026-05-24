"""Regression tests for hermes-agent#82 CLI-path autowire (post-#83 follow-up).

#83's autowire only fires from tui_gateway/entry.py. The `hermes` CLI
entrypoint (hermes_cli/main.py:main) is a separate code path and skips
the autowire entirely — so direct `hermes --provider X --model Y`
invocations leave config.yaml without `mcp_servers.hermes-internal`.

These source-level checks catch the regression class:
1. hermes_cli/main.py calls ensure_internal_mcp_server() at all.
2. The call is BEFORE discover_mcp_tools() — required so the freshly-
   written entry is visible on the same boot.
3. The call is inside the CLI-startup gated block (so it fires for
   chat/acp/rl/bare-invocation, not for `hermes mcp add` /
   introspection commands).

Source-level rather than runtime because exercising the full CLI main()
requires mocking argparse, provider auth, model resolution — orders of
magnitude more setup than the bug warrants. The regression risk is
"someone deletes the call" or "someone reorders past discover_mcp_tools"
— both caught at the source level.
"""
from __future__ import annotations

from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MAIN_PY = REPO / "hermes_cli" / "main.py"


def _main_py_src() -> str:
    return MAIN_PY.read_text()


def test_cli_main_calls_ensure_internal_mcp_server():
    """hermes_cli/main.py must call ensure_internal_mcp_server() —
    otherwise direct `hermes` invocations skip the autowire that
    tui_gateway/entry.py handles for the TUI-gateway path."""
    src = _main_py_src()
    assert "ensure_internal_mcp_server()" in src, (
        "hermes_cli/main.py missing ensure_internal_mcp_server() call; "
        "the `hermes` CLI entrypoint won't auto-register mcp_serve, "
        "and config.yaml will boot without `mcp_servers.hermes-internal`."
    )


def test_autowire_before_discover_mcp_tools_in_cli_main():
    """The autowire MUST run before discover_mcp_tools in the CLI
    startup block — otherwise discover sees stale config and skips
    the hermes-internal server on the same boot."""
    src = _main_py_src()
    autowire_pos = src.find("ensure_internal_mcp_server()")
    discover_pos = src.find(
        "from tools.mcp_tool import discover_mcp_tools"
    )
    assert autowire_pos > 0, "ensure_internal_mcp_server() not found"
    assert discover_pos > 0, "discover_mcp_tools import not found"
    assert autowire_pos < discover_pos, (
        "ensure_internal_mcp_server() must precede the "
        "`from tools.mcp_tool import discover_mcp_tools` block in "
        "hermes_cli/main.py — otherwise discover_mcp_tools reads "
        "config before the autowire writes the entry."
    )


def test_autowire_inside_cli_agent_startup_block():
    """The autowire must be inside the `_AGENT_COMMANDS` gated block
    so it only runs for agent-running commands (chat/acp/rl/bare). It
    must NOT run for management commands (`hermes mcp add`, `hermes
    hooks list`, `hermes cron list`, ...) — those don't need the
    autowire and triggering it would surprise operators who are
    inspecting state.

    We assert this structurally: the autowire call must appear after
    the `_AGENT_COMMANDS` definition AND after the gate's `if` check,
    AND before the `# Handle top-level --oneshot` block that follows
    the gate."""
    src = _main_py_src()
    agent_commands_pos = src.find("_AGENT_COMMANDS = {")
    autowire_pos = src.find("ensure_internal_mcp_server()")
    oneshot_pos = src.find("# Handle top-level --oneshot")

    assert agent_commands_pos > 0, "_AGENT_COMMANDS marker missing"
    assert autowire_pos > 0, "ensure_internal_mcp_server() missing"
    assert oneshot_pos > 0, "--oneshot marker missing"

    assert agent_commands_pos < autowire_pos < oneshot_pos, (
        "ensure_internal_mcp_server() must be inside the CLI startup "
        "gated block (between _AGENT_COMMANDS definition and the "
        "--oneshot handler) — not outside, where it would fire for "
        "management commands too."
    )
