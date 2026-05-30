"""Thin GraphQL client for devagentic's writeDoc + searchDocs
primitives. Sibling to plugins/devagentic-canvas/client.py — same
env conventions (DEVAGENTIC_BASE_URL, DEVAGENTIC_API_KEY,
DEVAGENTIC_USER_ID + profile fallback), same loss-tolerant contract.

Each method returns Optional[<shape>] — None on any failure. The
slash commands read `last_error_text()` to surface the specific
failure kind in user-facing messages (auth vs unreachable vs parse
vs no user_id), the same pattern used by the canvas plugin after
#15.

Devagentic's GraphQL surface is at $DEVAGENTIC_BASE_URL/graphql
(the /v1 suffix in the env var is stripped if present, mirroring
agent/devagentic_skills.py + agent/devagentic_memory.py).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Optional


logger = logging.getLogger(__name__)


# hermes-agent#161 — inline copy of utils.classify_http_error so this plugin
# survives a system-installed `utils` package shadowing hermes's top-level
# utils.py. Live error from poly-explorer 2026-05-29 23:34/23:44:
#   Error executing tool doc_write: cannot import name classify_http_error
#   from utils (/usr/local/lib/python3.11/dist-packages/utils.py)
# The original lazy `from utils import classify_http_error` only fires on
# the HTTP-error path, so the failure was self-masking until poly hit it.
# Keeping the classification local makes the doc_write HTTP-error path
# self-contained and shadow-immune; the small duplication is the cost of
# robustness on a recurring runtime error.
def _classify_http_error(exc: BaseException) -> str:
    """Map a urllib-style exception to one of: 'auth' / 'not_found' /
    'http' / 'unreachable' / 'unknown'. Mirrors utils.classify_http_error."""
    if isinstance(exc, urllib.error.HTTPError):
        code = getattr(exc, "code", None)
        if code in (401, 403):
            return "auth"
        if code == 404:
            return "not_found"
        return "http"
    if isinstance(exc, (urllib.error.URLError, OSError, TimeoutError)):
        return "unreachable"
    return "unknown"


_DEFAULT_TIMEOUT = 8.0


def _base_url() -> Optional[str]:
    """Resolve the GraphQL root URL. Accepts both `…:6070` and
    `…:6070/v1` forms (the latter has its /v1 stripped so we can
    compose `…/graphql`). Mirrors devagentic_skills + memory.

    hermes-agent#167 — returns ``None`` when ``DEVAGENTIC_BASE_URL`` is
    unset rather than silently falling back to ``http://127.0.0.1:6071``.
    The old default disguised a misconfigured / non-propagated env (the
    MCP subprocess inherited an empty env, which stripped this variable)
    as a transient ECONNREFUSED for hours. The caller in ``_post_graphql``
    records a precise error via ``_record_error`` so the slash-command
    surface gets ``DEVAGENTIC_BASE_URL not set`` instead of a misleading
    127.0.0.1 connection-refused.
    """
    raw = (os.environ.get("DEVAGENTIC_BASE_URL") or "").strip()
    if not raw:
        return None
    base = raw.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def _api_key() -> str:
    return (os.environ.get("DEVAGENTIC_API_KEY") or "").strip()


def _user_id() -> Optional[str]:
    override = (os.environ.get("DEVAGENTIC_USER_ID") or "").strip()
    if override:
        return override
    try:
        from hermes_cli.profiles import get_active_profile_name
        name = (get_active_profile_name() or "").strip()
        return name or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("docs client: profile resolution failed: %s", exc)
        return None


_last_error: Optional[str] = None


def last_error_text() -> Optional[str]:
    """Short, human-readable description of the most recent failure.
    Slash commands append this as `Reason: …` to their user-facing
    error strings. Cleared on every successful call."""
    return _last_error


def _record_error(text: Optional[str]) -> None:
    global _last_error
    _last_error = text


def _post_graphql(query: str, variables: dict,
                  *, timeout: float = _DEFAULT_TIMEOUT) -> Optional[dict]:
    """POST a GraphQL query. Returns the `data` dict on success,
    None on any failure. Never raises. Populates last_error_text()
    with the specific failure kind on None returns."""
    _record_error(None)
    user = _user_id()
    if not user:
        msg = ("could not resolve user_id — set DEVAGENTIC_USER_ID "
               "or run inside a hermes profile")
        logger.debug("docs client: %s", msg)
        _record_error(msg)
        return None
    base = _base_url()
    if base is None:
        # hermes-agent#167 — fail loud on unset DEVAGENTIC_BASE_URL
        # instead of silently defaulting to 127.0.0.1. Inside containerized
        # MCP subprocesses where the env wasn't propagated, the silent
        # default looked like a transient connection-refused for hours.
        msg = ("DEVAGENTIC_BASE_URL not set — propagate it from the "
               "parent env into the MCP server config, or set it in "
               "your hermes profile")
        logger.debug("docs client: %s", msg)
        _record_error(msg)
        return None
    url = f"{base}/graphql"
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
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
        # HTTPError is a URLError subclass; _classify_http_error (above)
        # handles all four kinds in one dispatch. Uses the local helper —
        # NOT `from utils import ...` — so a system `utils` package
        # shadow cannot break this error path (hermes-agent#161).
        kind = _classify_http_error(exc)
        if kind == "auth":
            msg = ("authentication failed — set DEVAGENTIC_API_KEY "
                   "(any non-empty value works when devagentic runs "
                   "in trust-header mode)")
        elif kind == "not_found":
            msg = f"not found at {url}"
        elif kind == "http":
            msg = f"HTTP {getattr(exc, 'code', '?')} from {url}"
        else:
            msg = f"unreachable at {url} ({exc})"
        logger.debug("docs client: POST %s → %s", url, msg)
        _record_error(msg)
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.debug("docs client: parse failed: %s", exc)
        _record_error("invalid response body (not JSON)")
        return None
    if not isinstance(payload, dict):
        _record_error("response was not a JSON object")
        return None
    if payload.get("errors"):
        # GraphQL-level error (resolver failure, invalid query, etc.)
        first = (payload.get("errors") or [{}])[0]
        msg = f"GraphQL error: {first.get('message', 'unknown')}"
        logger.debug("docs client: %s", msg)
        _record_error(msg)
        return None
    return payload.get("data") or None


# --- public API ----------------------------------------------

def search_docs(query: str, limit: int = 10,
                tag: Optional[str] = None,
                *, timeout: float = _DEFAULT_TIMEOUT
                ) -> Optional[list[dict]]:
    """Search the user's doc graph and return [{id, content, tags,
    source, ts}, ...]. Returns None on any failure (caller falls
    through); empty list when the search succeeds with no hits.

    `--tag` and free-text `query` map to different devagentic
    primitives in the current schema: `Query.docs(tags:[t])` is the
    tag-scoped browser; `Query.searchDocs(query, k)` is the lexical
    + embedding ranker. We route based on which is set:
      tag set       → docs(tags:[t]); slice client-side to `limit`
      tag unset     → searchDocs(query, k=limit)
    """
    if not query and not tag:
        _record_error("query or --tag is required")
        return None
    if tag:
        gql = (
            "query($t:[String!]){"
            " docs(tags:$t){ id content tags source ts }"
            "}"
        )
        data = _post_graphql(gql, {"t": [tag]}, timeout=timeout)
        if data is None:
            return None
        hits = data.get("docs") or []
        if not isinstance(hits, list):
            return []
        # docs() returns everything matching the tag; the user's
        # `limit` is a display cap.
        if query:
            # Free-text filter within the tag scope.
            q_lower = query.lower()
            hits = [h for h in hits
                    if q_lower in (h.get("content") or "").lower()]
        return hits[: max(1, int(limit))]
    gql = (
        "query($q:String!,$k:Int){"
        " searchDocs(query:$q, k:$k){ id content tags source ts }"
        "}"
    )
    data = _post_graphql(gql, {"q": query, "k": int(limit)},
                         timeout=timeout)
    if data is None:
        return None
    hits = data.get("searchDocs")
    if not isinstance(hits, list):
        return []
    return hits


def write_doc(content: str, tags: Optional[list[str]] = None,
              source: Optional[str] = None,
              *, timeout: float = _DEFAULT_TIMEOUT) -> Optional[dict]:
    """writeDoc(content, tags, source) → {id}. Returns the new doc
    dict on success, None on any failure."""
    if not content:
        _record_error("content is required")
        return None
    gql = (
        "mutation($c:String!,$t:[String!],$s:String){"
        " writeDoc(content:$c, tags:$t, source:$s){ id }"
        "}"
    )
    variables: dict[str, Any] = {"c": content}
    if tags:
        variables["t"] = list(tags)
    if source:
        variables["s"] = source
    data = _post_graphql(gql, variables, timeout=timeout)
    if data is None:
        return None
    return data.get("writeDoc") or None


def get_doc(doc_id: str, *,
            timeout: float = _DEFAULT_TIMEOUT) -> Optional[dict]:
    """Fetch a single doc by id via searchDocs (devagentic's
    canonical retrieval primitive — there is no GET /doc/<id>
    GraphQL field in the current surface). Issues a `k=1` search
    keyed on the id string and verifies the top hit's id matches."""
    if not doc_id:
        _record_error("doc_id is required")
        return None
    gql = (
        "query($q:String!){"
        " searchDocs(query:$q, k:1){ id content tags source ts }"
        "}"
    )
    data = _post_graphql(gql, {"q": doc_id}, timeout=timeout)
    if data is None:
        return None
    hits = data.get("searchDocs") or []
    if not isinstance(hits, list) or not hits:
        _record_error(f"no doc matched id={doc_id}")
        return None
    top = hits[0]
    if (top.get("id") or "") != doc_id:
        _record_error(
            f"top match was {top.get('id')!r}, not requested {doc_id!r}")
        return None
    return top


# --- Fork / context surface (issue #12 follow-up) ----------

def fork_context(parent_id: str,
                  tags: Optional[list[str]] = None,
                  annotations: Optional[list[dict]] = None,
                  *,
                  timeout: float = _DEFAULT_TIMEOUT) -> Optional[dict]:
    """Wrap `forkContext(parentId, tags, annotations)`. Returns the
    new Context dict ({id, ts, tags, annotations, ...}) on success.
    `annotations` is a list of {key, value, weight} dicts."""
    if not parent_id:
        _record_error("parent_id is required")
        return None
    gql = (
        "mutation($p:String!,$t:[String!],$a:[AnnotationInput!]){"
        " forkContext(parentId:$p, tags:$t, annotations:$a){"
        "   id ts tags annotations{ key value weight }"
        " }"
        "}"
    )
    variables: dict[str, Any] = {"p": parent_id}
    if tags:
        variables["t"] = list(tags)
    if annotations:
        variables["a"] = [
            {"key": a.get("key"), "value": a.get("value"),
             "weight": a.get("weight", 1.0)}
            for a in annotations]
    data = _post_graphql(gql, variables, timeout=timeout)
    if data is None:
        return None
    return data.get("forkContext") or None


def decorate_context(ctx_id: str, key: str, value: str,
                     weight: float = 1.0,
                     *,
                     timeout: float = _DEFAULT_TIMEOUT
                     ) -> Optional[dict]:
    """Wrap `decorateContext(ctxId, annotation)`. Returns the updated
    Context dict on success. One annotation per call (the schema's
    AnnotationInput is scalar)."""
    if not ctx_id or not key:
        _record_error("ctx_id and key are required")
        return None
    gql = (
        "mutation($c:String!,$a:AnnotationInput!){"
        " decorateContext(ctxId:$c, annotation:$a){"
        "   id annotations{ key value weight }"
        " }"
        "}"
    )
    variables = {
        "c": ctx_id,
        "a": {"key": key, "value": value, "weight": float(weight)},
    }
    data = _post_graphql(gql, variables, timeout=timeout)
    if data is None:
        return None
    return data.get("decorateContext") or None


def get_context(ctx_id: str, *,
                 timeout: float = _DEFAULT_TIMEOUT) -> Optional[dict]:
    """Wrap `context(id)`. Returns the Context dict or None."""
    if not ctx_id:
        _record_error("ctx_id is required")
        return None
    gql = (
        "query($i:String!){"
        " context(id:$i){"
        "   id ts tags annotations{ key value weight }"
        " }"
        "}"
    )
    data = _post_graphql(gql, {"i": ctx_id}, timeout=timeout)
    if data is None:
        return None
    return data.get("context") or None


def render_context(ctx_id: str, *,
                    timeout: float = _DEFAULT_TIMEOUT
                    ) -> Optional[str]:
    """Wrap `renderContext(ctxId) -> String`. Returns the rendered
    text. Returns None on failure, "" if devagentic returns null."""
    if not ctx_id:
        _record_error("ctx_id is required")
        return None
    gql = "query($c:String!){ renderContext(ctxId:$c) }"
    data = _post_graphql(gql, {"c": ctx_id}, timeout=timeout)
    if data is None:
        return None
    val = data.get("renderContext")
    if val is None:
        return ""
    return str(val)
