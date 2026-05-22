---
name: canvas
version: 1.0.0
description: "Canvas authoring + workflow conventions when working with the devagentic-canvas plugin."
metadata:
  hermes:
    fallback_for_toolsets: []
    requires_toolsets: []
    fallback_for_tools: []
    requires_tools: []
---

# Canvas authoring (devagentic-canvas)

The `devagentic-canvas` plugin surfaces devagentic's `/v1/canvas/*`
REST API as in-session slash commands + a state-aware preamble.
When a canvas is open, every turn includes the canvas state as
ephemeral context — the model conditions on what's drawn without
the user re-typing it.

## When to use this skill

Surface this skill explicitly when:

* The user mentions "canvas", "graph", "diagram", or "wiring".
* The user is iterating on a multi-piece design (data flow,
  component graph, dispatch topology) that benefits from visual
  structure rather than a long thread.
* The conversation has accumulated >5 named concepts and could
  benefit from a canvas-shaped layout for shared reference.

## Slash command surface

| command | effect |
|---|---|
| `/canvas list` | list your canvases |
| `/canvas open <id>` | mark the canvas as active for the session — its state is injected into every turn until `/canvas close` |
| `/canvas close` | clear the active canvas marker |
| `/canvas show` | print the currently-active canvas id + a short state summary |
| `/canvas new <name>` | create a new canvas; prints the new id |

## State-aware preamble

When a canvas is open, the plugin's `pre_llm_call` hook fetches
the canvas state from devagentic and injects a compact summary
into the user message (NOT the system prompt — that would invalidate
the prompt cache). The summary lists:

* Canvas name + description.
* First ~12 nodes (`id` + `node_type`).
* First ~12 edges (`source` + `target` + `edge_type`).

For a 200-node canvas, only the head is surfaced — operators tune
the cap if their canvases trend large.

## Auth + base URL

The plugin reuses the devagentic-local provider's configuration:

* `DEVAGENTIC_BASE_URL` — default `http://127.0.0.1:6071/v1`.
* `DEVAGENTIC_API_KEY` — bearer (any value works under
  `DEVAGENTIC_TRUST_HEADER=1` on devagentic).
* `DEVAGENTIC_USER_ID` — manual override, or derived from the
  active hermes profile name via `hermes_cli.profiles.
  get_active_profile_name()`.

## Failure semantics

Every plugin path is loss-tolerant:

* Devagentic unreachable → slash commands return a user-facing
  error string; the `pre_llm_call` hook returns None so the turn
  proceeds without canvas context.
* No canvas open → hook is inert; bare `/canvas` falls through
  to `/canvas list`.
* `DEVAGENTIC_USER_ID` unresolvable → adapter returns None; hook
  is inert.

The plugin never raises out of a slash command or a hook, so a
broken devagentic doesn't brick the session.
