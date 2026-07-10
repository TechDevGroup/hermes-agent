"""Devagentic local provider profile.

Registers a `devagentic-local` provider in hermes' provider registry. The
profile points at a devagentic instance's OpenAI-compatible `/v1` endpoint
running on the same machine (default: http://127.0.0.1:6071/v1).

Phase G (devagentic issue #50): the active hermes profile name is bound
to devagentic's per-user vertical via the `X-User-Id` request header.
`get_active_profile_name()` from `hermes_cli.profiles` resolves
HERMES_HOME to the matching profile (e.g. `~/.hermes/profiles/alice` →
`"alice"`; `~/.hermes` → `"default"`). The result is sent as
`X-User-Id` on every devagentic request so alice's hermes session lands
in alice's devagentic vertical and bob's lands in bob's. Manual
`DEVAGENTIC_USER_ID` env beats the auto-binding for ops cases that need
to pin a specific user_id.

Env vars:
  DEVAGENTIC_API_KEY   bearer token; can be any value when devagentic
                        runs in trust-header mode (DEVAGENTIC_TRUST_HEADER=1)
                        — hermes doesn't need the per-user salt. Required
                        by hermes' auth probe even when devagentic doesn't
                        enforce it.
  DEVAGENTIC_BASE_URL  override the default base URL.
  DEVAGENTIC_USER_ID   override the auto-bound X-User-Id with a fixed
                        value. Use when the active hermes profile name
                        and the desired devagentic vertical diverge.
"""

import logging
import os
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)


# Phase G binding — resolve via lazy import so the plugin still imports
# in contexts where hermes_cli isn't on the path (the standalone
# devagentic-local probe in CI imports this module directly).
def _resolve_user_id() -> str | None:
    """Pick the X-User-Id to send to devagentic.

    Resolution order:
      1. `DEVAGENTIC_USER_ID` env var — manual override.
      2. `hermes_cli.profiles.get_active_profile_name()` — derived
         from HERMES_HOME path; returns `"default"` for the
         ~/.hermes root, or the profile name for
         `~/.hermes/profiles/<name>`.
      3. `None` — caller doesn't inject the header, and devagentic
         either falls back to its trust-header default or rejects
         the request.
    """
    override = (os.environ.get("DEVAGENTIC_USER_ID") or "").strip()
    if override:
        return override
    try:
        from hermes_cli.profiles import get_active_profile_name
        name = (get_active_profile_name() or "").strip()
        return name or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("devagentic-local: profile resolution failed: %s", exc)
        return None


def _resolve_terminal_env() -> str:
    """The execution backend hermes runs tools in — ``local`` / ``modal`` /
    ``docker`` / etc., matching the ``TERMINAL_ENV`` convention used across
    ``tools/``. Defaults to ``local`` when unset. Sent as ``X-Terminal-Env``
    so devagentic derives the client's backend and composes the matching
    env-contract preamble (issue #155 / devagentic#397).
    """
    return (os.environ.get("TERMINAL_ENV") or "local").strip().lower() or "local"


class DevagenticLocalProfile(ProviderProfile):
    """Devagentic-as-a-completion-provider.

    The devagentic service does its own graph-mediated routing internally —
    from this profile's perspective it's just another OpenAI-compatible
    endpoint. The `model` value selects a devagentic *role* (e.g.
    `devagentic/coder`, `devagentic/researcher`), not an upstream LLM.
    Devagentic resolves the role to an actual model via its leaderboard.
    """

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 4.0,
    ) -> list[str] | None:
        """Hit devagentic's /v1/models. If unreachable, fall back to fallback_models."""
        try:
            return super().fetch_models(api_key=api_key, timeout=timeout)
        except Exception as exc:
            logger.debug("fetch_models(devagentic-local): %s", exc)
            return None

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Inject per-request devagentic headers.

        - `X-User-Id` (when resolvable) from the active hermes profile
          name, binding the session to the right per-user vertical.
        - `X-Terminal-Env` (always) so devagentic derives the client's
          execution backend and composes the matching env-contract
          preamble per backend (issue #155 / devagentic#397).

        The OpenAI SDK accepts `extra_headers` as a per-request kwarg;
        we add it to the `top_level_kwargs` half of the tuple so the
        transport layer threads it onto each `chat.completions.create`
        call. Per-request (not client-construction) so a process that
        switches profiles or backends mid-session picks up the change
        on the next completion.
        """
        extra_body_additions: dict[str, Any] = {}
        top_level_kwargs: dict[str, Any] = {}
        headers: dict[str, str] = {}
        user_id = _resolve_user_id()
        if user_id:
            headers["X-User-Id"] = user_id
        headers["X-Terminal-Env"] = _resolve_terminal_env()
        if headers:
            top_level_kwargs["extra_headers"] = headers
        return extra_body_additions, top_level_kwargs


_base_url = os.environ.get("DEVAGENTIC_BASE_URL", "http://127.0.0.1:6071/v1").rstrip("/")

devagentic_local = DevagenticLocalProfile(
    name="devagentic-local",
    aliases=("dvg", "devagentic"),
    env_vars=("DEVAGENTIC_API_KEY",),
    display_name="Devagentic (local)",
    description="Graph-mediated completions from your devagentic vertical (self-hosted)",
    signup_url="http://127.0.0.1:6071/",
    base_url=_base_url,
    models_url=f"{_base_url}/models",
    fallback_models=(
        "devagentic/coder",
        "devagentic/researcher",
        "devagentic/orchestrator",
    ),
)

register_provider(devagentic_local)
