"""Phase D devagentic-graph memory adapter (devagentic issue #54).

Talks to a running devagentic over HTTP to resolve user-facts from
graph nodes (kind:user-fact) before hermes falls back to its
~/.hermes/MEMORY.md / USER.md / SOUL.md files.

Opt-in via env `DEVAGENTIC_MEMORY_GRAPH=1`. Default off keeps the
file-based flow byte-stable. The migration script
`scripts/migrate_memory_to_graph.py` populates the graph from
existing files; once enabled, `query_user_facts(query_text)`
returns relevance-ranked facts the caller can splice into a
system prompt or memory rollup.

Env vars:
  DEVAGENTIC_MEMORY_GRAPH  set to `1` to enable graph reads
                            (default: off). When off,
                            query_user_facts always returns
                            []; callers fall straight to files.
  DEVAGENTIC_BASE_URL       devagentic base URL (default
                            http://127.0.0.1:6071/v1). Reused
                            from the devagentic-local provider
                            so a single configuration covers
                            skills, memory, and completions.
  DEVAGENTIC_API_KEY        bearer token forwarded to devagentic.
                            With DEVAGENTIC_TRUST_HEADER=1 on
                            devagentic, any non-empty value works.

Failure semantics: every code path that touches the network is
wrapped in try/except. Resolver returns an empty list on any
failure (network, parse, no facts, etc.); callers MUST keep their
existing file fallback so a transient devagentic outage doesn't
brick memory retrieval.

Transport caveat: this adapter assumes devagentic exposes
`/graphql` over HTTP. Some canonical deployments don't (see
TechDevGroup/hermes-agent#21); query_user_facts returns []
silently on every call there. Run `hermes doctor` for a probe
+ actionable hint when graph mode is enabled.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Optional


logger = logging.getLogger(__name__)


GRAPH_ENV = "DEVAGENTIC_MEMORY_GRAPH"

# Network timeout. Generous on a local loopback; faster than that
# and a slow devagentic startup would race the caller's file
# fallback.
_DEFAULT_TIMEOUT = 8.0


def graph_enabled() -> bool:
    """True iff `DEVAGENTIC_MEMORY_GRAPH` is in `1|true|yes|on`."""
    return os.environ.get(GRAPH_ENV, "0").strip().lower() in (
        "1", "true", "yes", "on")


def _base_url() -> str:
    raw = os.environ.get("DEVAGENTIC_BASE_URL", "http://127.0.0.1:6071/v1")
    base = raw.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def _api_key() -> str:
    return (os.environ.get("DEVAGENTIC_API_KEY") or "").strip()


def _user_id() -> Optional[str]:
    """Resolve the X-User-Id to send.

    Same precedence as the devagentic-local provider plugin
    (Phase G #50) and the skill adapter (Phase C #52):
      1. `DEVAGENTIC_USER_ID` env override.
      2. `hermes_cli.profiles.get_active_profile_name()`.
      3. None — caller doesn't inject the header; adapter returns
         empty.
    """
    override = (os.environ.get("DEVAGENTIC_USER_ID") or "").strip()
    if override:
        return override
    try:
        from hermes_cli.profiles import get_active_profile_name
        name = (get_active_profile_name() or "").strip()
        return name or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("devagentic_memory: profile resolution failed: %s", exc)
        return None


def _post_graphql(query: str, variables: dict,
                  *, timeout: float = _DEFAULT_TIMEOUT) -> Optional[dict]:
    """POST a GraphQL query to devagentic. Returns parsed `data`
    on success, None on any failure (network, non-200, parse error,
    GraphQL error). Failures log at DEBUG only."""
    base = _base_url()
    user = _user_id()
    if not user:
        logger.debug("devagentic_memory: no user_id resolved; skipping")
        return None
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/graphql", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("X-User-Id", user)
    api_key = _api_key()
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        logger.debug("devagentic_memory: request failed: %s", exc)
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.debug("devagentic_memory: parse failed: %s", exc)
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("errors"):
        logger.debug("devagentic_memory: graphql errors: %s",
                     payload.get("errors"))
        return None
    return payload.get("data") or None


def query_user_facts(
    query: str,
    *,
    top_k: int = 10,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[dict]:
    """Relevance-rank user-facts from the devagentic graph.

    Returns a list of dicts shaped `{id, body, tags, source,
    confidence}` — keys mirror the GraphQL UserFact type. Empty
    list on:
      * gate off (`DEVAGENTIC_MEMORY_GRAPH` unset / `0`)
      * empty query
      * no user_id resolvable
      * network / parse error
      * no matching facts

    Callers MUST keep their existing file fallback; this is a
    passive read-through, not a hard dependency."""
    if not graph_enabled():
        return []
    if not query:
        return []
    gql = (
        "query($u:String!,$q:String!,$k:Int)"
        "{userFactQuery(userId:$u,query:$q,topK:$k)"
        "{id body tags source confidence}}"
    )
    user = _user_id()
    if not user:
        return []
    data = _post_graphql(
        gql, {"u": user, "q": query, "k": top_k}, timeout=timeout)
    if data is None:
        return []
    facts = data.get("userFactQuery") or []
    if not isinstance(facts, list):
        return []
    return [f for f in facts if isinstance(f, dict) and f.get("body")]


def create_user_fact(
    body: str,
    source: str,
    *,
    tags: Optional[list[str]] = None,
    confidence: Optional[float] = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Optional[str]:
    """Write a new `kind:user-fact` node to the devagentic graph.
    Returns the new fact's head_id on success, None on any
    failure. Used by `scripts/migrate_memory_to_graph.py`."""
    user = _user_id()
    if not user:
        return None
    gql = (
        "mutation($u:String!,$b:String!,$s:String!,$t:[String!],$c:Float)"
        "{userFactCreate(userId:$u,body:$b,source:$s,tags:$t,confidence:$c)"
        "{id source}}"
    )
    data = _post_graphql(gql, {
        "u": user, "b": body, "s": source,
        "t": list(tags) if tags else None,
        "c": confidence,
    }, timeout=timeout)
    if data is None:
        return None
    fact = data.get("userFactCreate")
    if not isinstance(fact, dict):
        return None
    return fact.get("id")
