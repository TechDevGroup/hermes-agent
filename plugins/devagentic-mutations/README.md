# devagentic-mutations

Exposes devagentic graph mutations + queries as hermes MCP tools so worker
sessions never shell out to `python api.py` or read `genesis/doc.jsonl`
directly (TechDevGroup/devagentic#203 §1.2 invariant).

## Phase 1 surface (this PR — hermes-agent#56 / G2 of devagentic#203)

| MCP tool      | Devagentic resolver              | Purpose |
|---------------|----------------------------------|---------|
| `silo_query`  | `querySilo` field                | Run a one-shot prompt against a named silo. Returns text + token-usage stats. |
| `confer_run`  | `runConferLoop` mutation         | Confer 3-4 free-tier silos on a `kind:finding-candidate`; returns rolled-up consensus + per-silo response ids. |

`writeDoc` is already exposed via `plugins/devagentic-docs/` + the
`doc_write` MCP tool registered in `mcp_serve._register_docs_tools`. This
plugin does NOT duplicate it.

`forkContext` is already exposed via the `fork_*` MCP tools in
`_register_docs_tools` (`fork_open`, `fork_decorate`, `fork_get`,
`fork_render`).

## Roadmap

Sub-issues tracked under #56:

- **assertOutput** MCP tool (needs `ExpectationInput` shape mapping)
- **patchArtifact / readArtifact** MCP tools
- **fetchUrl** MCP tool

These will be filed as separate hermes-agent sub-issues and shipped in
follow-up PRs against #56.

## Registration

The plugin module provides `client.py` (the GraphQL transport).
MCP tool registration lives in `mcp_serve.py::_register_devagentic_mutation_tools`,
called from `create_mcp_server()` alongside the canvas + docs tool
registrations. Mirrors the canvas-plugin pattern.

## Failure semantics

Every client function returns `None` on any failure:
- No resolvable user_id
- devagentic unreachable / network error
- GraphQL parse error or `errors` in response
- Missing/null `data` field
- Empty required args

Populates `last_error_text()` with a short human-readable kind so MCP tool
wrappers can surface actionable diagnostics in their `{"error": "..."}`
strings.

## Environment

Same vars as the other devagentic-adjacent hermes pieces:

- `DEVAGENTIC_BASE_URL` — default `http://127.0.0.1:6071/v1`
- `DEVAGENTIC_API_KEY` — bearer (any non-empty under `DEVAGENTIC_TRUST_HEADER=1`)
- `DEVAGENTIC_USER_ID` — manual override; otherwise resolved from active hermes profile
