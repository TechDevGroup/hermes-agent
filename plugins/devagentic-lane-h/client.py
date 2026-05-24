"""GraphQL client for the devagentic-lane-h plugin (G4 / hermes-agent#58).

Public surface:
  * ``list_reasoning_grafts(user_id=None, limit=20)`` — wraps the
    ``reasoningGraftCandidates(userId, limit)`` GraphQL query from
    devagentic#207.
  * ``fetch_reasoning_graft(graft_id)`` — wraps the existing
    ``searchDocs(query, k:1)`` retrieval pattern with kind-tag
    validation.

Transport mirrors the other devagentic-adjacent hermes plugins
(canvas / docs / mutations / vertical-preamble) — same env vars,
same X-User-Id resolution, same fail-soft contract.

Env vars:
  ``DEVAGENTIC_BASE_URL``  default ``http://127.0.0.1:6071/v1``
  ``DEVAGENTIC_API_KEY``   bearer
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


_DEFAULT_TIMEOUT = 8.0


def _base_url() -> str:
    raw = os.environ.get("DEVAGENTIC_BASE_URL", "http://127.0.0.1:6071/v1")
    base = raw.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def _api_key() -> str:
    return (os.environ.get("DEVAGENTIC_API_KEY") or "").strip()


def resolve_user_id() -> Optional[str]:
    """Same precedence as the other devagentic-adjacent plugins."""
    override = (os.environ.get("DEVAGENTIC_USER_ID") or "").strip()
    if override:
        return override
    try:
        from hermes_cli.profiles import get_active_profile_name
        name = (get_active_profile_name() or "").strip()
        return name or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("devagentic-lane-h: profile resolution failed: %s", exc)
        return None


_last_error: Optional[str] = None


def last_error_text() -> Optional[str]:
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
    """POST a GraphQL query to devagentic. Returns the parsed
    ``data`` block on success; ``None`` on any failure. Populates
    ``last_error_text()`` on failures."""
    _record_error(None)
    user = resolve_user_id()
    if not user:
        msg = ("could not resolve user_id — set DEVAGENTIC_USER_ID "
               "or run inside a hermes profile")
        logger.debug("devagentic-lane-h: %s", msg)
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
        logger.debug("devagentic-lane-h: %s", msg)
        _record_error(msg)
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        _record_error("invalid response body (not JSON)")
        return None
    if not isinstance(payload, dict):
        _record_error("response was not a JSON object")
        return None
    if payload.get("errors"):
        _record_error(f"graphql errors: {payload.get('errors')}")
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        _record_error("graphql payload had no data field")
        return None
    return data


# --- public surface --------------------------------------------------------

_LIST_QUERY = """query($u:String!,$l:Int!){
    reasoningGraftCandidates(userId:$u, limit:$l){
        id userId content ts
        conferResultRef candidateId conferConfidence
        scaffoldMatchStatus h7GateStatus
    }
}"""


def list_reasoning_grafts(
    user_id: Optional[str] = None,
    limit: int = 20,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Optional[list[dict]]:
    """Wrap ``reasoningGraftCandidates(userId, limit)``. When
    ``user_id`` is None, falls back to the active profile's
    resolved id. Returns the list of graft dicts (each with id,
    userId, content, ts, conferResultRef, candidateId,
    conferConfidence, scaffoldMatchStatus, h7GateStatus) on
    success or ``None`` on failure. Empty list when the user has
    no grafts (success path, NOT failure)."""
    effective_uid = (user_id or "").strip() or resolve_user_id()
    if not effective_uid:
        _record_error(
            "could not resolve user_id — pass user_id explicitly or "
            "set DEVAGENTIC_USER_ID"
        )
        return None
    if limit <= 0:
        return []
    data = _post_graphql(
        _LIST_QUERY,
        {"u": effective_uid, "l": int(limit)},
        timeout=timeout,
    )
    if data is None:
        return None
    rows = data.get("reasoningGraftCandidates")
    if not isinstance(rows, list):
        _record_error("reasoningGraftCandidates returned non-list")
        return None
    return rows


_FETCH_BY_ID_QUERY = """query($q:String!){
    searchDocs(query:$q, k:1){ id content tags source ts }
}"""


def fetch_reasoning_graft(
    graft_id: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """Fetch one Lane H doc by id. Uses the existing
    ``searchDocs(query, k:1)`` retrieval pattern (devagentic has
    no GET-by-id GraphQL field) + verifies the top hit's id
    matches AND the hit has the ``kind:reasoning-graft-candidate``
    tag. Returns the doc dict on success or ``None`` on failure /
    kind mismatch."""
    if not graft_id:
        _record_error("graft_id is required")
        return None
    data = _post_graphql(_FETCH_BY_ID_QUERY, {"q": graft_id}, timeout=timeout)
    if data is None:
        return None
    hits = data.get("searchDocs") or []
    if not isinstance(hits, list) or not hits:
        _record_error(f"no doc matched id={graft_id}")
        return None
    top = hits[0]
    if (top.get("id") or "") != graft_id:
        _record_error(
            f"top match was {top.get('id')!r}, not requested {graft_id!r}"
        )
        return None
    tags = top.get("tags") or []
    if "kind:reasoning-graft-candidate" not in tags:
        _record_error(
            f"doc {graft_id} is not a kind:reasoning-graft-candidate "
            f"(tags={tags})"
        )
        return None
    return top
