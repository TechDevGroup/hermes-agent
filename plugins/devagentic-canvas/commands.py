"""Slash command surface for the devagentic-canvas plugin
(hermes issue #55). Registers `/canvas` with subcommands:

  * `/canvas list`              — list user's canvases
  * `/canvas open <id>`         — mark the canvas as active for the
                                   current session; the pre_llm_call
                                   hook injects its state as context
                                   on every subsequent turn
  * `/canvas close`             — clear the active-canvas marker
  * `/canvas show`              — print the currently-active canvas
                                   id + a short state summary
  * `/canvas new <name>`        — create a new canvas; prints the
                                   new id

The "active canvas" is tracked in `~/.hermes/canvas-active` (or
`$HERMES_HOME/canvas-active`) — a single-line file containing the
canvas id. Cleared by `/canvas close`. Survives process restart so
a long-lived session keeps the canvas open across reconnects.

Failure semantics: every handler catches its own exceptions and
returns a user-facing error string. The plugin NEVER raises out
of a slash command — that would surface as an opaque "command
failed" in the hermes UI.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from . import client as canvas_client


logger = logging.getLogger(__name__)


_ACTIVE_FILE_NAME = "canvas-active"


def _hermes_home() -> Path:
    """Resolve HERMES_HOME for the active-canvas marker. Same logic
    as the memory migration script — prefers `hermes_constants.
    get_hermes_home()` when importable; falls back to env / default."""
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    except Exception:
        home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        return Path(home)


def _active_canvas_path() -> Path:
    return _hermes_home() / _ACTIVE_FILE_NAME


def get_active_canvas_id() -> Optional[str]:
    """Read the active-canvas marker. Returns None when no canvas
    is open (file absent / empty / unreadable)."""
    p = _active_canvas_path()
    if not p.is_file():
        return None
    try:
        cid = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return cid or None


def set_active_canvas_id(canvas_id: str) -> bool:
    """Write the active-canvas marker. Returns True on success."""
    p = _active_canvas_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(canvas_id + "\n", encoding="utf-8")
        tmp.replace(p)
        return True
    except OSError as exc:
        logger.warning("canvas plugin: failed to write %s: %s", p, exc)
        return False


def clear_active_canvas() -> bool:
    """Remove the active-canvas marker. Idempotent — returns True
    even when the marker wasn't there to begin with."""
    p = _active_canvas_path()
    try:
        p.unlink(missing_ok=True)
        return True
    except OSError as exc:
        logger.warning("canvas plugin: failed to unlink %s: %s", p, exc)
        return False


# --- handlers ------------------------------------------------

def _failure_detail() -> str:
    """Append the canvas client's last_error_text() to a user-facing
    failure message so operators can distinguish auth / network /
    not-found cases without digging into hermes logs (see #15)."""
    err = canvas_client.last_error_text()
    return f" Reason: {err}." if err else ""


def _handle_list(_args: str) -> str:
    canvases = canvas_client.list_canvases()
    if canvases is None:
        return ("Couldn't reach devagentic. Check that it's running "
                "at $DEVAGENTIC_BASE_URL and that your X-User-Id is "
                "resolvable (see the canvas-plugin README)."
                + _failure_detail())
    if not canvases:
        return "No canvases yet. Create one with `/canvas new <name>`."
    lines = ["**Your canvases:**"]
    active = get_active_canvas_id()
    for c in canvases:
        cid = c.get("id") or "(no id)"
        name = c.get("name") or "(untitled)"
        desc = (c.get("description") or "").strip()
        mark = " ← active" if cid == active else ""
        line = f"- `{cid}` — **{name}**{mark}"
        if desc:
            line += f"  _{desc[:80]}_"
        lines.append(line)
    return "\n".join(lines)


def _handle_open(args: str) -> str:
    cid = (args or "").strip().split()[0] if args.strip() else ""
    if not cid:
        return ("Usage: `/canvas open <canvas-id>`. "
                "List your canvases with `/canvas list`.")
    canvas = canvas_client.get_canvas(cid)
    if canvas is None:
        return (f"Canvas `{cid}` not found, or devagentic is unreachable. "
                "Check `/canvas list`." + _failure_detail())
    if not set_active_canvas_id(cid):
        return f"Couldn't persist the active-canvas marker. Canvas not opened."
    name = (canvas.get("canvas") or {}).get("name") or "(untitled)"
    n_nodes = len(canvas.get("nodes") or [])
    n_edges = len(canvas.get("edges") or [])
    return (f"Opened canvas **{name}** (`{cid}`) — "
            f"{n_nodes} nodes, {n_edges} edges. State will be "
            f"injected into every turn until `/canvas close`.")


def _handle_close(_args: str) -> str:
    prior = get_active_canvas_id()
    clear_active_canvas()
    if prior:
        return f"Closed canvas `{prior}`. No canvas active."
    return "No canvas was active."


def _handle_show(_args: str) -> str:
    cid = get_active_canvas_id()
    if not cid:
        return "No canvas is active. Open one with `/canvas open <id>`."
    canvas = canvas_client.get_canvas(cid)
    if canvas is None:
        return (f"Active canvas marker is `{cid}` but devagentic is "
                "unreachable. The state-injection preamble will be empty "
                "until devagentic comes back." + _failure_detail())
    meta = canvas.get("canvas") or {}
    name = meta.get("name") or "(untitled)"
    desc = (meta.get("description") or "").strip()
    nodes = canvas.get("nodes") or []
    edges = canvas.get("edges") or []
    lines = [f"**Active canvas:** {name} (`{cid}`)"]
    if desc:
        lines.append(f"_{desc}_")
    lines.append(f"- {len(nodes)} nodes, {len(edges)} edges")
    if nodes:
        lines.append("- Node types: " + ", ".join(
            sorted({n.get("node_type") or "?" for n in nodes})))
    return "\n".join(lines)


def _handle_new(args: str) -> str:
    name = (args or "").strip()
    if not name:
        return "Usage: `/canvas new <name>`"
    canvas = canvas_client.create_canvas(name=name)
    if canvas is None:
        return ("Couldn't create canvas — devagentic unreachable or "
                "auth failed. Check the canvas-plugin README."
                + _failure_detail())
    cid = canvas.get("id") or "(no id)"
    return (f"Created canvas **{name}** (`{cid}`). "
            f"Open with `/canvas open {cid}`.")


_SUBCOMMANDS = {
    "list":  _handle_list,
    "open":  _handle_open,
    "close": _handle_close,
    "show":  _handle_show,
    "new":   _handle_new,
}


def canvas_command(raw_args: str) -> str:
    """Top-level `/canvas` handler. Dispatches on the first
    whitespace-separated token; defaults to `list` when no
    subcommand is given (matches hermes' UX where bare `/canvas`
    should still do something useful)."""
    raw = (raw_args or "").strip()
    if not raw:
        return _handle_list("")
    parts = raw.split(None, 1)
    sub = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""
    handler = _SUBCOMMANDS.get(sub)
    if handler is None:
        return (f"Unknown `/canvas` subcommand: `{sub}`. "
                f"Try: {', '.join(sorted(_SUBCOMMANDS))}.")
    try:
        return handler(rest)
    except Exception as exc:  # noqa: BLE001
        logger.warning("canvas plugin: /%s failed: %s", sub, exc)
        return f"`/canvas {sub}` failed: {exc}"
