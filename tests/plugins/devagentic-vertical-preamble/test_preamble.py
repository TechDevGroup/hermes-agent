"""Tests for the devagentic-vertical-preamble plugin (G1 / hermes-agent#55).

Verifies:
  * The pre_llm_call hook returns None when no user_id resolves.
  * Returns None for the "default" profile (no vertical scoping).
  * Returns None when devagentic returns no docs (and marks the
    user_id as loaded so we don't re-query).
  * Returns a {"context": ...} dict with the rendered markdown
    when the rollup is populated.
  * Does NOT re-inject for the same user_id on a second call in
    the same process.
  * Caps per-graft content and total preamble length.
  * Renders worker-guardrails docs verbatim.
  * Fails soft on network errors (returns None, doesn't raise).
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
    """Load a plugin module by file path. The plugin lives under
    plugins/devagentic-vertical-preamble/ which isn't a regular
    importable package from tests (hyphen in dir name), so we
    import-by-path."""
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
    # client must load first so preamble's relative import resolves
    client = _load_plugin_module("client")
    spec = importlib.util.spec_from_file_location(
        "vertical_preamble_under_test.preamble",
        _PLUGIN_DIR / "preamble.py",
    )
    mod = importlib.util.module_from_spec(spec)
    # Wire the relative `from . import client as vertical_client`
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
    # Even if fetch was re-wired to return rich data, no preamble.
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
    assert fetched["calls"] == 0, "second call should short-circuit before fetch"


# ─── Happy path: populated rollup ─────────────────────────────

def test_returns_context_dict_with_rendered_markdown(preamble_mod, monkeypatch):
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "resolve_user_id", lambda: "polynomial-explorer",
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
            {"id": "g1", "userId": "polynomial-explorer",
             "source": "file:///workspace/rnspak", "ref": "main",
             "sha": "abc12345", "path": "concepts/crt.md",
             "content": "## Chinese Remainder Theorem\nResidues tuple form...",
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
    assert "polynomial-explorer" in body
    assert "Chinese Remainder Theorem" in body
    assert "blackbox backbone" in body
    assert "concepts/crt.md" in body


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


# ─── Caps ─────────────────────────────────────────────────────

def test_per_graft_content_truncated(preamble_mod, monkeypatch):
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "resolve_user_id", lambda: "u-big",
    )
    big_content = "A" * 100_000
    rollup = {
        "userId": "u-big",
        "verticalSpec": None,
        "graftedContexts": [
            {"id": "g1", "userId": "u-big", "source": "file:///x",
             "ref": "main", "sha": "abc", "path": "x.md",
             "content": big_content, "ts": "2026-05-23"},
        ],
        "workerGuardrails": [],
    }
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "fetch_vertical_context", lambda uid: rollup,
    )
    result = preamble_mod.on_pre_llm_call()
    assert result is not None
    body = result["context"]
    assert "truncated" in body, "per-graft truncation marker present"
    assert len(body) < 100_000


def test_at_most_n_grafts_rendered(preamble_mod, monkeypatch):
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "resolve_user_id", lambda: "u-many",
    )
    grafts = [
        {"id": f"g{i}", "userId": "u-many", "source": "file:///x",
         "ref": "main", "sha": "abc", "path": f"{i:03d}.md",
         "content": "tiny", "ts": "2026-05-23"}
        for i in range(20)
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
    # Header should mention "20 docs, showing first 8"
    assert "20" in body
    assert "showing first 8" in body
    # First-8 paths present, last-12 absent
    assert "007.md" in body
    assert "008.md" not in body


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
    # Hook must catch + return None, not propagate.
    assert preamble_mod.on_pre_llm_call() is None


def test_resolve_user_id_exception_returns_none(preamble_mod, monkeypatch):
    def _boom():
        raise RuntimeError("simulated profile kaboom")
    monkeypatch.setattr(
        sys.modules["vertical_preamble_under_test"].client,
        "resolve_user_id", _boom,
    )
    assert preamble_mod.on_pre_llm_call() is None


# ─── Client unit checks ───────────────────────────────────────

def test_client_user_id_env_override(client_mod, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "override-user")
    assert client_mod.resolve_user_id() == "override-user"


def test_client_user_id_missing(client_mod, monkeypatch):
    monkeypatch.delenv("DEVAGENTIC_USER_ID", raising=False)
    # Force the hermes_cli profile fallback to fail
    monkeypatch.setattr(
        client_mod, "_api_key", lambda: "")

    def _no_profile():
        raise ImportError("no hermes_cli")
    # Monkey-patching the import is fragile across reloads; rely on
    # the try/except in resolve_user_id and stub the profile-getter.
    import builtins
    real_import = builtins.__import__

    def _fake_import(name, *a, **k):
        if name == "hermes_cli.profiles":
            raise ImportError("blocked for test")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _fake_import)
    assert client_mod.resolve_user_id() is None


def test_client_fetch_returns_none_on_empty_user_id(client_mod):
    assert client_mod.fetch_vertical_context("") is None
    assert client_mod.fetch_vertical_context(None) is None  # type: ignore[arg-type]
