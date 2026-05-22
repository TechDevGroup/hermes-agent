"""Tests for the devagentic-graph skill adapter (devagentic issue #52).

Phase C hermes-side. Verifies that:

  * `graph_enabled()` honors `DEVAGENTIC_SKILLS_GRAPH`.
  * `resolve_skill_body()` returns None when the env gate is off,
    regardless of fixture state.
  * When enabled, `resolve_skill_body()` returns the body field
    from a mocked GraphQL response.
  * Network failures, parse failures, and GraphQL errors all
    return None (caller falls back to file).
  * `create_skill()` posts the right GraphQL mutation and
    returns the new skill id on success.
  * User-id resolution uses `DEVAGENTIC_USER_ID` env override
    when set.
  * Migration script's `_iter_skill_files` and `_skill_name`
    helpers cover both layouts (`<name>.md` and `<dir>/SKILL.md`).
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def adapter():
    """Import agent.devagentic_skills fresh per test so module-level
    env reads inside helpers don't leak across cases."""
    import agent.devagentic_skills as mod
    importlib.reload(mod)
    return mod


@pytest.fixture
def migrate_script():
    """Import scripts/migrate_skills_to_graph.py by path so its
    helpers are testable independent of the migration's main()
    entrypoint."""
    repo_root = Path(__file__).resolve().parents[1]
    path = repo_root / "scripts" / "migrate_skills_to_graph.py"
    spec = importlib.util.spec_from_file_location(
        "migrate_skills_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# ─── Env gate ────────────────────────────────────────────────────────────

def test_graph_enabled_truthy_values(adapter, monkeypatch):
    for v in ("1", "true", "yes", "on", "TRUE", "On"):
        monkeypatch.setenv(adapter.GRAPH_ENV, v)
        assert adapter.graph_enabled() is True, f"{v!r} should enable"


def test_graph_enabled_falsy_values(adapter, monkeypatch):
    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv(adapter.GRAPH_ENV, v)
        assert adapter.graph_enabled() is False, f"{v!r} should disable"
    monkeypatch.delenv(adapter.GRAPH_ENV, raising=False)
    assert adapter.graph_enabled() is False, "unset → disabled"


# ─── resolve_skill_body ──────────────────────────────────────────────────

def test_resolve_skill_body_off_returns_none(adapter, monkeypatch):
    monkeypatch.delenv(adapter.GRAPH_ENV, raising=False)
    # Even with a populated _post_graphql, the env gate short-circuits.
    monkeypatch.setattr(adapter, "_post_graphql",
                        lambda *a, **k: pytest.fail(
                            "_post_graphql should not run when gate off"))
    assert adapter.resolve_skill_body("any-skill") is None


def test_resolve_skill_body_on_returns_body(adapter, monkeypatch):
    monkeypatch.setenv(adapter.GRAPH_ENV, "1")
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    fake_data = {
        "skillResolve": {
            "id": "skill-abc", "name": "refactor",
            "body": "Use tests as the spec.", "version": 1,
            "tags": ["coding"],
        }
    }
    monkeypatch.setattr(adapter, "_post_graphql",
                        lambda *a, **k: fake_data)
    body = adapter.resolve_skill_body("refactor")
    assert body == "Use tests as the spec."


def test_resolve_skill_body_missing_skill_returns_none(adapter, monkeypatch):
    monkeypatch.setenv(adapter.GRAPH_ENV, "1")
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    # devagentic returns `{ "skillResolve": null }` when no match.
    monkeypatch.setattr(adapter, "_post_graphql",
                        lambda *a, **k: {"skillResolve": None})
    assert adapter.resolve_skill_body("not-here") is None


def test_resolve_skill_body_network_failure_returns_none(adapter, monkeypatch):
    monkeypatch.setenv(adapter.GRAPH_ENV, "1")
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    monkeypatch.setattr(adapter, "_post_graphql",
                        lambda *a, **k: None)
    assert adapter.resolve_skill_body("anything") is None


def test_resolve_skill_body_empty_name_returns_none(adapter, monkeypatch):
    monkeypatch.setenv(adapter.GRAPH_ENV, "1")
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    monkeypatch.setattr(adapter, "_post_graphql",
                        lambda *a, **k: pytest.fail(
                            "_post_graphql should not run for empty name"))
    assert adapter.resolve_skill_body("") is None


def test_resolve_skill_body_no_user_id_returns_none(adapter, monkeypatch):
    monkeypatch.setenv(adapter.GRAPH_ENV, "1")
    monkeypatch.delenv("DEVAGENTIC_USER_ID", raising=False)
    # Force hermes_cli.profiles import path to yield no name.
    fake_mod = type(sys)("hermes_cli.profiles")
    fake_mod.get_active_profile_name = lambda: ""
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", fake_mod)
    monkeypatch.setattr(adapter, "_post_graphql",
                        lambda *a, **k: pytest.fail(
                            "_post_graphql should not run without user_id"))
    assert adapter.resolve_skill_body("x") is None


# ─── create_skill ────────────────────────────────────────────────────────

def test_create_skill_posts_mutation_and_returns_id(adapter, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    captured: dict = {}

    def _capture(query, variables, **kwargs):
        captured["query"] = query
        captured["variables"] = variables
        return {"skillCreate": {"id": "skill-xyz", "name": "ratchet",
                                 "version": 1}}
    monkeypatch.setattr(adapter, "_post_graphql", _capture)
    sid = adapter.create_skill(
        "ratchet", "skill body text",
        tags=["coding", "process"], authored_by="alice",
    )
    assert sid == "skill-xyz"
    assert "skillCreate" in captured["query"]
    v = captured["variables"]
    assert v["u"] == "alice"
    assert v["n"] == "ratchet"
    assert v["b"] == "skill body text"
    assert v["t"] == ["coding", "process"]
    assert v["ab"] == "alice"


def test_create_skill_returns_none_on_failure(adapter, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    monkeypatch.setattr(adapter, "_post_graphql",
                        lambda *a, **k: None)
    assert adapter.create_skill("x", "y") is None


# ─── Migration helpers ──────────────────────────────────────────────────

def test_iter_skill_files_flat_and_subdir_layouts(
        migrate_script, tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "alpha.md").write_text("alpha body")
    (skills / "beta.md").write_text("beta body")
    (skills / "complex").mkdir()
    (skills / "complex" / "SKILL.md").write_text("complex body")
    # A subdirectory without a SKILL.md should be skipped.
    (skills / "skip-me").mkdir()
    files = migrate_script._iter_skill_files(skills)
    paths = sorted(p.name + ":" + p.parent.name for p in files)
    assert paths == [
        "SKILL.md:complex", "alpha.md:skills", "beta.md:skills",
    ]


def test_skill_name_derivation(migrate_script, tmp_path):
    flat = tmp_path / "refactor-tips.md"
    nested = tmp_path / "auth-rules" / "SKILL.md"
    assert migrate_script._skill_name(flat) == "refactor-tips"
    assert migrate_script._skill_name(nested) == "auth-rules"


def test_iter_skill_files_missing_root(migrate_script, tmp_path):
    """Missing skills directory should not raise — script should
    just report 0 files and exit cleanly."""
    assert migrate_script._iter_skill_files(tmp_path / "nope") == []
