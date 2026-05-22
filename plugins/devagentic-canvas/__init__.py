"""devagentic-canvas plugin entrypoint (hermes issue #55).

Bundles three pieces:

  * `/canvas` slash commands (commands.py) — list / open / close /
    show / new. Operates over devagentic's `/v1/canvas/*` REST
    surface via canvas_client.py.
  * `pre_llm_call` preamble injection (preamble.py) — when a
    canvas is "open" in the session, fetches its state and
    surfaces it as ephemeral context for every turn.
  * `canvas` skill (skills/canvas/SKILL.md) — reference doc on
    canvas authoring conventions, surfaced via
    `skill_view('devagentic-canvas:canvas')`.

Enable/disable via `hermes plugin enable|disable devagentic-canvas`.
When disabled, the plugin is fully inert — no slash commands
registered, no hooks fired, no skill exposed. Hermes' other
flows are unchanged.

The plugin assumes devagentic is reachable at
`$DEVAGENTIC_BASE_URL` (default http://127.0.0.1:6071/v1) and
that an `X-User-Id` can be resolved (via
`$DEVAGENTIC_USER_ID` env or `hermes_cli.profiles.
get_active_profile_name()`). When neither is available, all
canvas operations return user-facing error strings — no crashes.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import commands as _commands, preamble as _preamble


logger = logging.getLogger(__name__)


_PLUGIN_DIR = Path(__file__).resolve().parent


def register(ctx) -> None:
    """Plugin loader entrypoint. Wires the /canvas slash command,
    the pre_llm_call preamble hook, and the canvas skill into the
    host's PluginContext."""
    # /canvas slash command
    ctx.register_command(
        name="canvas",
        handler=_commands.canvas_command,
        description=("Manage devagentic canvases: list / open / "
                     "close / show / new. When a canvas is open, "
                     "its state is injected into every turn until "
                     "you `/canvas close`."),
        args_hint="list | open <id> | close | show | new <name>",
    )

    # pre_llm_call preamble — only injects context when a canvas is
    # active (marker file present). Inert otherwise.
    ctx.register_hook("pre_llm_call", _preamble.on_pre_llm_call)

    # Plugin-scoped skill — resolvable as
    # `devagentic-canvas:canvas` via skill_view().
    skill_path = _PLUGIN_DIR / "skills" / "canvas" / "SKILL.md"
    if skill_path.is_file():
        try:
            ctx.register_skill(
                name="canvas",
                path=skill_path,
                description=("Canvas authoring conventions + the "
                             "devagentic-canvas plugin surface."),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "devagentic-canvas: failed to register skill: %s", exc)
    else:
        logger.debug(
            "devagentic-canvas: skill path %s missing; skipping skill "
            "registration", skill_path)
