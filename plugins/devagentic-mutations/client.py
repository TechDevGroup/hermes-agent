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


_READ_ARTIFACT_MUTATION = """mutation($p:String!,$c:String,$s:String){
    readArtifact(path:$p, ctxId:$c, strategy:$s)
}"""


def read_artifact(
    path: str,
    ctx_id: Optional[str] = None,
    strategy: Optional[str] = "raw",
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """Wrap devagentic's ``readArtifact`` mutation. Reads a file from
    the active workspace (filesystem / ssh-remote / sandbox-backend
    per ``current_workspace()``).

    Args:
        path: File path to read. Subject to devagentic-side workspace
            scoping + (when non-dev-scoped) the absolute-path
            whitelist.
        ctx_id: Optional ctx id (when supplied + dev-scoped, unlocks
            the 256K read cap; otherwise capped at 32K).
        strategy: Read strategy — ``"raw"`` (default), or one of the
            strategies surfaced by ``readart_strategies``.

    Returns ``{id, path, content, size, ack, truncated, strategy,
    ...}`` on success or ``None`` on failure (see
    ``last_error_text()``).
    """
    if not path:
        _record_error("path is required")
        return None
    data = _post_graphql(
        _READ_ARTIFACT_MUTATION,
        {"p": path, "c": ctx_id or None,
         "s": (strategy or "raw") or None},
        timeout=timeout,
    )
    if data is None:
        return None
    result = data.get("readArtifact")
    if not isinstance(result, dict):
        _record_error("readArtifact returned non-dict")
        return None
    return result


_PREVIEW_PATCH_QUERY = """query($p:String!,$f:String!,$r:String!){
    previewPatch(path:$p, find:$f, replace:$r)
}"""


def preview_patch(
    path: str,
    find: str,
    replace: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """Wrap devagentic's ``previewPatch`` query field. Computes the
    diff between the current file and the patched version + returns
    a ``confirm_token`` that ``patch_artifact`` requires (outside
    dev-scoped contexts).

    Args:
        path: File path to preview the patch against.
        find: Exact text to locate (single match — first occurrence
            replaced; mirrors ``str.replace(find, replace, 1)``
            semantics in devagentic).
        replace: Replacement text.

    Returns ``{confirm_token, diff, windows, syntax_check, ...}``
    on success — or ``{"error": ...}`` shape when the resolver
    surfaced an in-band error (e.g., find-string missing). On
    transport failure returns ``None`` (see ``last_error_text()``).
    """
    if not path or find is None:
        _record_error("path and find are required")
        return None
    data = _post_graphql(
        _PREVIEW_PATCH_QUERY,
        {"p": path, "f": find, "r": replace if replace is not None else ""},
        timeout=timeout,
    )
    if data is None:
        return None
    result = data.get("previewPatch")
    if not isinstance(result, dict):
        _record_error("previewPatch returned non-dict")
        return None
    return result


_PATCH_ARTIFACT_MUTATION = """mutation(
    $p:String!,$f:String!,$r:String!,$t:String,$c:String){
    patchArtifact(path:$p, find:$f, replace:$r,
                  confirmToken:$t, ctxId:$c)
}"""


def patch_artifact(
    path: str,
    find: str,
    replace: str,
    confirm_token: Optional[str] = None,
    ctx_id: Optional[str] = None,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """Wrap devagentic's ``patchArtifact`` mutation. Applies a
    find/replace edit in place. Requires ``confirm_token`` from a
    prior ``preview_patch`` call unless ``ctx_id`` is dev-scoped or
    a sandbox-backend is active.

    Args:
        path: File path to patch.
        find: Exact text to locate (single replacement).
        replace: Replacement text.
        confirm_token: Token from a prior ``preview_patch`` call;
            required outside dev-scoped + sandbox-backend contexts.
        ctx_id: Optional ctx id (when dev-scoped, bypasses the
            token requirement).

    Returns ``{id, path, written, replacements, ack, ...}`` on
    success or ``None`` on failure (see ``last_error_text()`` —
    typically ``"confirm_token does not match"`` or
    ``"file not found"``).
    """
    if not path or find is None:
        _record_error("path and find are required")
        return None
    data = _post_graphql(
        _PATCH_ARTIFACT_MUTATION,
        {"p": path, "f": find,
         "r": replace if replace is not None else "",
         "t": confirm_token or None,
         "c": ctx_id or None},
        timeout=timeout,
    )
    if data is None:
        return None
    result = data.get("patchArtifact")
    if not isinstance(result, dict):
        _record_error("patchArtifact returned non-dict")
        return None
    return result


_FETCH_URL_MUTATION = """mutation($u:String!,$c:String){
    fetchUrl(url:$u, ctxId:$c)
}"""


def fetch_url(
    url: str,
    ctx_id: Optional[str] = None,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """Wrap devagentic's ``fetchUrl`` mutation. Performs a graph-
    resident HTTP fetch; subject to devagentic-side constraints
    (localhost-only URLs, 16K body cap, mock-shape lookup +
    auto-capture gated by ``DEVAGENTIC_MOCK_LOOKUP`` /
    ``DEVAGENTIC_MOCK_CAPTURE`` env).

    Args:
        url: HTTP URL — devagentic enforces ``http://localhost`` /
            ``http://127.0.0.1`` prefix; other URLs are rejected.
        ctx_id: Optional ctx id (threaded into the resulting
            ``tool_call`` node's refs).

    Returns ``{url, status_code, content_type, body, truncated,
    mocked, ack, ...}`` on success or ``None`` on failure (see
    ``last_error_text()``).
    """
    if not url:
        _record_error("url is required")
        return None
    data = _post_graphql(
        _FETCH_URL_MUTATION,
        {"u": url, "c": ctx_id or None},
        timeout=timeout,
    )
    if data is None:
        return None
    result = data.get("fetchUrl")
    if not isinstance(result, dict):
        _record_error("fetchUrl returned non-dict")
        return None
    return result


_EXECUTE_WORKFLOW_PIPELINE_MUTATION = """mutation($p:String!,$u:String!){
    executeWorkflowPipeline(pipelineId:$p, userId:$u){
        runId pipelineRef userId status
        startedTs completedTs errorMessage
        nodeOutcomes {
            nodeId intent status startedTs completedTs
            outputSummary errorMessage retryCount
        }
    }
}"""


def run_pipeline(
    pipeline_id: str,
    user_id: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """Wrap devagentic's ``executeWorkflowPipeline`` mutation (R12 of
    devagentic#210). Looks up a ``kind:workflow-pipeline`` doc by id,
    walks its node DAG topologically, and returns the full
    ``PipelineRun`` structure synchronously.

    Args:
        pipeline_id: Doc id of the workflow-pipeline (typically
            from a prior ``propose_pipeline`` call or a previously
            authored pipeline doc).
        user_id: User scope for the run; threaded into per-node
            handlers + the resulting ``kind:pipeline-run`` doc.

    Returns the run dict ``{runId, pipelineRef, userId, status,
    startedTs, completedTs, errorMessage?, nodeOutcomes: [...]}``
    on success or ``None`` on failure (see ``last_error_text()``).
    """
    if not pipeline_id or not user_id:
        _record_error("pipeline_id and user_id are required")
        return None
    data = _post_graphql(
        _EXECUTE_WORKFLOW_PIPELINE_MUTATION,
        {"p": pipeline_id, "u": user_id},
        timeout=timeout,
    )
    if data is None:
        return None
    result = data.get("executeWorkflowPipeline")
    if not isinstance(result, dict):
        _record_error("executeWorkflowPipeline returned non-dict")
        return None
    return result


_WRITE_WORKFLOW_PIPELINE_MUTATION = """mutation(
    $n:String!,$i:String!,$v:Int!,$nodes:JSON!,$edges:JSON!,$u:String!,
    $lr:String,$pb:String){
    writeWorkflowPipeline(
        name:$n, intent:$i, version:$v,
        nodes:$nodes, edges:$edges, userId:$u,
        lineageRef:$lr, producedBy:$pb
    ){
        id content tags source ts
    }
}"""


def propose_pipeline(
    name: str,
    intent: str,
    version: int,
    nodes: list,
    edges: list,
    user_id: str,
    lineage_ref: Optional[str] = None,
    produced_by: Optional[str] = None,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """Wrap devagentic's ``writeWorkflowPipeline`` mutation (R12 of
    devagentic#210). Validates the pipeline DAG via #213's validator,
    composes the body, and persists a ``kind:workflow-pipeline``
    Doc. An invalid pipeline shape raises devagentic-side before any
    doc is written.

    Args:
        name: Human-readable pipeline name.
        intent: Intent key (matches devagentic#240's classifier
            vocabulary: ``code`` / ``confer`` / ``planning`` /
            ``exploration`` / ``refinement`` / ``generic``).
        version: Integer pipeline version. Increment per revision.
        nodes: List of node dicts. Each node has ``id``,
            ``intent``, plus handler-specific config.
        edges: List of edge dicts. Each edge has ``from``, ``to``.
        user_id: User scope for the new pipeline doc.
        lineage_ref: Optional parent doc id (when this pipeline
            revises an earlier one).
        produced_by: Optional source attribution (worker /
            orchestrator).

    Returns the persisted Doc shape ``{id, content, tags, source,
    ts}`` on success or ``None`` on failure (see
    ``last_error_text()``).
    """
    if not name or not intent or not user_id:
        _record_error("name, intent, and user_id are required")
        return None
    if not isinstance(version, int) or version < 1:
        _record_error("version must be a positive int")
        return None
    if not isinstance(nodes, list) or not isinstance(edges, list):
        _record_error("nodes and edges must be lists")
        return None
    data = _post_graphql(
        _WRITE_WORKFLOW_PIPELINE_MUTATION,
        {
            "n": name, "i": intent, "v": int(version),
            "nodes": nodes, "edges": edges, "u": user_id,
            "lr": lineage_ref or None,
            "pb": produced_by or None,
        },
        timeout=timeout,
    )
    if data is None:
        return None
    result = data.get("writeWorkflowPipeline")
    if not isinstance(result, dict):
        _record_error("writeWorkflowPipeline returned non-dict")
        return None
    return result


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
