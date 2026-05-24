"""Tests for the structural-empty terminal recovery (hermes-agent#67).

Verifies the decision logic for short-circuiting the empty-content retry
loop on structural failures (finish_reason=stop + no tool_calls + no
prior tool turn + tools attached). The decision itself is a guard
expression embedded in conversation_loop.py; rather than spinning up the
full conversation loop, these tests evaluate the same guard against
stubbed inputs.

This validates the OR/AND combination matches the comment in the source —
all five conditions must hold, with the one-shot flag preventing loops.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _structural_empty(
    *,
    truly_empty: bool,
    has_structured: bool,
    finish_reason: str,
    prior_was_tool: bool,
    agent,
) -> bool:
    """Mirror of the guard expression in conversation_loop.py around
    line 3600 (after C67 patch). Keep this function in sync with the
    `_structural_empty = (...)` block in the source; the test enforces
    the contract."""
    _tools_attached = bool(getattr(agent, "tools", None))
    return (
        truly_empty
        and not has_structured
        and finish_reason == "stop"
        and not prior_was_tool
        and _tools_attached
        and not getattr(agent, "_tools_empty_terminal_handled", False)
    )


def _agent(tools=("silo_query",), handled=False):
    return SimpleNamespace(
        tools=list(tools) if tools else None,
        _tools_empty_terminal_handled=handled,
    )


# ─── Positive: all conditions met → triggers ──────────────────

def test_full_match_triggers():
    """The poly-explorer failure shape: empty + stop + tools attached
    + not prior tool + first detection."""
    assert _structural_empty(
        truly_empty=True, has_structured=False,
        finish_reason="stop", prior_was_tool=False,
        agent=_agent(),
    )


# ─── Negative: each condition gates the trigger ───────────────

def test_non_empty_does_not_trigger():
    assert not _structural_empty(
        truly_empty=False, has_structured=False,
        finish_reason="stop", prior_was_tool=False,
        agent=_agent(),
    )


def test_has_structured_does_not_trigger():
    """Reasoning-only response should go through the existing
    thinking-prefill path, not structural-empty."""
    assert not _structural_empty(
        truly_empty=True, has_structured=True,
        finish_reason="stop", prior_was_tool=False,
        agent=_agent(),
    )


def test_finish_reason_length_does_not_trigger():
    """Truncated response is a separate failure mode (needs bigger
    context, not synthetic recovery)."""
    assert not _structural_empty(
        truly_empty=True, has_structured=False,
        finish_reason="length", prior_was_tool=False,
        agent=_agent(),
    )


def test_finish_reason_tool_calls_does_not_trigger():
    """tool_calls finish_reason means the model DID call a tool — that
    path is handled elsewhere; we shouldn't be here at all."""
    assert not _structural_empty(
        truly_empty=True, has_structured=False,
        finish_reason="tool_calls", prior_was_tool=False,
        agent=_agent(),
    )


def test_finish_reason_content_filter_does_not_trigger():
    """Content-filter refusal needs operator visibility (not synthetic
    explainer that hides the refusal)."""
    assert not _structural_empty(
        truly_empty=True, has_structured=False,
        finish_reason="content_filter", prior_was_tool=False,
        agent=_agent(),
    )


def test_prior_was_tool_does_not_trigger():
    """Existing post-tool-empty 'nudge to continue' path handles this
    case; don't shadow it with the structural-empty trigger."""
    assert not _structural_empty(
        truly_empty=True, has_structured=False,
        finish_reason="stop", prior_was_tool=True,
        agent=_agent(),
    )


def test_no_tools_attached_does_not_trigger():
    """If no tools were sent, the empty isn't structural — could be a
    transient hallucination or content-filter that didn't set the right
    finish_reason. Let the existing retry loop handle it."""
    assert not _structural_empty(
        truly_empty=True, has_structured=False,
        finish_reason="stop", prior_was_tool=False,
        agent=_agent(tools=None),
    )
    # Also empty tools list:
    assert not _structural_empty(
        truly_empty=True, has_structured=False,
        finish_reason="stop", prior_was_tool=False,
        agent=_agent(tools=()),
    )


def test_already_handled_does_not_re_trigger():
    """One-shot per session: once we've surfaced the synthetic recovery,
    don't surface it again. If the next iteration STILL empties, the
    existing 3-retry-then-fallback chain takes over."""
    assert not _structural_empty(
        truly_empty=True, has_structured=False,
        finish_reason="stop", prior_was_tool=False,
        agent=_agent(handled=True),
    )


# ─── Smoke: patch text actually contains the synthetic recovery ──

def test_patch_landed_correctly():
    """The C67 patch should have inserted the structural-empty block
    BEFORE the existing _truly_empty retry block, gated by the flag."""
    src = (Path(__file__).resolve().parents[2]
           / "agent" / "conversation_loop.py").read_text()
    assert "_tools_empty_terminal_handled" in src, \
        "C67 guard flag absent — patch did not land"
    assert "hermes-agent#67: structural empty" in src, \
        "C67 WARN log absent — patch did not land"
    assert "tools being attached; finish_reason=stop" in src, \
        "C67 synthetic assistant message absent — patch did not land"
    # Confirm ordering: structural-empty block precedes the legacy retry block.
    struct_idx = src.find("_structural_empty = (")
    legacy_idx = src.find("agent._empty_content_retries < 3:")
    assert struct_idx > 0 and legacy_idx > 0, "anchors missing"
    assert struct_idx < legacy_idx, \
        "structural-empty must precede legacy retry trigger"
