"""Phase C devagentic-graph skill adapter (devagentic issue #52).

Talks to a running devagentic over HTTP to resolve skill bodies from
graph nodes (kind:skill) before hermes falls back to its
~/.hermes/skills/*.md files.

Opt-in via env `DEVAGENTIC_SKILLS_GRAPH=1`. Default off keeps the
file-based flow byte-stable. The migration script
`scripts/migrate_skills_to_graph.py` populates the graph from
existing files; once enabled, `resolve_skill_body(name)` consults
the graph first and falls through to None on any error (caller
keeps its file-based fallback).

Env vars:
  DEVAGENTIC_SKILLS_GRAPH  set to `1` to enable graph lookups
                            (default: off). When off,
                            resolve_skill_body always returns
                            None — callers fall straight to files.
  DEVAGENTIC_BASE_URL       devagentic base URL (default
                            http://127.0.0.1:6071/v1). Reused from
                            the devagentic-local provider's setting
                            so a single configuration covers both.
  DEVAGENTIC_API_KEY        bearer token forwarded to devagentic.
                            With DEVAGENTIC_TRUST_HEADER=1 on
                            devagentic, any non-empty value works.

Failure semantics: every code path that touches the network is
wrapped in try/except. Resolver returns None on any failure
(network, parse, missing-skill, etc.); callers MUST keep their
existing file fallback so a transient devagentic outage doesn't
brick `/skill-name`.

Transport caveat: this adapter assumes devagentic exposes
`/graphql` over HTTP. Some canonical deployments don't (see
TechDevGroup/hermes-agent#21); the resolver silently falls back
to None on every call there, and the file fallback kicks in.
Run `hermes doctor` for a probe + actionable hint when graph
mode is enabled.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional


logger = logging.getLogger(__name__)


GRAPH_ENV = "DEVAGENTIC_SKILLS_GRAPH"

# 8s is generous for a local-loopback HTTP query; longer than that
# and the user is better served by the file fallback than by
# blocking on a flaky link.
_DEFAULT_TIMEOUT = 8.0


def graph_enabled() -> bool:
    """True iff `DEVAGENTIC_SKILLS_GRAPH` is in `1|true|yes|on`."""
    return os.environ.get(GRAPH_ENV, "0").strip().lower() in (
        "1", "true", "yes", "on")


def _base_url() -> str:
    raw = os.environ.get("DEVAGENTIC_BASE_URL", "http://127.0.0.1:6071/v1")
    # The OAI plugin uses `…/v1`; the GraphQL surface is on `…/graphql`.
    # Strip the trailing /v1 if present so we can compose either path.
    base = raw.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def _api_key() -> str:
    return (os.environ.get("DEVAGENTIC_API_KEY") or "").strip()


def _user_id() -> Optional[str]:
    """Resolve the X-User-Id to send.

    Same precedence as the devagentic-local provider plugin:
      1. `DEVAGENTIC_USER_ID` env override.
      2. `hermes_cli.profiles.get_active_profile_name()`.
      3. None (no header — caller-side may reject).
    """
    override = (os.environ.get("DEVAGENTIC_USER_ID") or "").strip()
    if override:
        return override
    try:
        from hermes_cli.profiles import get_active_profile_name
        name = (get_active_profile_name() or "").strip()
        return name or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("devagentic_skills: profile resolution failed: %s", exc)
        return None


def _post_graphql(
    query: str,
    variables: dict,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """POST a GraphQL query to devagentic. Returns the parsed
    `data` dict on success, None on any failure (network, non-200,
    parse error, GraphQL error). Failures log at DEBUG so
    operational signal doesn't flood the user-facing stream."""
    base = _base_url()
    user = _user_id()
    if not user:
        logger.debug("devagentic_skills: no user_id resolved; skipping")
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
        logger.debug("devagentic_skills: request failed: %s", exc)
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.debug("devagentic_skills: parse failed: %s", exc)
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("errors"):
        logger.debug("devagentic_skills: graphql errors: %s",
                     payload.get("errors"))
        return None
    return payload.get("data") or None


def resolve_skill_body(
    name: str,
    *,
    context_tags: Optional[list[str]] = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Optional[str]:
    """Resolve a skill body from the devagentic graph.

    Returns the body string on success, None on any failure. The
    caller MUST keep its file-based fallback for the None case;
    this function is a passive read-through, not a hard
    dependency.

    No-op (returns None immediately) when `graph_enabled()` is
    False, when no user_id can be resolved, or when `name` is
    empty. Logs at DEBUG; never raises."""
    if not graph_enabled():
        return None
    if not name:
        return None
    user = _user_id()
    if not user:
        return None
    query = (
        "query($u:String!,$n:String!,$ct:[String!])"
        "{skillResolve(userId:$u,name:$n,contextTags:$ct)"
        "{id name body version tags}}"
    )
    data = _post_graphql(query, {
        "u": user, "n": name,
        "ct": list(context_tags) if context_tags else None,
    }, timeout=timeout)
    if data is None:
        return None
    skill = data.get("skillResolve")
    if not isinstance(skill, dict):
        return None
    body = skill.get("body")
    if not isinstance(body, str) or not body:
        return None
    return body


def create_skill(
    name: str,
    body: str,
    *,
    tags: Optional[list[str]] = None,
    authored_by: Optional[str] = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Optional[str]:
    """Write a new `kind:skill` node to the devagentic graph.
    Returns the new skill's head_id on success, None on any
    failure. Used by `scripts/migrate_skills_to_graph.py`."""
    user = _user_id()
    if not user:
        return None
    mutation = (
        "mutation($u:String!,$n:String!,$b:String!,$t:[String!],$ab:String)"
        "{skillCreate(userId:$u,name:$n,body:$b,tags:$t,authoredBy:$ab)"
        "{id name version}}"
    )
    data = _post_graphql(mutation, {
        "u": user, "n": name, "b": body,
        "t": list(tags) if tags else None,
        "ab": authored_by,
    }, timeout=timeout)
    if data is None:
        return None
    skill = data.get("skillCreate")
    if not isinstance(skill, dict):
        return None
    return skill.get("id")
