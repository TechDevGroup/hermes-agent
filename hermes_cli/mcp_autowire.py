"""Auto-wire the bundled mcp_serve as a worker-session MCP server (#82).

Inserts a ``hermes-internal`` entry under ``mcp_servers`` in the active
profile's config.yaml so the worker's MCP client picks up + connects to
the bundled ``mcp_serve`` over stdio on session boot. The entry runs the
SAME Python interpreter that owns the worker process (``sys.executable -m
mcp_serve``), avoiding PATH lookups + venv mismatches.

Without this, the post-#79/#81 plugins (G2 devagentic-mutations, G3
hermes-github, G4 devagentic-lane-h) load but expose zero callable tools
because their tool implementations live server-side in ``mcp_serve.py``
and no MCP server is configured to host them.

Contract (#82 acceptance):
- Fresh worker session writes the entry once.
- Repeat boots are no-ops (idempotent).
- Existing ``mcp_servers`` entries (Linear, Notion, operator-added) are
  preserved.
- Opt-out: ``HERMES_DISABLE_INTERNAL_MCP=1`` skips the write entirely.
- Safe on partial installs: if ``mcp_serve`` isn't importable, skip.

Call site: ``tui_gateway/entry.py::main()``, before the gate that
short-circuits ``discover_mcp_tools()`` when ``mcp_servers`` is empty.
"""
from __future__ import annotations

import logging
import os
import sys
from importlib.util import find_spec
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

INTERNAL_SERVER_NAME = "hermes-internal"


def _opted_out() -> bool:
    """``HERMES_DISABLE_INTERNAL_MCP=1`` (or ``true`` / ``yes``) skips."""
    raw = (os.environ.get("HERMES_DISABLE_INTERNAL_MCP") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _mcp_serve_importable() -> bool:
    """Skip when ``mcp_serve`` isn't on sys.path — packaging gap (#80/#81)
    would otherwise leave a stale entry that immediately fails to spawn."""
    try:
        return find_spec("mcp_serve") is not None
    except (ImportError, ValueError):
        return False


def _build_internal_entry() -> Dict[str, Any]:
    """Construct the stdio server entry for the bundled mcp_serve.

    Uses ``sys.executable`` (the interpreter running this worker) rather
    than literal ``"python"`` so the entry survives venv-not-on-PATH
    deployments (e.g., container installs where /opt/hermes/venv/bin
    isn't on PATH in subprocess env)."""
    return {
        "command": sys.executable,
        "args": ["-m", "mcp_serve"],
        # Don't inherit interactive TTY state into the subprocess; stdio
        # MCP transport uses our stdin/stdout for protocol traffic.
        "env": {},
    }


def ensure_internal_mcp_server(
    *, config: Optional[Dict[str, Any]] = None
) -> bool:
    """Idempotently write the ``hermes-internal`` MCP server entry.

    Returns ``True`` if a write happened, ``False`` if skipped (opt-out,
    not importable, or entry already present).

    ``config`` is for tests; production callers pass nothing.
    """
    if _opted_out():
        logger.debug("HERMES_DISABLE_INTERNAL_MCP set — skipping auto-wire")
        return False

    if not _mcp_serve_importable():
        logger.debug("mcp_serve not importable — skipping auto-wire")
        return False

    try:
        from hermes_cli.config import load_config, save_config
    except Exception as exc:  # pragma: no cover — packaging guard
        logger.debug("hermes_cli.config unavailable: %s", exc)
        return False

    cfg = config if config is not None else load_config()
    if not isinstance(cfg, dict):
        return False

    servers = cfg.get("mcp_servers")
    if not isinstance(servers, dict):
        servers = {}
        cfg["mcp_servers"] = servers

    if INTERNAL_SERVER_NAME in servers:
        # Already present — respect operator edits (custom args, env).
        return False

    servers[INTERNAL_SERVER_NAME] = _build_internal_entry()

    if config is None:
        # Production path: persist via the canonical writer.
        try:
            save_config(cfg)
        except Exception as exc:  # pragma: no cover — disk/permission guard
            logger.warning(
                "auto-wire of %s failed at save_config: %s",
                INTERNAL_SERVER_NAME, exc,
            )
            return False

    logger.info(
        "auto-wired %s MCP server (command=%s -m mcp_serve)",
        INTERNAL_SERVER_NAME, sys.executable,
    )
    return True
