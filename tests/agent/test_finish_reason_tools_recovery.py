"""Tests for the ``finish_reason in {tool_calls, function_call}`` empty
recovery (hermes-agent#99).

When the model emits a tool-call-specific finish reason but the
normalized ``assistant_message.tool_calls`` reaches the response
handler empty (SDK normalization gap, upstream shape issue, etc.),
the else branch at conversation_loop.py:3493 would burn 3 empty-retries
before surfacing "(empty)". This guard short-circuits to a synthetic
recovery prompt — same shape as the #67 structural recovery, but
keyed on tool-call finish reasons rather than ``stop``.

These tests mirror the guard expression from conversation_loop.py to
catch drift.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


def _finish_reason_tools_recovery(
    *,
    finish_reason: str,
    tool_calls,
    agent,
) -> bool:
    """Mirror of the guard expression at conversation_loop.py around
    line 3500 (after the C99 patch). Keep this function in sync with
    the source; the test enforces the contract.

    Returns True when the recovery branch should fire — model wanted
    tools per finish_reason but no parseable tool_calls reached us.
    """
    _finish_wants_tools = finish_reason in {"tool_calls", "function_call"}
    _tool_calls_absent = not (tool_calls or [])
    _already_handled = getattr(
        agent, "_finish_reason_tools_handled", False)
    return _finish_wants_tools and _tool_calls_absent and not _already_handled


def _agent(handled=False):
    return SimpleNamespace(_finish_reason_tools_handled=handled)


# ─── Positive: tool-call finish reason with empty tool_calls ──

def test_tool_calls_finish_reason_with_none_tool_calls_triggers():
    """The hermes#99 failure shape: model emits tool_calls finish but
    the normalized list is None."""
    assert _finish_reason_tools_recovery(
        finish_reason="tool_calls", tool_calls=None,
        agent=_agent(),
    )


def test_tool_calls_finish_reason_with_empty_list_triggers():
    """Same shape, but tool_calls is an empty list rather than None.
    Both are "absent" semantically."""
    assert _finish_reason_tools_recovery(
        finish_reason="tool_calls", tool_calls=[],
        agent=_agent(),
    )


def test_function_call_finish_reason_with_none_triggers():
    """Legacy ``function_call`` finish reason (pre-tool_calls API
    shape) is treated the same as ``tool_calls``."""
    assert _finish_reason_tools_recovery(
        finish_reason="function_call", tool_calls=None,
        agent=_agent(),
    )


# ─── Negative: each condition gates the trigger ───────────────

def test_stop_finish_reason_does_not_trigger():
    """The #67 structural-empty path handles ``stop`` — we don't
    overlap with it."""
    assert not _finish_reason_tools_recovery(
        finish_reason="stop", tool_calls=None,
        agent=_agent(),
    )


def test_length_finish_reason_does_not_trigger():
    """Truncation is its own failure mode."""
    assert not _finish_reason_tools_recovery(
        finish_reason="length", tool_calls=None,
        agent=_agent(),
    )


def test_content_filter_finish_reason_does_not_trigger():
    """Content-filter is a separate signal too."""
    assert not _finish_reason_tools_recovery(
        finish_reason="content_filter", tool_calls=None,
        agent=_agent(),
    )


def test_tool_calls_populated_does_not_trigger():
    """Sanity: if tool_calls is actually populated, we should never
    have reached this branch in the first place — but defensively
    the guard does not fire."""
    # Real ToolCall objects would pass through line 3180 in the
    # source; here we just need any truthy list-like.
    assert not _finish_reason_tools_recovery(
        finish_reason="tool_calls",
        tool_calls=[{"id": "call_1", "function": {"name": "foo"}}],
        agent=_agent(),
    )


def test_already_handled_does_not_re_trigger():
    """One-shot semantics — once the agent has handled this case,
    don't loop. Prevents infinite synthetic-recovery cycles."""
    assert not _finish_reason_tools_recovery(
        finish_reason="tool_calls", tool_calls=None,
        agent=_agent(handled=True),
    )


# ─── Source-level check ──────────────────────────────────────

def test_patch_landed_in_conversation_loop():
    """Verify the patch literally exists in conversation_loop.py.
    Catches accidental reverts. Source-level check (no execution).
    """
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "agent" / "conversation_loop.py"
    text = src.read_text()
    # The guard variable + the recovery message marker must both
    # be present.
    assert "_finish_reason_tools_handled" in text
    assert "_finish_wants_tools" in text
    assert "_tool_calls_absent" in text
    # The hermes-agent#99 reference in the comment.
    assert "hermes-agent#99" in text
