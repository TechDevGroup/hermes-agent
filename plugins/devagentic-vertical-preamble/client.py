"""GraphQL client for devagentic's ``verticalContext(userId)`` query.

Mirrors the transport pattern in ``agent/devagentic_memory.py`` —
same env vars, same X-User-Id resolution, same fail-soft contract
(returns None on any failure; never raises). Used exclusively by
``preamble.py``.

Env vars:
  DEVAGENTIC_BASE_URL  default http://127.0.0.1:6071/v1
  DEVAGENTIC_API_KEY   bearer token (any non-empty value works
                        under DEVAGENTIC_TRUST_HEADER=1)
  DEVAGENTIC_USER_ID   manual override; otherwise resolved from
                        the active hermes profile name
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
    """Base URL with the /v1 suffix stripped (we append /graphql)."""
    raw = os.environ.get("DEVAGENTIC_BASE_URL", "http://127.0.0.1:6071/v1")
    base = raw.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def _api_key() -> str:
    return (os.environ.get("DEVAGENTIC_API_KEY") or "").strip()


def resolve_user_id() -> Optional[str]:
    """Same precedence as the memory + skills adapters.

    1. ``DEVAGENTIC_USER_ID`` env override.
    2. ``hermes_cli.profiles.get_active_profile_name()`` — derived
       from ``HERMES_HOME``; returns ``"default"`` for the
       ``~/.hermes`` root, or the profile name for
       ``~/.hermes/profiles/<name>``.
    3. None.
    """
    override = (os.environ.get("DEVAGENTIC_USER_ID") or "").strip()
    if override:
        return override
    try:
        from hermes_cli.profiles import get_active_profile_name
        name = (get_active_profile_name() or "").strip()
        return name or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("vertical-preamble: profile resolution failed: %s", exc)
        return None


_VERTICAL_CONTEXT_QUERY = """query($u:String!){
    verticalContext(userId:$u){
        userId
        verticalSpec{
            id userId name createdTs manifestPath envPath graftSourceCount
        }
        graftedContexts{id userId source ref sha path content ts}
        workerGuardrails{id userId constraints ts}
    }
}"""


def fetch_vertical_context(
    user_id: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """POST the verticalContext query to devagentic. Returns the
    rollup dict (with keys ``verticalSpec`` / ``graftedContexts`` /
    ``workerGuardrails``) on success, ``None`` on any failure.
    Never raises.
    """
    if not user_id:
        return None
    base = _base_url()
    body = json.dumps({
        "query": _VERTICAL_CONTEXT_QUERY,
        "variables": {"u": user_id},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/graphql", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("X-User-Id", user_id)
    api_key = _api_key()
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        logger.debug("vertical-preamble: request failed: %s", exc)
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.debug("vertical-preamble: parse failed: %s", exc)
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("errors"):
        logger.debug("vertical-preamble: graphql errors: %s",
                     payload.get("errors"))
        return None
    data = payload.get("data") or {}
    rollup = data.get("verticalContext")
    if not isinstance(rollup, dict):
        return None
    return rollup
