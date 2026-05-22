# MCP canvas tools (issue #56)

Last main-pitch item. Exposes devagentic canvas operations as MCP
tools on hermes' existing `mcp_serve.py` so any MCP host — Claude
Desktop, Claude Code, Cursor, Codex — can manipulate the canvas
without opening a browser.

## Tools surfaced

| MCP tool | devagentic endpoint | shape |
|---|---|---|
| `canvas_list` | `GET /v1/canvases` | list user's canvases |
| `canvas_open` | `GET /v1/canvas/{id}` | full state (canvas + nodes + edges) |
| `canvas_add_node` | `POST /v1/canvas/{id}/nodes` | author a node; optional `position_x`/`position_y` |
| `canvas_move_node` | `PATCH /v1/canvas/{id}/nodes/{nid}` (position-only body) | reposition; takes `x`, `y` |
| `canvas_update_node` | `PATCH /v1/canvas/{id}/nodes/{nid}` | partial merge via `fields_json` (JSON object string) |
| `canvas_delete_node` | `DELETE /v1/canvas/{id}/nodes/{nid}` | drop a node |
| `canvas_link_nodes` | `POST /v1/canvas/{id}/edges` | author an edge with `edge_type` (default `links`) |
| `canvas_delete_edge` | `DELETE /v1/canvas/{id}/edges/{eid}` | drop an edge |
| `canvas_search` | client-side filter over `GET /v1/canvas/{id}` | substring search over node body / name / node_type |

Every tool returns a JSON string. Success returns the parsed
endpoint payload; failures return `{"error": "<msg>"}`. **No MCP
tool ever raises** — a broken devagentic surfaces to the host as a
structured error message, never as a transport-level crash.

## Authentication

The MCP tools reuse the same auth resolution as the
`devagentic-canvas` plugin (Phase D #55) and the devagentic-local
provider (Phase G #50):

| env var | purpose | default |
|---|---|---|
| `DEVAGENTIC_BASE_URL` | devagentic root URL | `http://127.0.0.1:6071/v1` |
| `DEVAGENTIC_API_KEY` | bearer token | empty (any non-empty value works under `DEVAGENTIC_TRUST_HEADER=1` on devagentic) |
| `DEVAGENTIC_USER_ID` | manual X-User-Id override | empty — falls back to `hermes_cli.profiles.get_active_profile_name()` |

Bearer is set on every devagentic request as
`Authorization: Bearer <token>`; the resolved user_id is set as
`X-User-Id: <name>`. Devagentic's static authenticator checks both
when trust-header mode is on.

## Wiring Claude Desktop

Add this to your `claude_desktop_config.json` (default location:
`~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS):

```json
{
  "mcpServers": {
    "hermes-canvas": {
      "command": "hermes",
      "args": ["mcp", "serve"],
      "env": {
        "DEVAGENTIC_BASE_URL": "http://127.0.0.1:6071/v1",
        "DEVAGENTIC_API_KEY": "any-non-empty-token",
        "DEVAGENTIC_USER_ID": "alice"
      }
    }
  }
}
```

Restart Claude Desktop. The 9 canvas tools (plus all the existing
hermes MCP tools: `conversations_list`, `messages_read`, etc.)
become available under the `hermes-canvas` server in Claude's tool
picker.

### Verifying the wiring

In a fresh Claude Desktop conversation, ask the model:

> List my devagentic canvases.

Claude should invoke `canvas_list`. If you see `{"error":
"devagentic unreachable..."}`, check:

1. **Devagentic is running** on `$DEVAGENTIC_BASE_URL`.
2. **Trust-header mode is on**:
   `DEVAGENTIC_TRUST_HEADER=1 python service.py …` on the devagentic
   side. Without it, the bearer token has to be the salted value
   matching `$DEVAGENTIC_USER_ID`.
3. **`X-User-Id` is resolvable**: either set `DEVAGENTIC_USER_ID`
   explicitly (recommended for Claude Desktop, which doesn't know
   about hermes profiles) or set `HERMES_HOME` to a profile dir.

## Wiring Cursor / Codex / other MCP hosts

The configuration shape is host-specific but the contract is the
same: spawn `hermes mcp serve` with `DEVAGENTIC_*` env set. For
Cursor, the equivalent block goes in
`~/.cursor/mcp.json`. For Codex CLI, the binding lives in
`.codex/config.toml` under `[[mcpServers]]`.

## Tool reference

### `canvas_list`

No arguments. Returns:

```json
{"count": 2,
 "canvases": [
   {"id": "canvas-abc", "name": "Lane B design", ...},
   {"id": "canvas-xyz", "name": "Dispatch topology", ...}
 ]}
```

### `canvas_open(canvas_id)`

Returns the full canvas envelope:

```json
{"canvas":  {"name": "...", "description": "...", ...},
 "nodes":   [{"id": "n1", "node_type": "doc", ...}, ...],
 "edges":   [{"id": "e1", "source": "n1", "target": "n2", "edge_type": "links"}, ...]}
```

### `canvas_add_node(canvas_id, node_type, position_x?, position_y?)`

Authors a node. `node_type` is the kind label (`doc`, `assertion`,
`decision`, etc.). `position_x` / `position_y` are optional — omit
both to let devagentic position the node on its default grid.

### `canvas_move_node(canvas_id, node_id, x, y)`

Reposition only. Equivalent to
`canvas_update_node` with `fields_json='{"position": {"x":..., "y":...}}'`,
but exposed separately so the MCP picker surfaces a clear "move"
verb to the model.

### `canvas_update_node(canvas_id, node_id, fields_json)`

Partial node update. `fields_json` is a JSON-encoded string of the
merge payload. Examples:

```jsonc
// Change the node's kind
'{"node_type": "decision"}'

// Update the body content
'{"body": {"content": "Tightened the auth flow rationale."}}'

// Combined
'{"node_type": "decision", "body": {"content": "..."}}'
```

Invalid JSON returns `{"error": "fields_json is not valid JSON: ..."}`.

### `canvas_delete_node(canvas_id, node_id)`

Drops the node. Devagentic's cascade behavior governs what happens
to incident edges (varies by node_type — consult the devagentic
canvas REST docs).

### `canvas_link_nodes(canvas_id, source_id, target_id, edge_type="links")`

Authors an edge. `edge_type` defaults to `links`; customize for
`depends-on`, `derived-from`, `supersedes`, etc.

### `canvas_delete_edge(canvas_id, edge_id)`

Drops the edge.

### `canvas_search(canvas_id, query)`

Substring search (case-insensitive) over each node's `body`,
`name`, and `node_type`. v0 is client-side: fetches the full
canvas state and filters in Python. Server-side search is deferred
(devagentic doesn't ship a `/search` endpoint yet).

Returns:

```json
{"count": 2,
 "matches": [{"id": "n3", "node_type": "decision", ...}, ...]}
```

## Failure modes

| failure | tool response |
|---|---|
| Plugin missing (no `plugins/devagentic-canvas/client.py`) | `{"error": "canvas plugin not available on this hermes install ..."}` |
| Devagentic unreachable | `{"error": "devagentic unreachable or auth failed; check ..."}` |
| Canvas / node / edge not found | `{"error": "canvas <id> not found or devagentic unreachable"}` (the resolver can't tell these apart at the HTTP layer) |
| Invalid `fields_json` | `{"error": "fields_json is not valid JSON: ..."}` |
| Missing required arg | `{"error": "canvas_id and node_id are required"}` (etc.) |

## Where this fits

| phase | shipped |
|---|---|
| Phase A (#41/#43/#44) | Canvas REST surface in devagentic |
| Phase D #55 | hermes plugin: `/canvas` slash commands + preamble |
| **Phase D #56 (this PR)** | MCP tools exposing the canvas to any MCP host |

After this PR, the full main-pitch demo is reachable:

* Devagentic holds the canvas state in the per-user vertical.
* Hermes opens a canvas via `/canvas open`; the preamble injects
  state on every turn (#55).
* Claude Desktop drives the canvas through `canvas_*` MCP tools
  (#56). Hermes runs as the MCP server bridging Claude Desktop
  to devagentic.

## Deferred

* **OAuth bearer-introspection for MCP hosts.** v0 trusts the
  bearer passed via `DEVAGENTIC_API_KEY` env. A future PR could
  add MCP-host-side OAuth so per-user tokens come from the host's
  auth flow.
* **Server-side `canvas_search`.** v0 fetches the full canvas
  state and filters client-side. When devagentic ships
  `/v1/canvas/{id}/search`, the MCP tool will swap to the
  server-side endpoint.
* **Streaming tool results.** v0 returns one JSON blob per tool
  call. The `canvas_open` of a large canvas could SSE-stream
  node-by-node; deferred until MCP hosts widely support streaming
  tool results.
* **Computed-view tools.** `canvas_export`, `canvas_render_png`,
  `canvas_summary` — wrappers over the existing devagentic
  `/v1/canvas/{id}/export` endpoint that A4 wired. Useful for
  Claude Desktop "show me what this canvas looks like" flows.
