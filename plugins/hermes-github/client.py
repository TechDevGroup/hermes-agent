"""GitHub REST client for the hermes-github plugin's ``file_issue``
tool (G3 of devagentic#203 / hermes-agent#57).

Posts to ``POST /repos/TechDevGroup/<repo>/issues`` on github.com.
Token resolution happens on the hermes host (env or ``gh`` CLI) so
the worker conversation never sees credentials. Restricted by
design to TechDevGroup/devagentic + TechDevGroup/hermes-agent —
the two stack repos workers are allowed to file against per
#203 §3.2.

All public functions fail-soft (return ``None`` on any failure;
``last_error_text()`` carries the diagnostic). MCP tool wrappers
translate ``None`` into a ``{"error": ...}`` JSON string.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import Optional


logger = logging.getLogger(__name__)


# Restricted by design — workers file only against the two stack repos.
# Anything outside this set returns an error. Operators who want to
# allow a wider scope file a separate issue + extend the set explicitly.
_ALLOWED_REPOS: frozenset[str] = frozenset({
    "devagentic",
    "hermes-agent",
})
_OWNER = "TechDevGroup"


_DEFAULT_TIMEOUT = 15.0


_last_error: Optional[str] = None


def last_error_text() -> Optional[str]:
    """Most recent ``file_issue`` failure description on this process,
    or ``None`` if the last call succeeded."""
    return _last_error


def _record_error(text: Optional[str]) -> None:
    global _last_error
    _last_error = text


def allowed_repos() -> frozenset[str]:
    """Public view of the restricted repo set; exposed so MCP tool
    descriptors can quote it in their argument docs."""
    return _ALLOWED_REPOS


def _resolve_token() -> Optional[str]:
    """Pick a GitHub token in priority order:

    1. ``HERMES_GH_TOKEN`` env var — dedicated hermes-side override.
    2. ``GITHUB_TOKEN`` / ``GH_TOKEN`` env vars — standard PAT env.
    3. ``gh auth token`` subprocess — when the ``gh`` CLI is on
       PATH and the operator has logged in.
    4. ``None`` — caller surfaces an actionable error string.

    The token is NEVER written to logs or returned to the worker
    conversation. Only the request to api.github.com sees it.
    """
    for var in ("HERMES_GH_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        v = (os.environ.get(var) or "").strip()
        if v:
            return v
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        proc = subprocess.run(
            [gh, "auth", "token"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("hermes-github: gh auth token failed: %s", exc)
        return None
    if proc.returncode != 0:
        return None
    tok = (proc.stdout or "").strip()
    return tok or None


def file_issue(
    repo: str,
    title: str,
    body: str,
    labels: Optional[list[str]] = None,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """Open a GitHub issue on ``TechDevGroup/<repo>``.

    Args:
        repo: ``"devagentic"`` or ``"hermes-agent"`` — anything else
            is rejected (see :func:`allowed_repos`).
        title: Issue title. Required, non-empty.
        body: Issue body (markdown). Required, non-empty.
        labels: Optional list of label names to attach.
        timeout: HTTP timeout in seconds.

    Returns:
        Dict ``{"number": int, "url": str, "html_url": str,
        "title": str, "state": str}`` on success; ``None`` on
        failure (see :func:`last_error_text`).
    """
    _record_error(None)
    if not repo or repo not in _ALLOWED_REPOS:
        _record_error(
            f"repo must be one of {sorted(_ALLOWED_REPOS)} "
            f"(got {repo!r})"
        )
        return None
    if not title or not title.strip():
        _record_error("title is required (non-empty)")
        return None
    if not body or not body.strip():
        _record_error("body is required (non-empty)")
        return None
    token = _resolve_token()
    if not token:
        _record_error(
            "no GitHub token available — set HERMES_GH_TOKEN / "
            "GITHUB_TOKEN / GH_TOKEN, or run `gh auth login` on "
            "the hermes host"
        )
        return None

    payload: dict = {"title": title.strip(), "body": body}
    if labels:
        cleaned = [str(lbl).strip() for lbl in labels if str(lbl).strip()]
        if cleaned:
            payload["labels"] = cleaned

    url = f"https://api.github.com/repos/{_OWNER}/{repo}/issues"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "hermes-github-plugin/0.1.0")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        _record_error(
            f"GitHub API HTTP {exc.code} for {url}: "
            f"{err_body[:200] or exc.reason}"
        )
        return None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        _record_error(f"GitHub API unreachable at {url}: {exc}")
        return None

    if status >= 300:
        _record_error(f"GitHub API unexpected status {status}")
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _record_error("invalid GitHub API response (not JSON)")
        return None
    if not isinstance(data, dict) or "number" not in data:
        _record_error("GitHub API response missing 'number' field")
        return None
    return {
        "number": data["number"],
        "url": data.get("url", ""),
        "html_url": data.get("html_url", ""),
        "title": data.get("title", title),
        "state": data.get("state", "open"),
    }
