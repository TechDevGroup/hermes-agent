"""Tests for ``HERMES_DEFER_PERSONA`` narrowing in
``build_system_prompt_parts`` (issue #105).

When set, hermes-cli's system prompt drops SOUL.md +
DEFAULT_AGENT_IDENTITY + HERMES_AGENT_HELP / SKILLS / KANBAN /
SESSION_SEARCH guidance — devagentic's upstream R5 workflow-preamble
(or any external system-prompt source) becomes authoritative for
identity + persona. Strictly broader than the ``HERMES_INTENT_OVERRIDE=code``
narrowing (which keeps ``DEFAULT_AGENT_IDENTITY``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Set

import pytest

from agent import system_prompt as sp_mod
from agent.prompt_builder import (
    DEFAULT_AGENT_IDENTITY,
    HERMES_AGENT_HELP_GUIDANCE,
    KANBAN_GUIDANCE,
    MEMORY_GUIDANCE,
    SESSION_SEARCH_GUIDANCE,
    SKILLS_GUIDANCE,
)


@dataclass
class _FakeAgent:
    """Mirrors the minimal stand-in from
    test_system_prompt_intent_override.py."""
    valid_tool_names: Set[str] = field(default_factory=set)
    load_soul_identity: bool = True
    skip_context_files: bool = True
    provider: str = "openrouter"
    model: str = "claude-sonnet-4-5"
    platform: str = ""
    _tool_use_enforcement: object = False
    _memory_store: object = None
    _memory_manager: object = None
    _memory_enabled: bool = False
    _user_profile_enabled: bool = False
    _kanban_worker_guidance: Optional[str] = None
    pass_session_id: bool = False
    session_id: str = ""


@pytest.fixture
def _stub_run_agent(monkeypatch):
    import sys
    import types
    fake_ra = types.SimpleNamespace(
        load_soul_md=lambda: "[SOUL.md content marker — long persona block]",
        build_environment_hints=lambda: "",
        build_context_files_prompt=lambda cwd=None, skip_soul=False: "",
        build_nous_subscription_prompt=lambda tools: "",
        build_skills_system_prompt=lambda available_tools, available_toolsets: (
            "[skills_prompt — long block listing all skills]"),
        get_toolset_for_tool=lambda name: name,
    )
    monkeypatch.setitem(sys.modules, "run_agent", fake_ra)
    return fake_ra


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("HERMES_INTENT_OVERRIDE", raising=False)
    monkeypatch.delenv("HERMES_DEFER_PERSONA", raising=False)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def test_resolve_returns_false_when_unset(monkeypatch):
    monkeypatch.delenv("HERMES_DEFER_PERSONA", raising=False)
    assert sp_mod._resolve_persona_deferred() is False


def test_resolve_returns_false_for_empty(monkeypatch):
    monkeypatch.setenv("HERMES_DEFER_PERSONA", "")
    assert sp_mod._resolve_persona_deferred() is False


@pytest.mark.parametrize(
    "raw", ["1", "true", "TRUE", "Yes", "ON", "  yes  "])
def test_resolve_returns_true_for_truthy(monkeypatch, raw):
    monkeypatch.setenv("HERMES_DEFER_PERSONA", raw)
    assert sp_mod._resolve_persona_deferred() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "maybe", "kthx"])
def test_resolve_returns_false_for_falsy_or_unknown(monkeypatch, raw):
    monkeypatch.setenv("HERMES_DEFER_PERSONA", raw)
    assert sp_mod._resolve_persona_deferred() is False


# ---------------------------------------------------------------------------
# Narrowing — defer-persona DROPS DEFAULT_AGENT_IDENTITY (broader than code)
# ---------------------------------------------------------------------------

def test_defer_persona_drops_soul_md(monkeypatch, _stub_run_agent):
    monkeypatch.setenv("HERMES_DEFER_PERSONA", "1")
    agent = _FakeAgent(valid_tool_names={"skill_manage"})
    parts = sp_mod.build_system_prompt_parts(agent)
    assert "SOUL.md content marker" not in parts["stable"]


def test_defer_persona_drops_default_agent_identity(
        monkeypatch, _stub_run_agent):
    """Distinguishing test from code-intent narrowing: code keeps
    DEFAULT_AGENT_IDENTITY as a floor; defer-persona drops it
    entirely. The model has no built-in identity surface — upstream
    preamble owns it."""
    monkeypatch.setenv("HERMES_DEFER_PERSONA", "1")
    agent = _FakeAgent(valid_tool_names={"skill_manage"})
    parts = sp_mod.build_system_prompt_parts(agent)
    # The Nous Research identity string must be absent.
    assert "Nous Research" not in parts["stable"]
    assert "Hermes Agent" not in parts["stable"]
    # Sanity — the persona-identity FALLBACK didn't sneak back in.
    assert DEFAULT_AGENT_IDENTITY not in parts["stable"]


def test_code_intent_alone_keeps_default_agent_identity(
        monkeypatch, _stub_run_agent):
    """Counter-case: ``HERMES_INTENT_OVERRIDE=code`` without
    ``HERMES_DEFER_PERSONA`` keeps DEFAULT_AGENT_IDENTITY as floor."""
    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", "code")
    monkeypatch.delenv("HERMES_DEFER_PERSONA", raising=False)
    agent = _FakeAgent(valid_tool_names={"skill_manage"})
    parts = sp_mod.build_system_prompt_parts(agent)
    # SOUL.md is dropped (code-intent narrowing); DEFAULT_AGENT_IDENTITY
    # remains as the identity floor.
    assert "SOUL.md content marker" not in parts["stable"]
    assert DEFAULT_AGENT_IDENTITY[:40] in parts["stable"]


def test_defer_persona_drops_hermes_agent_help(
        monkeypatch, _stub_run_agent):
    monkeypatch.setenv("HERMES_DEFER_PERSONA", "true")
    agent = _FakeAgent(valid_tool_names={"skill_manage"})
    parts = sp_mod.build_system_prompt_parts(agent)
    assert HERMES_AGENT_HELP_GUIDANCE not in parts["stable"]


def test_defer_persona_drops_skills_prompt(
        monkeypatch, _stub_run_agent):
    monkeypatch.setenv("HERMES_DEFER_PERSONA", "yes")
    agent = _FakeAgent(valid_tool_names={"skill_manage", "skill_view"})
    parts = sp_mod.build_system_prompt_parts(agent)
    assert "skills_prompt" not in parts["stable"]


def test_defer_persona_drops_skills_session_kanban(
        monkeypatch, _stub_run_agent):
    monkeypatch.setenv("HERMES_DEFER_PERSONA", "on")
    agent = _FakeAgent(valid_tool_names={
        "skill_manage", "session_search", "kanban_show"})
    parts = sp_mod.build_system_prompt_parts(agent)
    assert SKILLS_GUIDANCE not in parts["stable"]
    assert SESSION_SEARCH_GUIDANCE not in parts["stable"]
    assert KANBAN_GUIDANCE not in parts["stable"]


def test_defer_persona_keeps_memory_guidance(
        monkeypatch, _stub_run_agent):
    """Memory is useful even when persona is deferred — keep it."""
    monkeypatch.setenv("HERMES_DEFER_PERSONA", "1")
    agent = _FakeAgent(valid_tool_names={"memory"})
    parts = sp_mod.build_system_prompt_parts(agent)
    assert MEMORY_GUIDANCE in parts["stable"]


def test_defer_persona_yields_strictly_smaller_than_code(
        monkeypatch, _stub_run_agent):
    """Defer-persona is strictly broader than code-intent narrowing:
    same code-narrowed sections drop, PLUS DEFAULT_AGENT_IDENTITY."""
    agent = _FakeAgent(valid_tool_names={
        "skill_manage", "skill_view", "skills_list", "memory",
        "session_search", "kanban_show",
    })

    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", "code")
    monkeypatch.delenv("HERMES_DEFER_PERSONA", raising=False)
    code_stable = sp_mod.build_system_prompt_parts(agent)["stable"]

    monkeypatch.delenv("HERMES_INTENT_OVERRIDE", raising=False)
    monkeypatch.setenv("HERMES_DEFER_PERSONA", "1")
    defer_stable = sp_mod.build_system_prompt_parts(agent)["stable"]

    assert len(defer_stable) < len(code_stable), (
        f"defer={len(defer_stable)}B, code={len(code_stable)}B")


def test_defer_persona_composes_with_intent_override(
        monkeypatch, _stub_run_agent):
    """Both env vars set together — defer-persona's broader narrowing
    applies (DEFAULT_AGENT_IDENTITY is still dropped)."""
    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", "code")
    monkeypatch.setenv("HERMES_DEFER_PERSONA", "1")
    agent = _FakeAgent(valid_tool_names={"skill_manage"})
    parts = sp_mod.build_system_prompt_parts(agent)
    assert "Nous Research" not in parts["stable"]
    assert DEFAULT_AGENT_IDENTITY not in parts["stable"]


def test_default_keeps_full_persona_when_neither_env_set(
        monkeypatch, _stub_run_agent):
    """Sanity counter-case: with no narrowing, SOUL.md AND
    DEFAULT_AGENT_IDENTITY's intent (via SOUL.md or fallback) are
    present."""
    monkeypatch.delenv("HERMES_INTENT_OVERRIDE", raising=False)
    monkeypatch.delenv("HERMES_DEFER_PERSONA", raising=False)
    agent = _FakeAgent(valid_tool_names={"skill_manage"})
    parts = sp_mod.build_system_prompt_parts(agent)
    assert "SOUL.md content marker" in parts["stable"]
