# devagentic-docs plugin

Hermes plugin that surfaces devagentic's GraphQL `writeDoc` /
`searchDocs` primitives as `/doc` slash commands inside a hermes
session.

Sibling to [`devagentic-canvas`](../devagentic-canvas/README.md).
Same env conventions, same loss-tolerant failure semantics, same
plugin enable/disable contract.

## Enable / disable

```bash
hermes plugin enable devagentic-docs
hermes plugin disable devagentic-docs
hermes plugin list
```

When disabled, the plugin is fully inert — no `/doc` command is
registered.

## Surface

* `/doc search <query> [--tag <tag>] [--limit N]` — top hits. When
  `--tag` is set, routes to `Query.docs(tags:[t])` (the tag-scoped
  browser) and filters by query client-side. Otherwise routes to
  `Query.searchDocs(query, k=limit)` (lexical + embedding ranking).
* `/doc write <body> [--tags t1,t2,...]` — persist a doc via
  `writeDoc`. Auto-tags with `source:hermes-cli`.
* `/doc show <id>` — fetch by id (top-1 search match, identity-
  verified).

### Fork surface (v0.2)

* `/fork open <parent_id> [--goal "..."]` — wrap `forkContext` to
  derive a context from a doc/context id. Sets a session-local
  active-fork marker at `$HERMES_HOME/docs-fork-active`. The
  parent is auto-pinned and the goal (if set) is stored as a
  `goal` annotation.
* `/fork close` — clear the active-fork marker.
* `/fork show` — print the active fork's tags + annotations.
* `/fork pin <doc_id>` — append a `pinned-doc` annotation to the
  active fork via `decorateContext`.
* `/fork render` — call `renderContext(ctxId)` and print the
  result inline. Useful for debugging the preamble injection.

### Preamble injection

When a fork is active, the plugin's `pre_llm_call` hook calls
`renderContext(ctxId)` and appends the result to the user message
as ephemeral context. Cached prompt prefixes stay valid (the
context goes on the user side, not the system prompt).

Capped at 8000 chars by the hermes-side ceiling; devagentic's
`renderContext` does its own bounding upstream.

## Configuration

Reuses the devagentic-local provider's environment, same as
`devagentic-canvas`:

| env var | default | purpose |
|---|---|---|
| `DEVAGENTIC_BASE_URL` | `http://127.0.0.1:6071/v1` | devagentic root; GraphQL lives at `<root-without-/v1>/graphql` |
| `DEVAGENTIC_API_KEY` | _(empty)_ | bearer token; any non-empty value works when devagentic runs in `DEVAGENTIC_TRUST_HEADER=1` mode |
| `DEVAGENTIC_USER_ID` | _(empty)_ | manual override for the `X-User-Id` header; falls back to `hermes_cli.profiles.get_active_profile_name()` |

## Failure semantics

Every code path is loss-tolerant:

* Devagentic unreachable → slash commands return a user-facing
  error string with a `Reason:` clause naming the specific failure
  (auth / 404 / unreachable / parse / no user_id).
* Empty result set → user-facing "no docs matched" message;
  not a failure.
* The plugin **never raises** out of a slash command — a broken
  devagentic doesn't brick the session.

## Layout

```
plugins/devagentic-docs/
├── plugin.yaml          # manifest (declares pre_llm_call hook)
├── __init__.py          # register() entrypoint
├── client.py            # GraphQL client + last_error_text()
├── commands.py          # /doc + /fork handlers
└── preamble.py          # pre_llm_call hook
```

## Out of scope (still)

* `/doc delete` — devagentic's doc graph is append-only by design;
  if a delete primitive lands upstream, surface it then.
* Fork branching from a *context* id rather than a doc id —
  `forkContext` accepts any parent id; the slash command surface
  treats them uniformly, but no helper exists for "fork the last
  conversation" yet.
