"""Tests for the devagentic-vertical-preamble plugin (G1 / hermes-agent#55
+ hermes-agent#71 lazy-load refactor).

Verifies the post-#71 index-only rendering:
  * Full graft bodies are NOT in the preamble.
  * Index entries (graft_id + source + path + abstract) ARE.
  * Worker-guardrails stay fully inlined.
  * Loader teaches the worker about `grafted_context_fetch`.
  * Per-process gate + fail-soft contract unchanged.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest


_PLUGIN_DIR = (Path(__file__).resolve().parents[3]
               / "plugins" / "devagentic-vertical-preamble")


def _load_plugin_module(name: str):
    spec = importlib.util.spec_from_file_location(
        f"vertical_preamble_under_test.{name}",
        _PLUGIN_DIR / f"{name}.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def client_mod():
    return _load_plugin_module("client")


@pytest.fixture
def preamble_mod(monkeypatch):
    client = _load_plugin_module("client")
    spec = importlib.util.spec_from_file_location(
        "vertical_preamble_under_test.preamble",
        _PLUGIN_DIR / "preamble.py",
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "vertical_preamble_under_test"
    sys.modules["vertical_preamble_under_test"] = importlib.util.module_from_spec(
        importlib.util.spec_from_loader("vertical_preamble_under_test", loader=None)
    )
    sys.modules["vertical_preamble_under_test"].client = client
    spec.loader.exec_module(mod)
    mod.reset_loaded_cache()
    yield mod
    mod.reset_loaded_cache()


# ─── Gate: no user_id ─────────────────────────────────────────

def test_returns_none_when_no_user_id(preamble_mod, monkeypatch):
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "resolve_user_id", lambda: None,
    )
    assert preamble_mod.on_pre_llm_call() is None


def test_returns_none_for_default_profile(preamble_mod, monkeypatch):
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "resolve_user_id", lambda: "default",
    )
    assert preamble_mod.on_pre_llm_call() is None


# ─── Gate: empty rollup ───────────────────────────────────────

def test_returns_none_when_devagentic_returns_no_docs(preamble_mod, monkeypatch):
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "resolve_user_id", lambda: "u-empty",
    )
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "fetch_vertical_context",
        lambda uid: {"userId": uid, "verticalSpec": None,
                     "graftedContexts": [], "workerGuardrails": []},
    )
    assert preamble_mod.on_pre_llm_call() is None
    # And user_id is marked loaded — second call short-circuits.
    fetched = {"calls": 0}

    def _spy(uid):
        fetched["calls"] += 1
        return {"userId": uid, "verticalSpec": {"name": "x", "userId": uid},
                "graftedContexts": [], "workerGuardrails": []}
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "fetch_vertical_context", _spy,
    )
    assert preamble_mod.on_pre_llm_call() is None
    assert fetched["calls"] == 0, "second call short-circuits before fetch"


# ─── Index-only rendering (the v71 contract) ──────────────────

def test_returns_context_with_index_not_full_bodies(preamble_mod, monkeypatch):
    """The poly-explorer-scale case: preamble lists grafts as an index
    + abstract, NOT inlined full bodies. Worker fetches on demand."""
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "resolve_user_id", lambda: "polynomial-explorer",
    )
    long_body = (
        "Chinese Remainder Theorem (CRT) — residue tuples encode "
        "integers via simultaneous moduli. " * 100  # ~7KB content
    )
    rollup = {
        "userId": "polynomial-explorer",
        "verticalSpec": {
            "id": "doc-spec1", "userId": "polynomial-explorer",
            "name": "polynomial-explorer",
            "createdTs": "2026-05-23T00:00:00+00:00",
            "manifestPath": "/workspace/verticals/polynomial-explorer/manifest.json",
            "envPath": "/workspace/verticals/polynomial-explorer/.env",
            "graftSourceCount": 3,
        },
        "graftedContexts": [
            {"id": "doc-graft-a", "userId": "polynomial-explorer",
             "source": "file:///workspace/rnspak", "ref": "main",
             "sha": "abc12345", "path": "concepts/crt.md",
             "content": long_body,
             "ts": "2026-05-23T00:00:01+00:00"},
        ],
        "workerGuardrails": [
            {"id": "wg1", "userId": "polynomial-explorer",
             "constraints": "CONSTRAINT-1: blackbox backbone — never edit devagentic/hermes source.",
             "ts": "2026-05-23T00:00:00+00:00"},
        ],
    }
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "fetch_vertical_context", lambda uid: rollup,
    )
    result = preamble_mod.on_pre_llm_call()
    assert isinstance(result, dict)
    body = result.get("context")
    assert isinstance(body, str)
    # Index entry present
    assert "doc-graft-a" in body, "graft id appears in index"
    assert "concepts/crt.md" in body, "path appears in index"
    assert "file:///workspace/rnspak" in body, "source appears in index"
    # Loader teaches the fetch idiom
    assert "grafted_context_fetch" in body, (
        "preamble explicitly mentions the fetch tool"
    )
    # Worker-guardrails stay fully inlined
    assert "blackbox backbone" in body, "guardrails kept inline"
    # FULL body NOT in preamble — only the abstract (first 120 chars)
    # The repeated phrase "encode integers via simultaneous moduli."
    # is at char ~85 — within the 120-char abstract → should appear ONCE
    # (in the abstract), not 100 times (which would be the full body).
    full_phrase_count = body.count("encode integers via simultaneous moduli")
    assert full_phrase_count <= 1, (
        f"full body inlined ({full_phrase_count}× phrase) — should be ≤1 (abstract only)"
    )
    # Verify preamble is bounded — for poly-explorer (70 grafts) this
    # should be well under the 32KB safety net.
    assert len(body) < 5000, (
        f"preamble grew unexpectedly large ({len(body)} chars) "
        "— index rendering should be tight"
    )


def test_many_grafts_renders_all_in_index(preamble_mod, monkeypatch):
    """Unlike pre-#71 (capped at 8 grafts), index rendering shows ALL
    grafts — worker decides which to fetch."""
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "resolve_user_id", lambda: "u-many",
    )
    grafts = [
        {"id": f"doc-graft-{i:03d}", "userId": "u-many",
         "source": "file:///x", "ref": "main", "sha": "abc",
         "path": f"src/{i:03d}.md",
         "content": f"content body {i}", "ts": "2026-05-23"}
        for i in range(70)  # poly-explorer scale
    ]
    rollup = {
        "userId": "u-many", "verticalSpec": None,
        "graftedContexts": grafts, "workerGuardrails": [],
    }
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "fetch_vertical_context", lambda uid: rollup,
    )
    result = preamble_mod.on_pre_llm_call()
    body = result["context"]
    # Header announces total count (no "showing first N" suffix anymore)
    assert "70 docs" in body
    # Every graft id appears in the index — no cap
    for i in (0, 7, 35, 50, 69):
        assert f"doc-graft-{i:03d}" in body, f"index missing graft {i:03d}"
    # And the bound holds: 70 grafts × ~150 chars + headers ~= 11KB, well
    # within the 32KB safety net.
    assert len(body) < 32768


def test_abstract_truncated_to_120_chars(preamble_mod, monkeypatch):
    """Index abstract is first 120 chars + ellipsis when content longer."""
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "resolve_user_id", lambda: "u-trunc",
    )
    big = "X" * 500
    rollup = {
        "userId": "u-trunc", "verticalSpec": None,
        "graftedContexts": [{
            "id": "g1", "userId": "u-trunc",
            "source": "file:///x", "ref": "main", "sha": "a", "path": "x.md",
            "content": big, "ts": "2026-05-23",
        }],
        "workerGuardrails": [],
    }
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "fetch_vertical_context", lambda uid: rollup,
    )
    body = preamble_mod.on_pre_llm_call()["context"]
    # Should have at most 120 X's + ellipsis, NOT 500
    assert body.count("X") <= 121
    assert "…" in body, "ellipsis marker after truncation"


# ─── Gate: load-once ──────────────────────────────────────────

def test_does_not_re_inject_for_same_user_id(preamble_mod, monkeypatch):
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "resolve_user_id", lambda: "u-once",
    )
    rollup = {
        "userId": "u-once",
        "verticalSpec": {"id": "s", "userId": "u-once", "name": "v",
                         "createdTs": "2026-05-23T00:00:00+00:00",
                         "manifestPath": None, "envPath": None,
                         "graftSourceCount": 0},
        "graftedContexts": [],
        "workerGuardrails": [],
    }
    call_count = {"n": 0}

    def _counting(uid):
        call_count["n"] += 1
        return rollup
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "fetch_vertical_context", _counting,
    )
    first = preamble_mod.on_pre_llm_call()
    second = preamble_mod.on_pre_llm_call()
    assert first is not None and "context" in first
    assert second is None, "second call must not re-inject"
    assert call_count["n"] == 1, "fetch should only run once per user_id per process"


# ─── Failure modes ────────────────────────────────────────────

def test_fetch_exception_returns_none(preamble_mod, monkeypatch):
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "resolve_user_id", lambda: "u-boom",
    )

    def _boom(uid):
        raise RuntimeError("simulated network kaboom")
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "fetch_vertical_context", _boom,
    )
    assert preamble_mod.on_pre_llm_call() is None


def test_resolve_user_id_exception_returns_none(preamble_mod, monkeypatch):
    def _boom():
        raise RuntimeError("simulated profile kaboom")
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "resolve_user_id", _boom,
    )
    assert preamble_mod.on_pre_llm_call() is None


# ─── Client unit checks (unchanged from #59) ─────────────────

def test_client_user_id_env_override(client_mod, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "override-user")
    assert client_mod.resolve_user_id() == "override-user"


def test_client_fetch_returns_none_on_empty_user_id(client_mod):
    assert client_mod.fetch_vertical_context("") is None
