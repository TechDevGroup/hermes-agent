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
import urllib.error
import urllib.request
from typing import Any, Optional


logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT = 8.0


def _base_url() -> str:
    raw = os.environ.get("DEVAGENTIC_BASE_URL", "http://127.0.0.1:6071/v1")
    return raw.rstrip("/")


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


def _request(method: str, path: str,
             body: Optional[dict] = None,
             *, timeout: float = _DEFAULT_TIMEOUT) -> Optional[dict]:
    """Run one HTTP request against devagentic's REST surface.
    Returns parsed JSON on 2xx, None on any failure. Never raises."""
    base = _base_url()
    user = _user_id()
    if not user:
        logger.debug("canvas client: no user_id resolved")
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
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        logger.debug("canvas client: %s %s failed: %s", method, url, exc)
        return None
    try:
        payload = json.loads(raw or "null")
    except json.JSONDecodeError as exc:
        logger.debug("canvas client: parse failed: %s", exc)
        return None
    if not isinstance(payload, dict):
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
