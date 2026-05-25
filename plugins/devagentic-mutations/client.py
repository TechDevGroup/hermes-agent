"""GraphQL client for devagentic mutations + queries exposed via MCP.

Phase 1 surface (this PR — hermes-agent#56 / G2):
  * ``query_silo(name, prompt, role_override=None)`` — wraps the
    ``querySilo`` GraphQL field. Foundational read tool for any
    worker that needs to ask a named silo.
  * ``run_confer_loop(user_id, candidate_id)`` — wraps the
    ``runConferLoop`` mutation. Kicks off a confer (3-4 free-tier
    silos vote on a finding-candidate; returns rolled-up consensus).

``writeDoc`` is already exposed via the existing
``plugins/devagentic-docs/client.py::write_doc`` + ``doc_write`` MCP
tool registered in ``mcp_serve._register_docs_tools``; this plugin
does NOT duplicate it.

Transport mirrors the other devagentic-adjacent plugins
(devagentic-canvas, devagentic-docs, agent/devagentic_memory.py):
same env vars, same ``X-User-Id`` resolution (env override or
active hermes profile), same fail-soft contract (returns ``None``
on any failure; never raises).

Env vars:
  ``DEVAGENTIC_BASE_URL``  default ``http://127.0.0.1:6071/v1``
  ``DEVAGENTIC_API_KEY``   bearer (any non-empty under trust-header mode)
  ``DEVAGENTIC_USER_ID``   manual override
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional


logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT = 30.0


def _base_url() -> str:
    """Base URL with the ``/v1`` suffix stripped (we append ``/graphql``)."""
    raw = os.environ.get("DEVAGENTIC_BASE_URL", "http://127.0.0.1:6071/v1")
    base = raw.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def _api_key() -> str:
    return (os.environ.get("DEVAGENTIC_API_KEY") or "").strip()


def resolve_user_id() -> Optional[str]:
    """Same precedence as the other devagentic-adjacent plugins.

    1. ``DEVAGENTIC_USER_ID`` env override
    2. ``hermes_cli.profiles.get_active_profile_name()``
    3. ``None``
    """
    override = (os.environ.get("DEVAGENTIC_USER_ID") or "").strip()
    if override:
        return override
    try:
        from hermes_cli.profiles import get_active_profile_name
        name = (get_active_profile_name() or "").strip()
        return name or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("devagentic-mutations: profile resolution failed: %s", exc)
        return None


_last_error: Optional[str] = None


def last_error_text() -> Optional[str]:
    """Most recent ``_post_graphql`` failure description on this
    process, or ``None`` if the last call succeeded. MCP tool
    wrappers append this to their ``{"error": ...}`` strings so
    operators see actionable diagnostics."""
    return _last_error


def _record_error(text: Optional[str]) -> None:
    global _last_error
    _last_error = text


def _post_graphql(
    query: str,
    variables: dict,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """POST a GraphQL query/mutation to devagentic. Returns the
    parsed ``data`` block on success; ``None`` on any failure
    (network, non-200, parse error, ``errors`` in payload, missing
    user_id). Never raises. Populates ``last_error_text()`` on the
    failure path."""
    _record_error(None)
    user = resolve_user_id()
    if not user:
        msg = ("could not resolve user_id — set DEVAGENTIC_USER_ID "
               "or run inside a hermes profile")
        logger.debug("devagentic-mutations: %s", msg)
        _record_error(msg)
        return None
    base = _base_url()
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    url = f"{base}/graphql"
    req = urllib.request.Request(url, data=body, method="POST")
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
        msg = f"devagentic unreachable at {url} ({exc})"
        logger.debug("devagentic-mutations: %s", msg)
        _record_error(msg)
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = "invalid response body (not JSON)"
        logger.debug("devagentic-mutations: parse failed: %s", exc)
        _record_error(msg)
        return None
    if not isinstance(payload, dict):
        _record_error("response was not a JSON object")
        return None
    if payload.get("errors"):
        errs = payload.get("errors")
        msg = (f"graphql errors: {errs}"
               if isinstance(errs, list) and errs else "graphql errors")
        logger.debug("devagentic-mutations: %s", msg)
        _record_error(msg)
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        _record_error("graphql payload had no data field")
        return None
    return data


# --- public surface --------------------------------------------------------

_QUERY_SILO_QUERY = """query($n:String!,$p:String!,$r:String){
    querySilo(name:$n, prompt:$p, roleOverride:$r){
        siloId siloName text cachedTokenCount promptTokenCount totalTokenCount
    }
}"""


def query_silo(
    name: str,
    prompt: str,
    role_override: Optional[str] = None,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """Wrap devagentic's ``querySilo`` field. Returns the silo reply
    dict ``{siloId, siloName, text, cachedTokenCount,
    promptTokenCount, totalTokenCount}`` on success or ``None`` on
    failure (see ``last_error_text()``)."""
    if not name or not prompt:
        _record_error("name and prompt are required")
        return None
    data = _post_graphql(
        _QUERY_SILO_QUERY,
        {"n": name, "p": prompt, "r": role_override or None},
        timeout=timeout,
    )
    if data is None:
        return None
    reply = data.get("querySilo")
    if not isinstance(reply, dict):
        _record_error("querySilo returned non-dict")
        return None
    return reply


_ASSERT_OUTPUT_MUTATION = """mutation($cid:String!,$ef:String!,$ep:String){
    assertOutput(callId:$cid, expectation:{fragment:$ef, predicate:$ep}){
        id ts callId passed violations
    }
}"""


def assert_output(
    call_id: str,
    fragment: str,
    predicate: Optional[str] = None,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """Wrap devagentic's ``assertOutput`` mutation. Evaluates an
    ``ExpectationInput {fragment, predicate?}`` against a previously
    emitted ``tool_call`` (referenced by ``call_id``) and writes a
    ``Verdict`` node.

    Args:
        call_id: The ``tool_call`` node id to vet.
        fragment: A path into the tool_call subject (e.g.,
            ``"content"``, ``"args.path"``). Required.
        predicate: Optional predicate string. When omitted /
            empty / null-stringly, the devagentic-side
            ``_stiffen_predicate`` substitutes a tool-specific
            floor (and emits a ``kind:predicate-coerced``
            telemetry doc).

    Returns ``{id, ts, callId, passed, violations: [...]}`` on
    success or ``None`` on failure (see ``last_error_text()``).
    """
    if not call_id or not fragment:
        _record_error("call_id and fragment are required")
        return None
    data = _post_graphql(
        _ASSERT_OUTPUT_MUTATION,
        {
            "cid": call_id,
            "ef": fragment,
            "ep": predicate if predicate not in (None, "") else None,
        },
        timeout=timeout,
    )
    if data is None:
        return None
    verdict = data.get("assertOutput")
    if not isinstance(verdict, dict):
        _record_error("assertOutput returned non-dict")
        return None
    return verdict


_RUN_CONFER_LOOP_MUTATION = """mutation($u:String!,$c:String!){
    runConferLoop(userId:$u, candidateId:$c)
}"""


def run_confer_loop(
    user_id: str,
    candidate_id: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """Wrap devagentic's ``runConferLoop`` mutation. Confer 3-4
    free-tier silos on a ``kind:finding-candidate`` doc. Returns the
    JSON rollup ``{confer_result_id, candidate_id, consensus_action,
    confidence, recommendation, silos_consulted,
    per_silo_response_ids, disagreement_points}`` on success or
    ``None`` on failure.

    Note: the mutation auto-binds X-User-Id from the request header,
    but the resolver also requires ``user_id`` as an explicit arg
    (its current shape). Workers can pass their own user_id; in MCP
    the wrapper accepts it as a tool arg."""
    if not user_id or not candidate_id:
        _record_error("user_id and candidate_id are required")
        return None
    data = _post_graphql(
        _RUN_CONFER_LOOP_MUTATION,
        {"u": user_id, "c": candidate_id},
        timeout=timeout,
    )
    if data is None:
        return None
    rollup = data.get("runConferLoop")
    # The resolver returns JSON scalar — strawberry passes it through.
    if rollup is None:
        _record_error("runConferLoop returned null")
        return None
    if not isinstance(rollup, dict):
        # Already-decoded JSON; coerce-to-dict expectation.
        _record_error("runConferLoop returned non-dict")
        return None
    return rollup
