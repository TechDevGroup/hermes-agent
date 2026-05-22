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

* `/doc search <query> [--tag <tag>] [--limit N]` — top hits from
  `searchDocs`. Lexical + embedding ranking; tag scopes to docs
  with that exact tag.
* `/doc write <body> [--tags t1,t2,...]` — persist a doc via
  `writeDoc`. Auto-tags with `source:hermes-cli` for downstream
  filtering.
* `/doc show <id>` — fetch a doc by id (top-1 search match,
  identity-verified).

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
├── plugin.yaml          # manifest (no hooks in MVP)
├── __init__.py          # register() entrypoint
├── client.py            # GraphQL client + last_error_text()
└── commands.py          # /doc slash command handlers
```

## Out of scope for MVP

* `/fork open <doc_id>` + `/fork close` (planned per #12; deferred
  to keep MVP small).
* `pre_llm_call` hook for pinned-doc context injection (planned
  per #12; deferred — needs the `/fork` marker file first).
* `/doc delete` — devagentic's doc graph is append-only by design;
  if a delete primitive lands, surface it then.
