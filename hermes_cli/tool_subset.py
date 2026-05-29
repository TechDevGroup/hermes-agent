"""Shared HERMES_TOOLS_SUBSET parsing + allow-list check (#86).

Originally added inline in ``agent/agent_init.py`` (#75) to narrow the
built-in tool surface. #86 extends the same filter to MCP-discovered
tools at registration time — see ``tools/mcp_tool.py::_register_server_tools``
— so the subset uniformly applies to both built-in and MCP tools across
initial discovery and ``/reload-mcp`` paths.

Single source of truth: this module owns the env-var parsing + the
allow-list semantics so the two call sites (agent_init.py + mcp_tool.py)
can't drift in casing/whitespace/empty-vs-missing handling.
"""
from __future__ import annotations

import os
from typing import Optional, Set

ENV_VAR = "HERMES_TOOLS_SUBSET"

# hermes-agent#159 — the write/exec "actuator" spine. If a subset removes the
# whole set while the agent still has read/think tools, the agent can reason
# but not act, and the only symptom is a cryptic "Unknown tool 'write_file' —
# not in N registered tools". Used to make a surface-wiping subset loud at
# startup (a stray/inherited HERMES_TOOLS_SUBSET export collapsed a vertical
# to a single non-actionable tool — see #159).
CORE_EXEC_TOOLS = frozenset({"write_file", "patch", "execute_code", "terminal"})


def suppressed_exec_surface(before_names, kept_names) -> list[str]:
    """Return the write/exec actuator tools wiped by the subset filter.

    Non-empty ONLY when the subset removes the *entire* actuator surface —
    i.e. at least one of :data:`CORE_EXEC_TOOLS` was present before the filter
    and none survived. Returns ``[]`` when no actuators were present to begin
    with, or when at least one survived (intentional narrowing that keeps the
    agent able to act stays quiet — e.g. the sandbox profile pinning
    ``execute_code,read_file,write_file,patch,terminal``). This is the
    "can reason but not act" footgun from hermes-agent#159.
    """
    before = {n for n in before_names if n} & CORE_EXEC_TOOLS
    kept = {n for n in kept_names if n} & CORE_EXEC_TOOLS
    if before and not kept:
        return sorted(before)
    return []


def get_subset_allow() -> Optional[Set[str]]:
    """Return the parsed allow-list, or ``None`` when unset/empty.

    ``None`` means "no filtering — allow everything", distinct from an
    empty set (which would mean "allow nothing" — that's an edge case we
    treat the same as unset since an empty allow-list at boot would
    leave the worker with zero tools and isn't a meaningful configuration).
    """
    raw = (os.environ.get(ENV_VAR) or "").strip()
    if not raw:
        return None
    wanted = {n.strip() for n in raw.split(",") if n.strip()}
    return wanted or None


def is_tool_allowed(name: str, subset: Optional[Set[str]]) -> bool:
    """Check whether ``name`` is permitted by the subset.

    Pass ``None`` (or the result of ``get_subset_allow()`` when no env
    var is set) to mean "no filter, everything allowed". For MCP tools
    use the prefixed registry name (``mcp_<server>_<tool>``)."""
    if subset is None:
        return True
    return name in subset
