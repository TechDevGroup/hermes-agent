"""hermes-github plugin entrypoint (G3 of devagentic#203 /
hermes-agent#57).

Exposes a hermes-native ``file_issue`` MCP tool so worker sessions
can surface stack gaps as GitHub issues on TechDevGroup/devagentic
or TechDevGroup/hermes-agent without ever editing stack source
(#203 §3.2).

The plugin holds NO credentials of its own — token resolution
happens on the hermes host (env vars or ``gh auth token``) so the
worker conversation never sees the token. Restricted by design to
the two TechDevGroup stack repos to keep blast radius bounded.

Enable/disable via ``hermes plugin enable|disable hermes-github``.
Inert when disabled.
"""
from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Plugin loader entrypoint (hermes-agent#78). MCP-only plugin —
    ``file_issue`` tool is registered server-side in
    ``mcp_serve.py::_register_github_tools``. Nothing to wire through
    the plugin loader directly.

    register() exists so the loader doesn't warn `no register()` and
    skip the plugin."""
    logger.debug("hermes-github: loaded (MCP-only; file_issue via mcp_serve)")
