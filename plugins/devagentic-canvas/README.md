# devagentic-canvas plugin

Hermes plugin that surfaces devagentic's `/v1/canvas/*` REST API
as in-session slash commands plus a state-aware preamble (Phase D
hermes integration; devagentic issue #55).

## Enable / disable

```bash
hermes plugin enable devagentic-canvas
hermes plugin disable devagentic-canvas
hermes plugin list                      # verify
```

When disabled, the plugin is fully inert — no slash commands, no
hooks fired, no skill exposed. Hermes' other flows are unchanged.

## Surface

* **`/canvas` slash commands**:
  * `/canvas list` — list your canvases.
  * `/canvas open <id>` — mark canvas as active for the session.
  * `/canvas close` — clear the active marker.
  * `/canvas show` — currently-active canvas summary.
  * `/canvas new <name>` — create a new canvas.

* **`pre_llm_call` hook**: when a canvas is open, fetches its state
  and injects a compact summary as ephemeral context on every
  turn (appended to the user message, not the system prompt — keeps
  prompt cache valid).

* **`devagentic-canvas:canvas` skill**: reference doc on canvas
  authoring + when to surface a canvas-shaped layout. Plugin
  skills are opt-in explicit loads; not in the system-prompt
  `<available_skills>` index.

## How the active-canvas marker works

`/canvas open <id>` writes a single-line file at
`$HERMES_HOME/canvas-active` (default `~/.hermes/canvas-active`)
containing the canvas id. The `pre_llm_call` hook reads this file
on every turn; presence + readability is the gate.

`/canvas close` unlinks the file. The marker survives process
restart, so a long-running session keeps the canvas open across
reconnects without re-issuing `/canvas open`.

## Configuration

The plugin reuses the devagentic-local provider's environment:

| env var | default | purpose |
|---|---|---|
| `DEVAGENTIC_BASE_URL` | `http://127.0.0.1:6071/v1` | devagentic root URL |
| `DEVAGENTIC_API_KEY` | _(empty)_ | bearer token; any non-empty value works when devagentic runs in `DEVAGENTIC_TRUST_HEADER=1` mode |
| `DEVAGENTIC_USER_ID` | _(empty)_ | manual override for the X-User-Id header; falls back to `hermes_cli.profiles.get_active_profile_name()` |
| `HERMES_HOME` | `~/.hermes` | location of the `canvas-active` marker file |

## Failure semantics

Every plugin path is loss-tolerant:

* Devagentic unreachable → slash commands return a user-facing
  error string; the `pre_llm_call` hook returns None so the turn
  proceeds without canvas context.
* No canvas open → hook is inert.
* `DEVAGENTIC_USER_ID` unresolvable → adapter returns None;
  hook + commands surface a clear error.

The plugin **never raises** out of a slash command or a hook —
a broken devagentic doesn't brick the session.

## Layout

```
plugins/devagentic-canvas/
├── plugin.yaml          # manifest (hooks: pre_llm_call)
├── __init__.py          # register() entrypoint
├── client.py            # HTTP client for /v1/canvas/*
├── commands.py          # /canvas slash command handlers
├── preamble.py          # pre_llm_call hook
├── README.md            # this file
└── skills/
    └── canvas/
        └── SKILL.md     # canvas authoring conventions
```
