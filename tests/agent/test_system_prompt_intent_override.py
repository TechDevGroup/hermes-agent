"""Tests for ``HERMES_INTENT_OVERRIDE`` narrowing in
``build_system_prompt_parts`` (issue #97 / #89 Direction A).

When the env var is set to ``code``, the ``stable`` layer is narrowed:
SOUL.md is skipped (DEFAULT_AGENT_IDENTITY floor), ``HERMES_AGENT_HELP``
+ ``SKILLS_GUIDANCE`` + ``KANBAN_GUIDANCE`` + ``SESSION_SEARCH_GUIDANCE``
+ skills_prompt are dropped. Tool-use enforcement + per-model
operational guidance + env/platform hints stay.
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
    """Minimal agent stand-in for ``build_system_prompt_parts``."""
    valid_tool_names: Set[str] = field(default_factory=set)
    load_soul_identity: bool = True
    skip_context_files: bool = True
    provider: str = "openrouter"
    model: str = "claude-sonnet-4-5"
    platform: str = ""
    _tool_use_enforcement: object = False  # disable so per-model add-ons stay quiet
    _memory_store: object = None
    _memory_manager: object = None
    _memory_enabled: bool = False
    _user_profile_enabled: bool = False
    _kanban_worker_guidance: Optional[str] = None
    pass_session_id: bool = False
    session_id: str = ""


@pytest.fixture
def _stub_run_agent(monkeypatch):
    """Stub out the ``_ra()`` lookups so the system-prompt assembly
    doesn't actually walk the filesystem for SOUL.md / context files
    / nous-subscription / etc."""
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


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def test_resolve_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("HERMES_INTENT_OVERRIDE", raising=False)
    assert sp_mod._resolve_intent_override() is None


def test_resolve_returns_none_for_empty(monkeypatch):
    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", "")
    assert sp_mod._resolve_intent_override() is None


def test_resolve_returns_none_for_whitespace(monkeypatch):
    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", "   ")
    assert sp_mod._resolve_intent_override() is None


def test_resolve_returns_none_for_unknown(monkeypatch):
    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", "coding")  # close but wrong
    assert sp_mod._resolve_intent_override() is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("code", "code"),
        ("CODE", "code"),
        ("  Confer  ", "confer"),
        ("planning", "planning"),
        ("exploration", "exploration"),
        ("refinement", "refinement"),
        ("generic", "generic"),
    ],
)
def test_resolve_normalizes_valid_keys(monkeypatch, raw, expected):
    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", raw)
    assert sp_mod._resolve_intent_override() == expected


# ---------------------------------------------------------------------------
# Narrowing behavior — code intent drops the listed sections
# ---------------------------------------------------------------------------

def test_code_intent_drops_soul_md(monkeypatch, _stub_run_agent):
    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", "code")
    agent = _FakeAgent(valid_tool_names={"skill_manage"})
    parts = sp_mod.build_system_prompt_parts(agent)
    assert "SOUL.md content marker" not in parts["stable"]
    # Falls back to DEFAULT_AGENT_IDENTITY floor.
    assert DEFAULT_AGENT_IDENTITY[:40] in parts["stable"]


def test_default_intent_keeps_soul_md(monkeypatch, _stub_run_agent):
    monkeypatch.delenv("HERMES_INTENT_OVERRIDE", raising=False)
    agent = _FakeAgent(valid_tool_names={"skill_manage"})
    parts = sp_mod.build_system_prompt_parts(agent)
    assert "SOUL.md content marker" in parts["stable"]


def test_code_intent_drops_hermes_agent_help(
        monkeypatch, _stub_run_agent):
    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", "code")
    agent = _FakeAgent(valid_tool_names={"skill_manage"})
    parts = sp_mod.build_system_prompt_parts(agent)
    assert HERMES_AGENT_HELP_GUIDANCE not in parts["stable"]


def test_code_intent_drops_skills_prompt(monkeypatch, _stub_run_agent):
    """The skills_prompt block is the biggest single contributor to
    prompt mass when many skills are loaded — verify it's gone under
    code-intent narrowing."""
    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", "code")
    agent = _FakeAgent(valid_tool_names={"skill_manage", "skill_view"})
    parts = sp_mod.build_system_prompt_parts(agent)
    assert "skills_prompt" not in parts["stable"]


def test_code_intent_drops_skills_guidance(
        monkeypatch, _stub_run_agent):
    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", "code")
    agent = _FakeAgent(valid_tool_names={"skill_manage"})
    parts = sp_mod.build_system_prompt_parts(agent)
    assert SKILLS_GUIDANCE not in parts["stable"]


def test_code_intent_drops_session_search_guidance(
        monkeypatch, _stub_run_agent):
    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", "code")
    agent = _FakeAgent(valid_tool_names={"session_search"})
    parts = sp_mod.build_system_prompt_parts(agent)
    assert SESSION_SEARCH_GUIDANCE not in parts["stable"]


def test_code_intent_drops_kanban_guidance(
        monkeypatch, _stub_run_agent):
    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", "code")
    agent = _FakeAgent(valid_tool_names={"kanban_show"})
    parts = sp_mod.build_system_prompt_parts(agent)
    assert KANBAN_GUIDANCE not in parts["stable"]


def test_code_intent_keeps_memory_guidance(
        monkeypatch, _stub_run_agent):
    """MEMORY_GUIDANCE stays even under narrowing — small block,
    sometimes useful for coding-with-memory workers."""
    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", "code")
    agent = _FakeAgent(valid_tool_names={"memory"})
    parts = sp_mod.build_system_prompt_parts(agent)
    assert MEMORY_GUIDANCE in parts["stable"]


def test_code_intent_keeps_kanban_worker_guidance_when_no_narrowing(
        monkeypatch, _stub_run_agent):
    """Sanity counter-case: without the env override, the kanban
    block is present (so narrowing's drop is real, not a side
    effect of fixture setup)."""
    monkeypatch.delenv("HERMES_INTENT_OVERRIDE", raising=False)
    agent = _FakeAgent(valid_tool_names={"kanban_show"})
    parts = sp_mod.build_system_prompt_parts(agent)
    assert KANBAN_GUIDANCE in parts["stable"]


def test_code_intent_narrows_byte_count(monkeypatch, _stub_run_agent):
    """End-to-end: with all the narrowed-by-#97 blocks active, the
    code-intent variant is strictly smaller than the default."""
    agent = _FakeAgent(valid_tool_names={
        "skill_manage", "skill_view", "skills_list", "memory",
        "session_search", "kanban_show",
    })

    monkeypatch.delenv("HERMES_INTENT_OVERRIDE", raising=False)
    default_stable = sp_mod.build_system_prompt_parts(agent)["stable"]

    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", "code")
    narrowed_stable = sp_mod.build_system_prompt_parts(agent)["stable"]

    assert len(narrowed_stable) < len(default_stable), (
        f"narrowed={len(narrowed_stable)}B, "
        f"default={len(default_stable)}B")


# ---------------------------------------------------------------------------
# Pass-through for non-code intents (v1 only narrows on code)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "intent", ["confer", "planning", "exploration", "refinement", "generic"])
def test_non_code_intents_keep_default_prompt(
        monkeypatch, _stub_run_agent, intent):
    """v1 only narrows on `code`; other valid intents are recognized
    but pass through with the unchanged default prompt shape."""
    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", intent)
    agent = _FakeAgent(valid_tool_names={"skill_manage"})
    parts = sp_mod.build_system_prompt_parts(agent)
    # SOUL.md should be present (no narrowing).
    assert "SOUL.md content marker" in parts["stable"]
    # SKILLS_GUIDANCE should be present (skill_manage is in tools).
    assert SKILLS_GUIDANCE in parts["stable"]


def test_unknown_intent_falls_back_to_default(
        monkeypatch, _stub_run_agent):
    """Unknown env value (typo) → resolver returns None → no narrowing."""
    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", "kode")  # typo
    agent = _FakeAgent(valid_tool_names={"skill_manage"})
    parts = sp_mod.build_system_prompt_parts(agent)
    assert "SOUL.md content marker" in parts["stable"]
    assert SKILLS_GUIDANCE in parts["stable"]
