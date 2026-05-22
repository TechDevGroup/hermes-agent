"""Thin HTTP client for devagentic's `/v1/canvas/*` REST surface
(Phase D, hermes issue #55).

Lets the canvas plugin talk to a running devagentic without
depending on the devagentic Python package being importable. Same
network conventions as the skill / memory adapters in
agent/devagentic_skills.py + agent/devagentic_memory.py:

  * Base URL from $DEVAGENTIC_BASE_URL (default http://127.0.0.1:6071/v1)
  * Bearer token from $DEVAGENTIC_API_KEY
  * X-User-Id from $DEVAGENTIC_USER_ID or hermes_cli.profiles
    .get_active_profile_name()
  * Every method returns Optional[<shape>] — None on any failure;
    callers MUST fall through gracefully

The contract for /v1/canvas/* endpoints is documented in
devagentic's service.py canvas REST surface (issue #41 + #43).
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any, Optional


logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT = 8.0
_VERSION_SUFFIX_RE = re.compile(r"/v\d+$")


def _base_url() -> str:
    raw = os.environ.get("DEVAGENTIC_BASE_URL", "http://127.0.0.1:6071/v1")
    trimmed = raw.rstrip("/")
    # Append /v1 when the operator set only host[:port] — silent 404s
    # otherwise (see #13). Respects an explicit /vN suffix.
    if not _VERSION_SUFFIX_RE.search(trimmed):
        trimmed = trimmed + "/v1"
    return trimmed


def _api_key() -> str:
    return (os.environ.get("DEVAGENTIC_API_KEY") or "").strip()


def _user_id() -> Optional[str]:
    """Same precedence as the skill / memory adapters."""
    override = (os.environ.get("DEVAGENTIC_USER_ID") or "").strip()
    if override:
        return override
    try:
        from hermes_cli.profiles import get_active_profile_name
        name = (get_active_profile_name() or "").strip()
        return name or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("canvas client: profile resolution failed: %s", exc)
        return None


_last_error: Optional[str] = None


def last_error_text() -> Optional[str]:
    """Return a short, human-readable description of the most recent
    `_request` failure on this process, or None if the last call
    succeeded. Used by `/canvas` slash commands to enrich the
    user-facing error string when a call returns None — the
    `pre_llm_call` hook ignores this and stays silent (see #15).
    """
    return _last_error


def _record_error(text: Optional[str]) -> None:
    global _last_error
    _last_error = text


def _request(method: str, path: str,
             body: Optional[dict] = None,
             *, timeout: float = _DEFAULT_TIMEOUT) -> Optional[dict]:
    """Run one HTTP request against devagentic's REST surface.
    Returns parsed JSON on 2xx, None on any failure. Never raises.

    On failure, populates the module-level `last_error_text()` slot
    with a short human-readable kind ("auth failed", "unreachable",
    "not found at <url>", etc.). Callers that want loud failures
    (slash commands) read it; callers that want silent loss-tolerant
    behavior (pre_llm_call hook) ignore it.
    """
    _record_error(None)
    base = _base_url()
    user = _user_id()
    if not user:
        msg = ("could not resolve user_id — set DEVAGENTIC_USER_ID or "
               "run inside a hermes profile")
        logger.debug("canvas client: %s", msg)
        _record_error(msg)
        return None
    url = f"{base}/canvas{path}" if path else f"{base}/canvas"
    if not path.startswith("/") and path:
        # Defensive: the /v1 prefix already has /canvas appended; paths
        # like "es" extend to /v1/canvases.
        url = f"{base}/canvas{path}"
    data: Optional[bytes] = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("X-User-Id", user)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    api_key = _api_key()
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            msg = ("authentication failed — set DEVAGENTIC_API_KEY "
                   "(any non-empty value works when devagentic runs "
                   "in trust-header mode)")
        elif exc.code == 404:
            msg = f"not found at {url}"
        else:
            msg = f"HTTP {exc.code} from {url}"
        logger.debug("canvas client: %s %s → %s", method, url, msg)
        _record_error(msg)
        return None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        msg = f"unreachable at {url} ({exc})"
        logger.debug("canvas client: %s %s failed: %s", method, url, exc)
        _record_error(msg)
        return None
    try:
        payload = json.loads(raw or "null")
    except json.JSONDecodeError as exc:
        msg = "invalid response body (not JSON)"
        logger.debug("canvas client: parse failed: %s", exc)
        _record_error(msg)
        return None
    if not isinstance(payload, dict):
        _record_error("response was not a JSON object")
        return None
    return payload


# --- public API ----------------------------------------------

def list_canvases(*, timeout: float = _DEFAULT_TIMEOUT) -> Optional[list[dict]]:
    """GET /v1/canvases — list user's canvases. Returns the list of
    canvas dicts, or None on failure."""
    data = _request("GET", "es", timeout=timeout)
    if data is None:
        return None
    canvases = data.get("canvases")
    if not isinstance(canvases, list):
        return []
    return canvases


def get_canvas(canvas_id: str, *,
               timeout: float = _DEFAULT_TIMEOUT) -> Optional[dict]:
    """GET /v1/canvas/{id} — full canvas state (canvas + nodes + edges).
    Returns None on 404 / network / parse failure."""
    if not canvas_id:
        return None
    return _request("GET", f"/{canvas_id}", timeout=timeout)


def create_canvas(name: str, description: str = "",
                  tags: Optional[list[str]] = None,
                  *,
                  timeout: float = _DEFAULT_TIMEOUT) -> Optional[dict]:
    """POST /v1/canvas — author a new canvas. Returns the new canvas
    dict (with `id`, `name`, ...) on success."""
    body: dict[str, Any] = {"name": name}
    if description:
        body["description"] = description
    if tags:
        body["tags"] = list(tags)
    return _request("POST", "", body=body, timeout=timeout)


# --- Mutations: nodes + edges (issue #56 MCP surface) -------

def add_node(canvas_id: str, node_type: str,
             position: Optional[dict] = None,
             *,
             timeout: float = _DEFAULT_TIMEOUT) -> Optional[dict]:
    """POST /v1/canvas/{id}/nodes — add a node to a canvas.
    `node_type` is the kind label (e.g. `doc`, `assertion`); the
    `position` dict is `{x, y}` if known. Returns the new node
    dict on success."""
    if not canvas_id or not node_type:
        return None
    body: dict[str, Any] = {"node_type": node_type}
    if position:
        body["position"] = dict(position)
    return _request("POST", f"/{canvas_id}/nodes",
                    body=body, timeout=timeout)


def update_node(canvas_id: str, node_id: str,
                fields: dict,
                *,
                timeout: float = _DEFAULT_TIMEOUT) -> Optional[dict]:
    """PATCH /v1/canvas/{id}/nodes/{node_id} — partial update.
    `fields` is the merge payload (e.g. {"node_type": "decision"}
    or {"position": {"x": 12, "y": 34}}). Returns the updated
    node dict on success."""
    if not canvas_id or not node_id or not fields:
        return None
    return _request("PATCH", f"/{canvas_id}/nodes/{node_id}",
                    body=dict(fields), timeout=timeout)


def move_node(canvas_id: str, node_id: str,
              x: float, y: float,
              *,
              timeout: float = _DEFAULT_TIMEOUT) -> Optional[dict]:
    """Convenience around `update_node` for repositioning. The
    underlying REST surface treats moves as a `position`-only
    update; this wrapper makes the MCP tool surface explicit."""
    return update_node(canvas_id, node_id,
                       {"position": {"x": float(x), "y": float(y)}},
                       timeout=timeout)


def delete_node(canvas_id: str, node_id: str,
                *,
                timeout: float = _DEFAULT_TIMEOUT) -> Optional[dict]:
    """DELETE /v1/canvas/{id}/nodes/{node_id} — drop a node.
    Returns the `{deleted: true, ...}` envelope on success."""
    if not canvas_id or not node_id:
        return None
    return _request("DELETE", f"/{canvas_id}/nodes/{node_id}",
                    timeout=timeout)


def link_nodes(canvas_id: str, source_id: str, target_id: str,
               edge_type: str = "links",
               *,
               timeout: float = _DEFAULT_TIMEOUT) -> Optional[dict]:
    """POST /v1/canvas/{id}/edges — author an edge between two
    nodes. `edge_type` is a free-form label. Returns the new
    edge dict on success."""
    if not canvas_id or not source_id or not target_id:
        return None
    body = {
        "source": source_id, "target": target_id,
        "edge_type": edge_type,
    }
    return _request("POST", f"/{canvas_id}/edges",
                    body=body, timeout=timeout)


def delete_edge(canvas_id: str, edge_id: str,
                *,
                timeout: float = _DEFAULT_TIMEOUT) -> Optional[dict]:
    """DELETE /v1/canvas/{id}/edges/{edge_id} — drop an edge.
    Returns the `{deleted: true, ...}` envelope on success."""
    if not canvas_id or not edge_id:
        return None
    return _request("DELETE", f"/{canvas_id}/edges/{edge_id}",
                    timeout=timeout)


def search_canvas(canvas_id: str, query: str,
                  *,
                  timeout: float = _DEFAULT_TIMEOUT) -> Optional[list[dict]]:
    """Client-side keyword search over a canvas's nodes. v0 — the
    REST surface doesn't ship a server-side search endpoint, so
    this fetches the full canvas state and filters nodes whose
    `body` / `name` / `node_type` field contains the query
    (case-insensitive substring match). Returns the list of
    matching node dicts, or None on fetch failure."""
    if not canvas_id or not query:
        return None
    state = get_canvas(canvas_id, timeout=timeout)
    if state is None:
        return None
    q = query.lower()
    matches: list[dict] = []
    for n in state.get("nodes") or []:
        haystack_fields = [
            str(n.get("node_type") or ""),
            str(n.get("name") or ""),
            str(n.get("body") or ""),
            str((n.get("body") or {}).get("content") or "")
            if isinstance(n.get("body"), dict) else "",
        ]
        if any(q in f.lower() for f in haystack_fields):
            matches.append(n)
    return matches
