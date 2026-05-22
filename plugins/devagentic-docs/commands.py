"""Slash command surface for the devagentic-docs plugin (hermes #12).
Registers `/doc` with subcommands:

  * `/doc search <query> [--tag <tag>] [--limit N]`  — top hits
  * `/doc write <body> [--tags t1,t2,...]`           — persist a doc
  * `/doc show <id>`                                 — full body

Failure semantics: every handler catches its own exceptions and
returns a user-facing error string. Reasons are surfaced from
`docs_client.last_error_text()` when a primitive returns None,
so operators distinguish auth / network / parse / no-user-id
without trawling DEBUG logs (same pattern as canvas after #15).
"""
from __future__ import annotations

import logging
import shlex
from typing import Optional

from . import client as docs_client


logger = logging.getLogger(__name__)


def _failure_detail() -> str:
    err = docs_client.last_error_text()
    return f" Reason: {err}." if err else ""


def _parse_search_args(args: str) -> tuple[str, int, Optional[str]]:
    """Return (query, limit, tag). Limit defaults to 10; tag to None.
    Recognises `--tag <t>` and `--limit N` flags anywhere in args."""
    try:
        tokens = shlex.split(args or "")
    except ValueError:
        tokens = (args or "").split()
    limit = 10
    tag: Optional[str] = None
    rest: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--tag" and i + 1 < len(tokens):
            tag = tokens[i + 1]
            i += 2
            continue
        if t == "--limit" and i + 1 < len(tokens):
            try:
                limit = max(1, min(100, int(tokens[i + 1])))
            except ValueError:
                pass
            i += 2
            continue
        rest.append(t)
        i += 1
    return " ".join(rest).strip(), limit, tag


def _parse_write_args(args: str) -> tuple[str, list[str]]:
    """Return (body, tags). Recognises a `--tags a,b,c` flag.
    Everything else is the body."""
    try:
        tokens = shlex.split(args or "")
    except ValueError:
        tokens = (args or "").split()
    tags: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--tags" and i + 1 < len(tokens):
            tags = [s.strip() for s in tokens[i + 1].split(",") if s.strip()]
            i += 2
            continue
        rest.append(t)
        i += 1
    return " ".join(rest).strip(), tags


def _handle_search(args: str) -> str:
    query, limit, tag = _parse_search_args(args)
    if not query:
        return ("Usage: `/doc search <query> [--tag <tag>] "
                "[--limit N]`. Tag scopes to docs with that exact "
                "tag; limit defaults to 10.")
    hits = docs_client.search_docs(query=query, limit=limit, tag=tag)
    if hits is None:
        return ("Couldn't reach devagentic for the doc search."
                + _failure_detail())
    if not hits:
        return (f"No docs matched `{query}`"
                + (f" (tag=`{tag}`)" if tag else "") + ".")
    lines = [f"**Top {len(hits)} hit(s)" +
             (f" (tag=`{tag}`)" if tag else "") + ":**"]
    for h in hits:
        did = h.get("id") or "(no id)"
        snippet = ((h.get("content") or "").splitlines() or [""])[0]
        snippet = snippet[:120]
        score = h.get("score")
        score_str = f"  _score={score:.2f}_" if isinstance(
            score, (int, float)) else ""
        lines.append(f"- `{did}` — {snippet}{score_str}")
    return "\n".join(lines)


def _handle_write(args: str) -> str:
    body, tags = _parse_write_args(args)
    if not body:
        return ("Usage: `/doc write <body> [--tags t1,t2,...]`. "
                "Body is required; tags are optional. The doc is "
                "auto-tagged with `source:hermes-cli`.")
    # Always tag with source:hermes-cli so doc-graph queries can
    # distinguish CLI-authored finds from worker-authored.
    if "source:hermes-cli" not in tags:
        tags = list(tags) + ["source:hermes-cli"]
    doc = docs_client.write_doc(content=body, tags=tags,
                                 source="hermes-cli")
    if doc is None:
        return ("Couldn't persist the doc." + _failure_detail())
    did = doc.get("id") or "(no id)"
    tag_str = " ".join(f"`{t}`" for t in tags)
    return (f"Wrote doc **`{did}`**. Tags: {tag_str}.")


def _handle_show(args: str) -> str:
    did = (args or "").strip().split()[0] if args.strip() else ""
    if not did:
        return ("Usage: `/doc show <id>`. List ids with "
                "`/doc search …` first.")
    doc = docs_client.get_doc(did)
    if doc is None:
        return (f"Couldn't fetch doc `{did}`." + _failure_detail())
    content = doc.get("content") or "(empty)"
    tags = doc.get("tags") or []
    tag_str = " ".join(f"`{t}`" for t in tags) if tags else "_(no tags)_"
    return (f"**`{did}`** — {tag_str}\n\n{content}")


_SUBCOMMANDS = {
    "search": _handle_search,
    "write":  _handle_write,
    "show":   _handle_show,
}


def doc_command(args: str) -> str:
    """Dispatcher for `/doc <sub> <args>`. Bare `/doc` is a usage
    hint; unknown subcommands fall back to the same hint."""
    raw = (args or "").strip()
    if not raw:
        return ("Usage: `/doc <subcommand>` — `search`, `write`, "
                "or `show`.")
    head, _, tail = raw.partition(" ")
    sub = head.strip().lower()
    handler = _SUBCOMMANDS.get(sub)
    if handler is None:
        return (f"Unknown `/doc` subcommand: `{sub}`. "
                f"Available: {', '.join(_SUBCOMMANDS)}.")
    try:
        return handler(tail)
    except Exception as exc:  # noqa: BLE001
        logger.exception("devagentic-docs: handler crashed")
        return f"`/doc {sub}` failed: {exc}"
