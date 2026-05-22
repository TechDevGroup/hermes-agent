"""devagentic-docs plugin entrypoint (hermes issue #12).

Surfaces devagentic's writeDoc + searchDocs GraphQL primitives as
`/doc` slash commands. Sibling to devagentic-canvas — same env
conventions, same loss-tolerant contract, same enable/disable
semantics.

When disabled, the plugin is fully inert — no slash command is
registered. Hermes' other flows are unchanged.
"""
from __future__ import annotations

import logging

from . import commands as _commands


logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Plugin loader entrypoint. Wires the /doc slash command into
    the host's PluginContext."""
    ctx.register_command(
        name="doc",
        handler=_commands.doc_command,
        description=("Search, write, and show devagentic doc-graph "
                     "entries from inside a hermes session."),
        args_hint=("search <query> [--tag t] [--limit N] | "
                   "write <body> [--tags a,b] | show <id>"),
    )
