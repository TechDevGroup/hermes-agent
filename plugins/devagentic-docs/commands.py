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
import os
import shlex
from pathlib import Path
from typing import Optional

from . import client as docs_client


logger = logging.getLogger(__name__)


_FORK_MARKER_NAME = "docs-fork-active"


def _failure_detail() -> str:
    err = docs_client.last_error_text()
    return f" Reason: {err}." if err else ""


# --- Active-fork marker file (analog of canvas-active) ----

def _marker_path() -> Path:
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / _FORK_MARKER_NAME


def _read_active_fork() -> Optional[str]:
    p = _marker_path()
    if not p.exists():
        return None
    try:
        ctx_id = p.read_text(encoding="utf-8").strip()
        return ctx_id or None
    except OSError as exc:
        logger.warning("devagentic-docs: failed to read %s: %s", p, exc)
        return None


def _write_active_fork(ctx_id: str) -> bool:
    p = _marker_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(ctx_id, encoding="utf-8")
        return True
    except OSError as exc:
        logger.warning("devagentic-docs: failed to write %s: %s", p, exc)
        return False


def _clear_active_fork() -> bool:
    p = _marker_path()
    if not p.exists():
        return True
    try:
        p.unlink()
        return True
    except OSError as exc:
        logger.warning("devagentic-docs: failed to unlink %s: %s", p, exc)
        return False


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


# --- /fork command surface --------------------------------

def _parse_fork_open_args(args: str) -> tuple[str, Optional[str]]:
    try:
        tokens = shlex.split(args or "")
    except ValueError:
        tokens = (args or "").split()
    goal: Optional[str] = None
    rest: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--goal" and i + 1 < len(tokens):
            goal = tokens[i + 1]
            i += 2
            continue
        rest.append(t)
        i += 1
    return (rest[0] if rest else ""), goal


def _handle_fork_open(args: str) -> str:
    parent_id, goal = _parse_fork_open_args(args)
    if not parent_id:
        return ("Usage: `/fork open <parent_id> [--goal \"...\"]`. "
                "The parent is the doc or context id to derive from; "
                "`--goal` is a human description stored as the goal "
                "annotation on the new fork.")
    annotations = []
    if goal:
        annotations.append({"key": "goal", "value": goal,
                            "weight": 1.0})
    annotations.append({"key": "pinned-doc", "value": parent_id,
                        "weight": 1.0})
    ctx = docs_client.fork_context(
        parent_id=parent_id,
        tags=["source:hermes-cli"],
        annotations=annotations)
    if ctx is None:
        return ("Couldn't fork the context." + _failure_detail())
    ctx_id = ctx.get("id") or ""
    if not ctx_id:
        return "Fork succeeded but devagentic returned no id."
    if not _write_active_fork(ctx_id):
        return (f"Forked context `{ctx_id}` but couldn't persist the "
                "active-fork marker. /fork close to clear.")
    msg = (f"Opened fork **`{ctx_id}`** (parent: `{parent_id}`)")
    if goal:
        msg += f" — goal: _{goal}_"
    msg += ". Pinned-doc annotations will be injected each turn."
    return msg


def _handle_fork_close(_args: str) -> str:
    prior = _read_active_fork()
    _clear_active_fork()
    if prior:
        return f"Closed fork `{prior}`. No fork active."
    return "No fork was active."


def _handle_fork_show(_args: str) -> str:
    ctx_id = _read_active_fork()
    if not ctx_id:
        return "No fork is active. Open one with `/fork open <id>`."
    ctx = docs_client.get_context(ctx_id)
    if ctx is None:
        return (f"Active fork marker is `{ctx_id}` but devagentic "
                "is unreachable." + _failure_detail())
    tags = ctx.get("tags") or []
    annotations = ctx.get("annotations") or []
    lines = [f"**Active fork:** `{ctx_id}`"]
    goal = next((a.get("value") for a in annotations
                 if a.get("key") == "goal"), None)
    if goal:
        lines.append(f"_Goal:_ {goal}")
    pinned = [a.get("value") for a in annotations
              if a.get("key") == "pinned-doc"]
    if pinned:
        lines.append("**Pinned docs:**")
        for did in pinned:
            lines.append(f"- `{did}`")
    if tags:
        lines.append("Tags: " + ", ".join(f"`{t}`" for t in tags))
    return "\n".join(lines)


def _handle_fork_pin(args: str) -> str:
    did = (args or "").strip().split()[0] if args.strip() else ""
    if not did:
        return ("Usage: `/fork pin <doc_id>`. The doc will be added "
                "as a pinned-doc annotation on the active fork; its "
                "rendered content is injected every turn.")
    ctx_id = _read_active_fork()
    if not ctx_id:
        return "No fork is active. Open one with `/fork open <id>`."
    updated = docs_client.decorate_context(
        ctx_id=ctx_id, key="pinned-doc", value=did, weight=1.0)
    if updated is None:
        return ("Couldn't pin the doc." + _failure_detail())
    return f"Pinned doc `{did}` on fork `{ctx_id}`."


def _handle_fork_render(_args: str) -> str:
    ctx_id = _read_active_fork()
    if not ctx_id:
        return "No fork is active. Open one with `/fork open <id>`."
    rendered = docs_client.render_context(ctx_id)
    if rendered is None:
        return ("Couldn't render the fork." + _failure_detail())
    if not rendered.strip():
        return f"Fork `{ctx_id}` rendered to an empty string."
    return f"**Rendered fork `{ctx_id}`:**\n\n{rendered}"


_FORK_SUBCOMMANDS = {
    "open":   _handle_fork_open,
    "close":  _handle_fork_close,
    "show":   _handle_fork_show,
    "pin":    _handle_fork_pin,
    "render": _handle_fork_render,
}


def fork_command(args: str) -> str:
    """Dispatcher for `/fork <sub> <args>`."""
    raw = (args or "").strip()
    if not raw:
        return ("Usage: `/fork <subcommand>` — `open`, `close`, "
                "`show`, `pin`, or `render`.")
    head, _, tail = raw.partition(" ")
    sub = head.strip().lower()
    handler = _FORK_SUBCOMMANDS.get(sub)
    if handler is None:
        return (f"Unknown `/fork` subcommand: `{sub}`. "
                f"Available: {', '.join(_FORK_SUBCOMMANDS)}.")
    try:
        return handler(tail)
    except Exception as exc:  # noqa: BLE001
        logger.exception("devagentic-docs: fork handler crashed")
        return f"`/fork {sub}` failed: {exc}"
