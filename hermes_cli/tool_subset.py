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
