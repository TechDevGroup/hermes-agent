# devagentic-vertical-preamble

Auto-loads the active hermes profile's vertical context as a pre-LLM-call
preamble on session boot. Closes G1 of [devagentic#203](https://github.com/TechDevGroup/devagentic/issues/203).

## What it does

When a hermes session boots with `HERMES_HOME=~/.hermes/profiles/<name>`, the
profile name is the X-User-Id devagentic scopes per-user data to. On the
first LLM call of the process, this plugin queries devagentic's
`verticalContext(userId)` and injects the resulting rollup —
`kind:vertical-spec` + `kind:grafted-context` + `kind:worker-guardrails`
docs — as ephemeral preamble appended to the user message.

After the first successful load, the user_id is marked loaded and no
re-injection happens for the rest of the process. Session-stable docs
should not pay token cost per turn.

## Why

Under devagentic#203 §1.2, worker sessions must NOT read `genesis/doc.jsonl`
directly or shell out to `python api.py`. The vertical's manifest + grafted
material + guardrails need to land in the worker's context through a hermes-
native path. This plugin is that path.

Depends on devagentic exposing `verticalContext(userId)` GraphQL
(devagentic#204 / PR #205).

## Failure semantics

Any failure returns `None` and the turn proceeds without preamble:
- No resolvable user_id (no env override, no profile)
- The `"default"` profile (no vertical scoping)
- This user_id was already loaded in this process
- devagentic unreachable / network error
- GraphQL parse error or `errors` in response
- Empty rollup (no docs for this user_id) — marked loaded so we don't re-query

The plugin never blocks the turn.

## Caps

To keep the preamble bounded:
- Per grafted-context: 2KB content slice
- At most 8 grafts rendered
- Total preamble: 32KB max

Operators can re-tune via follow-up PR if their verticals trend large.

## Environment

Same vars as the other devagentic hermes plugins (memory, skills, canvas):
- `DEVAGENTIC_BASE_URL` — default `http://127.0.0.1:6071/v1`
- `DEVAGENTIC_API_KEY` — bearer; any non-empty value works under `DEVAGENTIC_TRUST_HEADER=1`
- `DEVAGENTIC_USER_ID` — manual override; otherwise resolved from active hermes profile

## Enable / disable

```
hermes plugin enable devagentic-vertical-preamble
hermes plugin disable devagentic-vertical-preamble
```

Inert when disabled — no hook fires, no network calls.
