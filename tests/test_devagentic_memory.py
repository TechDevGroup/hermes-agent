"""Tests for the devagentic-graph memory adapter (devagentic #54).

Phase D hermes-side. Verifies that:

  * `graph_enabled()` honors `DEVAGENTIC_MEMORY_GRAPH`.
  * `query_user_facts()` returns [] when the gate is off,
    regardless of fixture state.
  * When enabled, returns the parsed `userFactQuery` array from
    a mocked GraphQL response.
  * Network failures, parse failures, empty query, GraphQL
    errors, and missing user_id all return [].
  * `create_user_fact()` posts the right GraphQL mutation and
    returns the new fact id on success.
  * User-id resolution uses `DEVAGENTIC_USER_ID` env override.
  * Migration helpers `_iter_memory_files` + `_today_tag` work
    as documented.
"""
from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from pathlib import Path

import pytest


@pytest.fixture
def adapter():
    """Import agent.devagentic_memory fresh per test so module-level
    env reads inside helpers don't leak across cases."""
    import agent.devagentic_memory as mod
    importlib.reload(mod)
    return mod


@pytest.fixture
def migrate_script():
    """Import scripts/migrate_memory_to_graph.py by path so its
    helpers are testable independent of the migration's main()."""
    repo_root = Path(__file__).resolve().parents[1]
    path = repo_root / "scripts" / "migrate_memory_to_graph.py"
    spec = importlib.util.spec_from_file_location(
        "migrate_memory_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# ─── Env gate ────────────────────────────────────────────────

def test_graph_enabled_truthy(adapter, monkeypatch):
    for v in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv(adapter.GRAPH_ENV, v)
        assert adapter.graph_enabled() is True, f"{v!r}"


def test_graph_enabled_falsy(adapter, monkeypatch):
    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv(adapter.GRAPH_ENV, v)
        assert adapter.graph_enabled() is False, f"{v!r}"
    monkeypatch.delenv(adapter.GRAPH_ENV, raising=False)
    assert adapter.graph_enabled() is False, "unset"


# ─── query_user_facts ────────────────────────────────────────

def test_query_user_facts_off_returns_empty(adapter, monkeypatch):
    monkeypatch.delenv(adapter.GRAPH_ENV, raising=False)
    monkeypatch.setattr(adapter, "_post_graphql",
                        lambda *a, **k: pytest.fail(
                            "_post_graphql ran with gate off"))
    assert adapter.query_user_facts("anything") == []


def test_query_user_facts_returns_array(adapter, monkeypatch):
    monkeypatch.setenv(adapter.GRAPH_ENV, "1")
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    fake = {
        "userFactQuery": [
            {"id": "f1", "body": "alice prefers terse",
             "tags": ["preference"], "source": "user-explicit",
             "confidence": 1.0},
            {"id": "f2", "body": "alice's tz is UTC-5",
             "tags": ["timezone"], "source": "hermes-curator",
             "confidence": 0.9},
        ]
    }
    monkeypatch.setattr(adapter, "_post_graphql",
                        lambda *a, **k: fake)
    rows = adapter.query_user_facts("terse preference timezone",
                                     top_k=5)
    assert len(rows) == 2
    assert rows[0]["body"] == "alice prefers terse"


def test_query_user_facts_empty_query(adapter, monkeypatch):
    monkeypatch.setenv(adapter.GRAPH_ENV, "1")
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    monkeypatch.setattr(adapter, "_post_graphql",
                        lambda *a, **k: pytest.fail(
                            "_post_graphql ran on empty query"))
    assert adapter.query_user_facts("") == []


def test_query_user_facts_network_failure(adapter, monkeypatch):
    monkeypatch.setenv(adapter.GRAPH_ENV, "1")
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    monkeypatch.setattr(adapter, "_post_graphql",
                        lambda *a, **k: None)
    assert adapter.query_user_facts("anything") == []


def test_query_user_facts_filters_blank_bodies(adapter, monkeypatch):
    monkeypatch.setenv(adapter.GRAPH_ENV, "1")
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    # Adapter should drop entries whose `body` is missing / empty.
    fake = {"userFactQuery": [
        {"id": "good", "body": "valid body", "tags": [],
         "source": "x", "confidence": 1.0},
        {"id": "blank", "body": "", "tags": []},
        {"id": "absent", "tags": []},
    ]}
    monkeypatch.setattr(adapter, "_post_graphql",
                        lambda *a, **k: fake)
    rows = adapter.query_user_facts("x")
    assert [r["id"] for r in rows] == ["good"]


def test_query_user_facts_no_user_id(adapter, monkeypatch):
    monkeypatch.setenv(adapter.GRAPH_ENV, "1")
    monkeypatch.delenv("DEVAGENTIC_USER_ID", raising=False)
    fake_mod = type(sys)("hermes_cli.profiles")
    fake_mod.get_active_profile_name = lambda: ""
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", fake_mod)
    monkeypatch.setattr(adapter, "_post_graphql",
                        lambda *a, **k: pytest.fail(
                            "_post_graphql ran without user_id"))
    assert adapter.query_user_facts("x") == []


# ─── create_user_fact ────────────────────────────────────────

def test_create_user_fact_posts_and_returns_id(adapter, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    captured: dict = {}

    def _capture(query, variables, **kwargs):
        captured["query"] = query
        captured["variables"] = variables
        return {"userFactCreate": {"id": "fact-xyz",
                                    "source": "test-source"}}
    monkeypatch.setattr(adapter, "_post_graphql", _capture)
    fid = adapter.create_user_fact(
        body="terse preferred",
        source="user-explicit",
        tags=["preference"], confidence=0.95)
    assert fid == "fact-xyz"
    assert "userFactCreate" in captured["query"]
    v = captured["variables"]
    assert v["u"] == "alice"
    assert v["b"] == "terse preferred"
    assert v["s"] == "user-explicit"
    assert v["t"] == ["preference"]
    assert v["c"] == 0.95


def test_create_user_fact_no_user_id_returns_none(
        adapter, monkeypatch):
    monkeypatch.delenv("DEVAGENTIC_USER_ID", raising=False)
    fake_mod = type(sys)("hermes_cli.profiles")
    fake_mod.get_active_profile_name = lambda: ""
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", fake_mod)
    assert adapter.create_user_fact("x", "y") is None


def test_create_user_fact_failure_returns_none(adapter, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    monkeypatch.setattr(adapter, "_post_graphql",
                        lambda *a, **k: None)
    assert adapter.create_user_fact("x", "y") is None


# ─── Migration helpers ──────────────────────────────────────

def test_iter_memory_files(migrate_script, tmp_path):
    (tmp_path / "MEMORY.md").write_text("memory content\n")
    (tmp_path / "USER.md").write_text("user content\n")
    (tmp_path / "SOUL.md").write_text("soul content\n")
    files = migrate_script._iter_memory_files(tmp_path)
    names = sorted(p.name for p in files)
    assert names == ["MEMORY.md", "SOUL.md", "USER.md"]


def test_iter_memory_files_skips_empty(migrate_script, tmp_path):
    (tmp_path / "MEMORY.md").write_text("memory content\n")
    (tmp_path / "USER.md").write_text("   \n   \n")  # whitespace
    files = migrate_script._iter_memory_files(tmp_path)
    names = [p.name for p in files]
    assert names == ["MEMORY.md"]


def test_iter_memory_files_skips_missing(migrate_script, tmp_path):
    (tmp_path / "MEMORY.md").write_text("just memory")
    files = migrate_script._iter_memory_files(tmp_path)
    assert [p.name for p in files] == ["MEMORY.md"]


def test_today_tag_format(migrate_script):
    tag = migrate_script._today_tag()
    assert re.match(r"^migration:\d{4}-\d{2}-\d{2}$", tag), tag
