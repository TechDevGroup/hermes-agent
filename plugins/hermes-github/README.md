# hermes-github

Worker-facing GitHub issue filer. Closes G3 of
[TechDevGroup/devagentic#203](https://github.com/TechDevGroup/devagentic/issues/203).

Workers in a vertical session must NOT edit stack source (`devagentic` /
`hermes-agent`). Per #203 §3.2 they surface stack gaps as GitHub issues
via a hermes-native tool — that's this plugin.

## MCP tool surface

| Tool         | Args                                       | Returns |
|--------------|--------------------------------------------|---------|
| `file_issue` | `repo`, `title`, `body`, `labels?`         | `{number, url, html_url, title, state}` or `{error}` |

`repo` is restricted to the two stack repos:

- `devagentic`  → opens issue on `TechDevGroup/devagentic`
- `hermes-agent` → opens issue on `TechDevGroup/hermes-agent`

Any other value returns `{"error": "..."}`. Operators who want to widen
the allowed set file a follow-up issue + extend `_ALLOWED_REPOS` in
`client.py`.

## Auth model

The plugin holds NO credentials. Token resolution happens on the
hermes host in priority order:

1. `HERMES_GH_TOKEN` env var — dedicated hermes-side override.
2. `GITHUB_TOKEN` / `GH_TOKEN` env vars — standard PAT env.
3. `gh auth token` subprocess — when the `gh` CLI is on PATH and
   the operator has logged in (`gh auth login`).

The token is NEVER written to logs and NEVER reaches the worker's
conversation. Only the outbound `api.github.com` request sees it.

If no token is available, `file_issue` returns
`{"error": "no GitHub token available — set HERMES_GH_TOKEN / GITHUB_TOKEN / GH_TOKEN, or run `gh auth login` on the hermes host"}`
so the worker gets actionable diagnostics.

## Failure semantics

`file_issue` returns `None` on any failure (the MCP wrapper translates
to `{"error": ...}` JSON). Failures include:

- Invalid / disallowed `repo`
- Empty `title` or `body`
- Missing token
- GitHub API HTTP error (4xx / 5xx) — the response body is included
  in the error string (truncated to 200 chars)
- Network unreachable

`last_error_text()` carries the most recent failure message for the
MCP wrapper to surface.

## Required token scope

To open issues, the token needs the `repo` scope (for private repos)
or `public_repo` (for public). For TechDevGroup/devagentic +
TechDevGroup/hermes-agent (private), use `repo`.

## Enable / disable

```
hermes plugin enable hermes-github
hermes plugin disable hermes-github
```

When disabled, the MCP tool is not registered. Workers attempting to
file an issue from a disabled-plugin host will see the absent-tool
error from their LLM provider.
