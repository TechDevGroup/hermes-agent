"""devagentic-lane-h plugin entrypoint (G4 of devagentic#203 /
hermes-agent#58).

Worker-facing read surface for Lane H ``kind:reasoning-graft-candidate``
docs. These are emitted automatically by devagentic's cycle_tick
when a confer-result clears the Lane H auto-trigger confidence
threshold (#174). Workers consume them as authoritative preamble
for subsequent moves.

Phase 1 (this PR) — MCP tools:
  * ``lane_h_list(user_id?, limit?)`` — list a user's recent grafts
    newest-first (wraps ``reasoningGraftCandidates`` GraphQL query
    from devagentic#207).
  * ``lane_h_fetch(graft_id)`` — fetch a single graft by id (wraps
    the existing ``searchDocs`` retrieval pattern + validates the
    kind tag).

No ``requestReasoningGraft`` tool: Lane H is auto-triggered by the
cycle_tick when conferring produces a high-confidence result.
Workers trigger the underlying flow via ``confer_run`` (G2,
already shipped). The pipeline picks up where confer-loop leaves
off; this plugin only exposes the READ side.

Transport mirrors the other devagentic-adjacent plugins
(canvas / docs / mutations / vertical-preamble): same env vars,
same X-User-Id resolution, same fail-soft contract.

Enable/disable via ``hermes plugin enable|disable
devagentic-lane-h``. Inert when disabled.
"""
from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Plugin loader entrypoint (hermes-agent#78). MCP-only plugin —
    ``lane_h_list`` + ``lane_h_fetch`` + ``grafted_context_fetch``
    tools are registered server-side in
    ``mcp_serve.py::_register_lane_h_tools``. Nothing to wire through
    the plugin loader directly.

    register() exists so the loader doesn't warn `no register()` and
    skip the plugin."""
    logger.debug("devagentic-lane-h: loaded (MCP-only; tools via mcp_serve)")
