# devagentic-lane-h

Worker-facing read surface for Lane H reasoning-graft-candidate docs.
Closes G4 of [TechDevGroup/devagentic#203](https://github.com/TechDevGroup/devagentic/issues/203).

## What is Lane H?

Lane H is the **reasoning-graft signal layer** (#174). When devagentic's
cycle_tick produces a high-confidence confer-result, it auto-emits a
`kind:reasoning-graft-candidate` doc as a marker. A downstream consumer
(operator, scaffold matcher, future H7-gated executor) decides whether
to actually apply a graft. Per devagentic#203 §3.2, when a worker needs
a paid-tier reasoning lift, it triggers conferring (via the existing
`confer_run` MCP tool from G2) and reads the resulting graft candidates
through THIS plugin.

There is no `requestReasoningGraft` tool because Lane H is event-driven,
not request-response. The pipeline is:

```
worker → confer_run → high-confidence result → cycle_tick auto-emits
                                                kind:reasoning-graft-candidate
                                              ↓
                                        worker reads via lane_h_list / lane_h_fetch
```

## MCP tool surface

| Tool          | Args                       | Purpose |
|---------------|----------------------------|---------|
| `lane_h_list` | `user_id?`, `limit?` (def. 20) | List a user's recent Lane H candidates newest-first. Falls back to the active profile's resolved id when `user_id` is empty. |
| `lane_h_fetch`| `graft_id`                 | Fetch a single Lane H doc by id; validates the `kind:reasoning-graft-candidate` tag. |

## Returned shape

`lane_h_list` returns a list of dicts:

```json
{
  "id": "doc-...",
  "userId": "polynomial-explorer",
  "content": "reasoning-graft candidate (confer-result ..., candidate ..., confidence 0.94). ...",
  "ts": "2026-05-24T...",
  "conferResultRef": "doc-...",
  "candidateId": "doc-...",
  "conferConfidence": 0.94,
  "scaffoldMatchStatus": "not_evaluated",
  "h7GateStatus": "not_evaluated"
}
```

`lane_h_fetch` returns the same dict shape but for a single id (after kind validation).

## Failure semantics

Both functions return `None` on any failure:
- No resolvable user_id (`lane_h_list`)
- Empty / missing graft_id (`lane_h_fetch`)
- devagentic unreachable / network error
- GraphQL parse error or `errors` in response
- Top hit's id mismatch (`lane_h_fetch`)
- Top hit isn't a `kind:reasoning-graft-candidate` doc (`lane_h_fetch`)

`last_error_text()` carries a short diagnostic the MCP wrapper surfaces in
its `{"error": "..."}` JSON.

## Dependency

Requires devagentic to expose `reasoningGraftCandidates(userId, limit?)`
GraphQL field — landed in devagentic#207 / PR #207.

## Environment

Same vars as the other devagentic-adjacent plugins:

- `DEVAGENTIC_BASE_URL` — default `http://127.0.0.1:6071/v1`
- `DEVAGENTIC_API_KEY` — bearer
- `DEVAGENTIC_USER_ID` — manual override; otherwise resolved from active hermes profile

## Enable / disable

```
hermes plugin enable devagentic-lane-h
hermes plugin disable devagentic-lane-h
```

When disabled, the MCP tools are not registered.
