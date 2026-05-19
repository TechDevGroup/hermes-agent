"""Devagentic local provider profile.

Registers a `devagentic-local` provider in hermes' provider registry. The
profile points at a devagentic instance's OpenAI-compatible `/v1` endpoint
running on the same machine (default: http://127.0.0.1:6071/v1).

At v0 the devagentic-side `/v1/chat/completions` endpoint is a stub — the
provider plugin exists so the hermes-side wiring is ready when devagentic
finishes shipping its OpenAI-compat shim (tracked as Phase A in the
hermes↔devagentic integration plan).

Env vars:
  DEVAGENTIC_API_KEY   bearer token; can be any value while the v0 shim
                        accepts all tokens. Required by hermes' auth probe
                        even if devagentic doesn't enforce it yet.
  DEVAGENTIC_BASE_URL  override the default base URL.
"""

import logging
import os
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)


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
