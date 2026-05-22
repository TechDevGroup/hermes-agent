"""Pre-LLM-call hook for the devagentic-docs plugin (hermes #12).

When a fork is "open" in the session (marker file written by
`/fork open <id>`), this hook calls `renderContext(ctxId)` on
devagentic and returns the rendered string as ephemeral context
for the turn. The hermes pre_llm_call contract appends the
returned context to the user message (not the system prompt) so
cached prompt prefixes stay valid.

Failure semantics: any failure (no marker, devagentic down,
empty render) returns None and the LLM call proceeds without
preamble. The hook never blocks the turn.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import client as docs_client, commands as docs_commands


logger = logging.getLogger(__name__)


# Cap so a runaway render can't crowd out the user's message.
# `renderContext` already does its own bounding on the devagentic
# side (it's used by devagentic itself for context injection), but
# we keep a hermes-side ceiling for safety.
_MAX_PREAMBLE_CHARS = 8000


def on_pre_llm_call(**kwargs: Any) -> Optional[dict]:
    """`pre_llm_call` hook entrypoint. Returns either:
      * `{"context": "<renderContext output>"}` when a fork is
        active and devagentic responds with non-empty text.
      * `None` when there's no active fork, the marker can't be
        read, devagentic is unreachable, or the render is empty.
        The turn proceeds without preamble.
    """
    try:
        ctx_id = docs_commands._read_active_fork()
    except Exception as exc:  # noqa: BLE001
        logger.debug("docs preamble: marker read failed: %s", exc)
        return None
    if not ctx_id:
        return None
    try:
        rendered = docs_client.render_context(ctx_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("docs preamble: render_context(%s) failed: %s",
                     ctx_id, exc)
        return None
    if rendered is None or not rendered.strip():
        return None
    if len(rendered) > _MAX_PREAMBLE_CHARS:
        rendered = rendered[: _MAX_PREAMBLE_CHARS] + "\n…[truncated]"
    header = (
        f"## Active devagentic fork: `{ctx_id}`\n"
        "Treat the following as background context for this turn. "
        "It reflects the fork's rendered state at request time and "
        "isn't instructional.\n\n"
    )
    return {"context": header + rendered}
