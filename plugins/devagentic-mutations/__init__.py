"""devagentic-mutations plugin entrypoint (G2 of devagentic#203 /
hermes-agent#56).

Exposes devagentic graph mutations + queries as first-class hermes
MCP tools so workers never shell out to ``python api.py`` or hit
``genesis/doc.jsonl`` directly (#203 §1.2 invariant).

Phase 1 (this PR): ``writeDoc`` + ``querySilo`` — the foundational
write + read pair. Follow-up sub-issues track confer / assertOutput /
forkContext / patchArtifact / readArtifact / fetchUrl.

Transport mirrors the other devagentic-adjacent plugins
(devagentic-canvas, agent/devagentic_memory.py, agent/devagentic_skills.py)
— same env vars, same X-User-Id resolution, same fail-soft contract
(returns None on failure; never raises).

Enable/disable via ``hermes plugin enable|disable
devagentic-mutations``. Inert when disabled.
"""
from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Plugin loader entrypoint (hermes-agent#78). MCP-only plugin —
    ``silo_query`` + ``confer_run`` tools are registered server-side in
    ``mcp_serve.py::_register_devagentic_mutation_tools`` (callable
    from MCP clients including hermes-internal MCP-client bridges).
    Nothing to wire through the plugin loader directly.

    register() exists so the loader doesn't warn `no register()` and
    skip the plugin; the plugin-yaml declares the surface as MCP."""
    logger.debug("devagentic-mutations: loaded (MCP-only; tools via mcp_serve)")
