"""Tests for the _diagnose_empty_response helper (hermes-agent#67)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


_MODULE_PATH = (Path(__file__).resolve().parents[2]
                / "agent" / "conversation_loop.py")


@pytest.fixture(scope="module")
def diag():
    # Import the helper by-path so we don't drag in the whole module's
    # heavy side-effect import chain at test discovery time.
    spec = importlib.util.spec_from_file_location(
        "conversation_loop_for_diag_test", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        # conversation_loop.py is a 200KB+ file with many imports; if the
        # module-level execution fails for environmental reasons in this
        # test harness, fall back to extracting just the helper. The
        # helper is a pure function, so a textual extraction is sufficient.
        src = _MODULE_PATH.read_text()
        marker = "def _diagnose_empty_response("
        start = src.index(marker)
        # The helper ends at the next blank-line + non-indent. Slice
        # generously and exec.
        end = src.index("\ndef ", start + 1)
        helper_src = src[start:end] + "\n"
        ns: dict = {}
        exec(helper_src, ns)  # noqa: S102
        mod = SimpleNamespace(**ns)
    return mod._diagnose_empty_response


def _agent(provider="openai"):
    return SimpleNamespace(provider=provider, model="m")


# ─── Tool-calls count extraction ──────────────────────────────

def test_object_with_tool_calls_list(diag):
    msg = SimpleNamespace(tool_calls=[1, 2, 3], id="resp-abc")
    out = diag(assistant_message=msg, final_response="",
               agent=_agent(), finish_reason="tool_calls",
               prior_was_tool=False)
    assert out["tool_calls_count"] == 3
    assert out["finish_reason"] == "tool_calls"
    assert out["response_id"] == "resp-abc"
    assert out["response_len"] == 0
    assert out["prior_was_tool"] is False
    assert out["provider"] == "openai"


def test_object_with_no_tool_calls(diag):
    msg = SimpleNamespace(tool_calls=None, id="r-1")
    out = diag(assistant_message=msg, final_response="",
               agent=_agent(), finish_reason="stop",
               prior_was_tool=False)
    assert out["tool_calls_count"] == 0


def test_dict_message_shape(diag):
    msg = {"tool_calls": [{}, {}], "id": "dict-r-1"}
    out = diag(assistant_message=msg, final_response="x",
               agent=_agent(), finish_reason="stop",
               prior_was_tool=False)
    assert out["tool_calls_count"] == 2
    assert out["response_id"] == "dict-r-1"
    assert out["response_len"] == 1


def test_dict_message_no_tool_calls(diag):
    msg = {"id": "r"}
    out = diag(assistant_message=msg, final_response="",
               agent=_agent(), finish_reason="stop",
               prior_was_tool=False)
    assert out["tool_calls_count"] == 0


# ─── Fail-soft / sentinel paths ───────────────────────────────

def test_missing_id_sentinels(diag):
    msg = SimpleNamespace(tool_calls=None)  # no id attr
    out = diag(assistant_message=msg, final_response=None,
               agent=_agent(), finish_reason=None,
               prior_was_tool=False)
    assert out["response_id"] == "?"
    assert out["finish_reason"] == "?"
    assert out["response_len"] == 0  # None → ""


def test_weird_object_doesnt_raise(diag):
    class _Weird:
        @property
        def tool_calls(self):
            raise RuntimeError("explosive getter")

        @property
        def id(self):
            raise RuntimeError("explosive id")

    out = diag(assistant_message=_Weird(), final_response="ok",
               agent=_agent(), finish_reason="stop",
               prior_was_tool=False)
    assert out["tool_calls_count"] == "?"
    assert out["response_id"] == "?"
    assert out["response_len"] == 2


def test_agent_without_provider_attr(diag):
    """provider lookup fails-soft to "?" if the agent doesn't carry it."""
    agent = SimpleNamespace()  # no provider attr
    out = diag(assistant_message=SimpleNamespace(tool_calls=None),
               final_response="x", agent=agent,
               finish_reason="stop", prior_was_tool=False)
    # getattr default is the second arg → "?" via _safe wrapper
    assert out["provider"] == "?"


# ─── prior_was_tool propagation ──────────────────────────────

def test_prior_was_tool_propagates(diag):
    out = diag(assistant_message=SimpleNamespace(tool_calls=None),
               final_response="", agent=_agent(),
               finish_reason="stop", prior_was_tool=True)
    assert out["prior_was_tool"] is True


# ─── Realistic shape ──────────────────────────────────────────

def test_realistic_openai_empty_with_tool_calls(diag):
    """The actual scenario from #67: response has tool_calls but
    final_response is empty — diagnostic should distinguish this from
    a true-empty response."""
    msg = SimpleNamespace(
        tool_calls=[SimpleNamespace(id="tc1", function=None)],
        id="chatcmpl-67",
        content="",
    )
    out = diag(assistant_message=msg, final_response="",
               agent=_agent("devagentic-local"),
               finish_reason="tool_calls", prior_was_tool=False)
    assert out["tool_calls_count"] == 1
    assert out["finish_reason"] == "tool_calls"
    assert out["response_id"] == "chatcmpl-67"
    assert out["provider"] == "devagentic-local"
    assert out["response_len"] == 0
