"""Tests for ``resolve_requested_provider`` priority order (issue #70).

Priority (highest to lowest):
  1. explicit ``requested`` arg (CLI ``--provider``)
  2. ``HERMES_DEFAULT_PROVIDER`` env (deployment-priority, beats config)
  3. ``model.provider`` from persisted config.yaml
  4. ``HERMES_INFERENCE_PROVIDER`` env (legacy, below config)
  5. ``"auto"``
"""
from __future__ import annotations

import pytest

from hermes_cli import runtime_provider


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Drop both env knobs so each case starts from a known floor."""
    monkeypatch.delenv("HERMES_DEFAULT_PROVIDER", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)


def _patch_config(monkeypatch, provider_value: object) -> None:
    monkeypatch.setattr(
        runtime_provider,
        "_get_model_config",
        lambda: ({"provider": provider_value}
                 if provider_value is not None else {}),
    )


# ---------------------------------------------------------------------------
# (1) explicit arg wins over everything
# ---------------------------------------------------------------------------

def test_explicit_request_wins_over_default_env(monkeypatch):
    monkeypatch.setenv("HERMES_DEFAULT_PROVIDER", "devagentic-local")
    _patch_config(monkeypatch, "openrouter")
    assert runtime_provider.resolve_requested_provider("nous") == "nous"


def test_explicit_request_wins_over_config(monkeypatch):
    _patch_config(monkeypatch, "openrouter")
    assert runtime_provider.resolve_requested_provider("anthropic") == "anthropic"


def test_explicit_request_lowercased_and_stripped(monkeypatch):
    _patch_config(monkeypatch, None)
    assert (runtime_provider.resolve_requested_provider("  OpenAI  ")
            == "openai")


# ---------------------------------------------------------------------------
# (2) HERMES_DEFAULT_PROVIDER beats config (the new behavior for #70)
# ---------------------------------------------------------------------------

def test_default_env_beats_config(monkeypatch):
    """The #70 deployment case: container env should win over the
    persisted openrouter default from a fresh-install config."""
    monkeypatch.setenv("HERMES_DEFAULT_PROVIDER", "devagentic-local")
    _patch_config(monkeypatch, "openrouter")
    assert (runtime_provider.resolve_requested_provider()
            == "devagentic-local")


def test_default_env_beats_legacy_inference_env(monkeypatch):
    """When both env vars are set, the deployment-priority one wins
    (legacy semantics for HERMES_INFERENCE_PROVIDER unchanged)."""
    monkeypatch.setenv("HERMES_DEFAULT_PROVIDER", "devagentic-local")
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "nous")
    _patch_config(monkeypatch, None)
    assert (runtime_provider.resolve_requested_provider()
            == "devagentic-local")


def test_default_env_lowercased_and_stripped(monkeypatch):
    monkeypatch.setenv("HERMES_DEFAULT_PROVIDER", "  Devagentic-Local  ")
    _patch_config(monkeypatch, "openrouter")
    assert (runtime_provider.resolve_requested_provider()
            == "devagentic-local")


def test_default_env_empty_string_falls_through_to_config(monkeypatch):
    """An empty value behaves the same as unset — falls through to
    the next priority layer (config in this case)."""
    monkeypatch.setenv("HERMES_DEFAULT_PROVIDER", "")
    _patch_config(monkeypatch, "openrouter")
    assert (runtime_provider.resolve_requested_provider()
            == "openrouter")


def test_default_env_whitespace_only_falls_through(monkeypatch):
    monkeypatch.setenv("HERMES_DEFAULT_PROVIDER", "   ")
    _patch_config(monkeypatch, "openrouter")
    assert (runtime_provider.resolve_requested_provider()
            == "openrouter")


# ---------------------------------------------------------------------------
# (3) config beats legacy env (preserves pre-#70 behavior)
# ---------------------------------------------------------------------------

def test_config_beats_inference_env_legacy(monkeypatch):
    """The legacy HERMES_INFERENCE_PROVIDER env stays BELOW config so
    a stale shell export does not shadow the user's last-saved
    selection. Behavior must not regress."""
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "nous")
    _patch_config(monkeypatch, "openrouter")
    assert (runtime_provider.resolve_requested_provider()
            == "openrouter")


def test_inference_env_used_when_no_config(monkeypatch):
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "nous")
    _patch_config(monkeypatch, None)
    assert runtime_provider.resolve_requested_provider() == "nous"


# ---------------------------------------------------------------------------
# (4) auto floor
# ---------------------------------------------------------------------------

def test_auto_when_nothing_set(monkeypatch):
    _patch_config(monkeypatch, None)
    assert runtime_provider.resolve_requested_provider() == "auto"


def test_auto_when_config_empty_string(monkeypatch):
    _patch_config(monkeypatch, "")
    assert runtime_provider.resolve_requested_provider() == "auto"


def test_auto_when_config_provider_is_non_string(monkeypatch):
    """Defensive: a non-string provider value in config (e.g.,
    accidentally `null` after a hand-edit) falls through."""
    _patch_config(monkeypatch, 42)
    assert runtime_provider.resolve_requested_provider() == "auto"
