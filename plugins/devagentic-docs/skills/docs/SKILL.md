---
name: docs
version: 1.0.0
description: "Doc-graph authoring + fork-injection conventions when working with the devagentic-docs plugin."
metadata:
  hermes:
    fallback_for_toolsets: []
    requires_toolsets: []
    fallback_for_tools: []
    requires_tools: []
---

# Doc-graph authoring (devagentic-docs)

The `devagentic-docs` plugin surfaces devagentic's `writeDoc` /
`searchDocs` / `forkContext` / `decorateContext` / `renderContext`
GraphQL primitives as in-session slash commands. When a fork is
open, every turn includes the fork's `renderContext` output as
ephemeral background context — the model conditions on the pinned
material without the user re-pasting it.

Sibling to the `devagentic-canvas` plugin. Same env vars, same
enable/disable contract, same loss-tolerant failure semantics.

## When to use this skill

Surface this skill explicitly when:

* The user mentions "doc", "finding", "note", "audit trail", or
  "knowledge graph" in the devagentic sense.
* The conversation has produced a durable finding that should be
  persisted for future sessions to retrieve (use `/doc write`).
* The user wants to ground the next turn on prior work that lives
  in devagentic's doc graph (use `/fork open <doc_id>` + the
  `pre_llm_call` hook will inject the rendered context).

## Slash command surface

### `/doc` — search, write, show

| command | effect |
|---|---|
| `/doc search <query> [--tag <t>] [--limit N]` | top hits. With `--tag`, routes to `Query.docs(tags:[t])` and filters by query substring client-side; without `--tag`, routes to `Query.searchDocs(query, k=limit)` (lexical + embedding ranking). |
| `/doc write <body> [--tags a,b,c]` | persist a doc via `writeDoc`. Auto-tags with `source:hermes-cli`. |
| `/doc show <id>` | fetch a doc by id (top-1 search match, identity-verified). |

### `/fork` — context branching + injection

| command | effect |
|---|---|
| `/fork open <parent_id> [--goal "..."]` | wrap `forkContext` to derive a context from a doc/context id. Sets a session-local marker at `$HERMES_HOME/docs-fork-active`. The parent is auto-pinned; `--goal` is stored as a `goal` annotation. |
| `/fork close` | clear the active-fork marker. |
| `/fork show` | print the active fork's tags + annotations. |
| `/fork pin <doc_id>` | append a `pinned-doc` annotation to the active fork via `decorateContext`. |
| `/fork render` | call `renderContext(ctxId)` and print the result inline (debugging aid for the preamble injection). |

## State-aware preamble

When a fork is open, the plugin's `pre_llm_call` hook calls
`renderContext(ctxId)` and appends the result to the user message
as ephemeral context. Cached prompt prefixes stay valid (the
context goes on the user side, not the system prompt).

Capped at 8000 chars by the hermes-side ceiling; devagentic's
`renderContext` does its own bounding upstream.

## Authoring convention: tag schema

When writing docs from inside hermes, prefer the same tag schema
the orchestrator's autonomous loops already use, so doc-graph
queries can mix sources cleanly:

* `source:hermes-cli` — auto-applied by `/doc write` so docs
  authored from inside a CLI session are distinguishable from
  worker-authored finds.
* `user:<your-id>` — namespacing per `DEVAGENTIC_USER_ID` /
  active profile.
* `kind:<topic>` — short topic label (e.g. `kind:finding`,
  `kind:question`, `kind:proposal`).

## Auth + base URL

Same as `devagentic-canvas`:

* `DEVAGENTIC_BASE_URL` — default `http://127.0.0.1:6071/v1`. The
  plugin strips a trailing `/v1` so it can compose `<root>/graphql`.
* `DEVAGENTIC_API_KEY` — bearer (any value works under
  `DEVAGENTIC_TRUST_HEADER=1` on devagentic).
* `DEVAGENTIC_USER_ID` — manual override, or derived from the
  active hermes profile.

## Transport caveat (`/graphql` over HTTP)

This plugin only works against devagentic deployments that expose
`/graphql` over HTTP. Some canonical deployments expose REST
(e.g. `/v1/canvases`, used by `devagentic-canvas`) but **not**
GraphQL. In that case every `/doc` and `/fork` command surfaces
`Reason: not found at <url>/graphql.` See
[hermes-agent#21](https://github.com/TechDevGroup/hermes-agent/issues/21).

Verify with:

```bash
curl -X POST <url>/graphql \
     -H "Content-Type: application/json" \
     -H "X-User-Id: <your-user-id>" \
     -d '{"query":"{__typename}"}'
```

A `200` with `{"data":{"__typename":"Query"}}` means the surface
is present. A `404` means it isn't — fall back to the orchestrator's
SSH `api.py` path for now (`ssh dev@devbox 'cd /home/dev/projects
&& .venv/bin/python api.py …'`) until the upstream transport ships.

## Failure semantics

Every plugin path is loss-tolerant:

* Devagentic unreachable / `/graphql` 404 → slash commands return
  a user-facing error string with a `Reason:` clause naming the
  specific failure (auth / 404 / unreachable / parse / no
  user_id). The `pre_llm_call` hook returns None and the turn
  proceeds without preamble.
* No fork open → hook is inert; `/fork show` says "No fork is
  active."
* `DEVAGENTIC_USER_ID` unresolvable → client surfaces
  `could not resolve user_id — set DEVAGENTIC_USER_ID …`.

The plugin never raises out of a slash command or a hook, so a
broken devagentic doesn't brick the session.
