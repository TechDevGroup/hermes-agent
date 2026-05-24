"""Pre-LLM-call hook for the devagentic-vertical-preamble plugin
(G1 of devagentic#203 / hermes-agent#55).

Auto-injects the active hermes profile's vertical context once per
process. Unlike the canvas plugin (which re-injects every turn
while a canvas is "open"), vertical context is session-stable —
the worker shouldn't pay the token cost of re-receiving the same
manifest on every turn.

Gate sequence on every call:
  1. resolve user_id (env override or active profile)
  2. skip if user_id is missing or == "default" (no vertical)
  3. skip if this user_id was already loaded in this process
  4. fetch verticalContext(userId) via the GraphQL client
  5. render the rollup; mark loaded; return {"context": ...}

Any failure returns None. The turn proceeds without preamble.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import client as vertical_client


logger = logging.getLogger(__name__)


# Per-process gate. Each hermes CLI invocation is a fresh process,
# so this set is naturally session-scoped. Long-running daemon
# embeddings (e.g. gateway) would need a session-keyed map — file a
# follow-up if/when that lands.
_LOADED_USER_IDS: set[str] = set()


# Caps — index-only rendering (hermes-agent#71) keeps the preamble
# bounded automatically. _ABSTRACT_CHARS sets the per-graft summary
# length in the index; full bodies are NEVER in the preamble (worker
# fetches on demand via the grafted_context_fetch MCP tool).
# _MAX_TOTAL_PREAMBLE_CHARS remains as a safety net for pathological
# guardrails / vertical-spec sizes.
_ABSTRACT_CHARS = 120  # 1-line abstract from the first 120 chars of content
_MAX_TOTAL_PREAMBLE_CHARS = 32768


def reset_loaded_cache() -> None:
    """Test hook — clear the per-process loaded-user_id set."""
    _LOADED_USER_IDS.clear()


def _render_preamble(rollup: dict) -> Optional[str]:
    """Render the verticalContext rollup as a compact markdown
    block. Returns None when there's nothing useful to render
    (no vertical-spec, no grafts, no guardrails). Caps per-doc
    content + total length."""
    spec = rollup.get("verticalSpec") or None
    grafts = rollup.get("graftedContexts") or []
    guardrails = rollup.get("workerGuardrails") or []
    if not spec and not grafts and not guardrails:
        return None
    lines: list[str] = []
    if spec:
        name = spec.get("name") or "(unnamed)"
        uid = spec.get("userId") or "(no user_id)"
        created = spec.get("createdTs") or ""
        gsc = spec.get("graftSourceCount") or 0
        lines.append(f"## Active vertical: `{name}` (user_id=`{uid}`)")
        lines.append(
            "Treat the following as session-stable background context. "
            "It reflects this vertical's spinUpVertical manifest + "
            "grafted concept material at session boot."
        )
        meta_bits: list[str] = []
        if created:
            meta_bits.append(f"_created_ts:_ {created}")
        if gsc:
            meta_bits.append(f"_graft_sources:_ {gsc}")
        manifest = spec.get("manifestPath")
        if manifest:
            meta_bits.append(f"_manifest:_ `{manifest}`")
        if meta_bits:
            lines.append(" · ".join(meta_bits))
    if guardrails:
        lines.append("")
        lines.append(
            f"### Worker guardrails ({len(guardrails)} doc"
            + ("s" if len(guardrails) != 1 else "") + ")"
        )
        lines.append(
            "These constraints govern this worker's behavior. They "
            "originated from the vertical spin-up and survive auto-"
            "compaction via the worker's own CLAUDE.md."
        )
        for g in guardrails:
            text = (g.get("constraints") or "").strip()
            if text:
                lines.append("")
                lines.append(text)
    if grafts:
        # v2 (hermes-agent#71): render INDEX only, NOT full bodies.
        # Workers fetch bodies on demand via the grafted_context_fetch
        # MCP tool. Cuts per-turn preamble cost from ~16KB (8 grafts at
        # 2KB cap) to ~50-150 chars per indexed graft. Scales to
        # hundreds of grafts without saturating model attention.
        lines.append("")
        lines.append(
            f"### Grafted concept material — INDEX ({len(grafts)} doc"
            + ("s" if len(grafts) != 1 else "") + ")"
        )
        lines.append(
            "Full bodies are NOT included in this preamble. Use the "
            "`grafted_context_fetch(graft_id)` MCP tool to load any "
            "row\'s full content on demand — typical pattern is to "
            "skim the index, identify relevant rows by source/path/"
            "abstract, then fetch only what you need for the current "
            "turn."
        )
        lines.append("")
        for g in grafts:
            gid = g.get("id") or "(no-id)"
            src = g.get("source") or "(unknown)"
            path = g.get("path") or "(no path)"
            content = (g.get("content") or "").strip()
            abstract = content[:_ABSTRACT_CHARS].replace("\n", " ").strip()
            if len(content) > _ABSTRACT_CHARS:
                abstract = abstract + "…"
            # One-line index entry. Markdown table-like for grep-ability.
            lines.append(
                f"- `{gid}` · {src} · `{path}` — {abstract}"
            )
    rendered = "\n".join(lines)
    if len(rendered) > _MAX_TOTAL_PREAMBLE_CHARS:
        rendered = (
            rendered[:_MAX_TOTAL_PREAMBLE_CHARS]
            + "\n\n…(preamble truncated to "
            f"{_MAX_TOTAL_PREAMBLE_CHARS} chars)"
        )
    return rendered or None


def on_pre_llm_call(**kwargs: Any) -> Optional[dict]:
    """``pre_llm_call`` hook entrypoint. Returns either:
      * ``{"context": "<rendered vertical preamble>"}`` on the
        first call of a process where the active profile resolves
        to a vertical with at least one doc.
      * ``None`` for: no resolvable user_id, the "default" profile,
        an already-loaded user_id in this process, devagentic
        unreachable, or no docs for this vertical. The turn always
        proceeds without preamble.
    """
    try:
        user_id = vertical_client.resolve_user_id()
    except Exception as exc:  # noqa: BLE001
        logger.debug("vertical-preamble: user_id resolution failed: %s", exc)
        return None
    if not user_id or user_id == "default":
        return None
    if user_id in _LOADED_USER_IDS:
        return None
    try:
        rollup = vertical_client.fetch_vertical_context(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("vertical-preamble: fetch failed: %s", exc)
        return None
    if not rollup:
        return None
    try:
        rendered = _render_preamble(rollup)
    except Exception as exc:  # noqa: BLE001
        logger.debug("vertical-preamble: render failed: %s", exc)
        return None
    if not rendered:
        # Mark loaded anyway — empty rollups won't suddenly populate
        # mid-process, and repeatedly re-querying devagentic on every
        # turn for an empty vertical is wasted RTT.
        _LOADED_USER_IDS.add(user_id)
        return None
    _LOADED_USER_IDS.add(user_id)
    return {"context": rendered}
