"""Tests for the leading-underscore internal-marker stripping in the
API-message preparation path (hermes-agent#110).

Strict OpenAI-compat validators (Groq, Mistral, Fireworks) reject any
property they don't recognize with HTTP 400. Hermes-side bookkeeping
markers (``_thinking_prefill``, ``_empty_recovery_synthetic``,
``_empty_terminal_sentinel``) must be stripped before the message is
sent upstream.

Source-level + behavior tests. The behavior mirror evaluates the same
strip logic the conversation loop uses inline.
"""
from __future__ import annotations

import pathlib

import pytest


def _strip_internal_markers(api_msg: dict) -> dict:
    """Mirror of the strip logic at conversation_loop.py around
    line 849 (after C110 patch). Keep in sync with the source; the
    source-level test catches drift."""
    for _k in [k for k in api_msg if isinstance(k, str)
               and k.startswith("_")]:
        api_msg.pop(_k, None)
    return api_msg


# ─── Behavior ──────────────────────────────────────────────────

def test_strips_empty_recovery_synthetic():
    """The #108 / #69 recovery marker — must NOT round-trip to API."""
    msg = {
        "role": "user",
        "content": "Your previous response was empty...",
        "_empty_recovery_synthetic": True,
    }
    _strip_internal_markers(msg)
    assert "_empty_recovery_synthetic" not in msg
    assert msg["role"] == "user"
    assert "content" in msg


def test_strips_empty_terminal_sentinel():
    """The #69 sentinel marker for ``(empty)`` user-facing failures."""
    msg = {
        "role": "assistant",
        "content": "(empty)",
        "_empty_terminal_sentinel": True,
    }
    _strip_internal_markers(msg)
    assert "_empty_terminal_sentinel" not in msg


def test_strips_thinking_prefill():
    """Pre-existing marker that was already individually stripped
    before this fix — still gets stripped by the generic logic."""
    msg = {
        "role": "assistant",
        "content": "thinking...",
        "_thinking_prefill": True,
    }
    _strip_internal_markers(msg)
    assert "_thinking_prefill" not in msg


def test_strips_multiple_markers_in_one_pass():
    msg = {
        "role": "assistant",
        "content": "x",
        "_thinking_prefill": True,
        "_empty_recovery_synthetic": True,
        "_empty_terminal_sentinel": True,
        "_future_marker_added_later": "anything",
    }
    _strip_internal_markers(msg)
    # All four leading-underscore keys gone.
    for k in ("_thinking_prefill", "_empty_recovery_synthetic",
              "_empty_terminal_sentinel", "_future_marker_added_later"):
        assert k not in msg
    # Non-underscore keys preserved.
    assert msg["role"] == "assistant"
    assert msg["content"] == "x"


def test_preserves_role_content_tool_calls_name():
    """Standard OpenAI shape fields must not be touched."""
    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call_1", "function": {"name": "foo"}}],
        "name": "test-name",
        "tool_call_id": "call_1",
        "_internal": True,
    }
    _strip_internal_markers(msg)
    assert msg["role"] == "assistant"
    assert msg["content"] == ""
    assert msg["tool_calls"] == [{"id": "call_1",
                                  "function": {"name": "foo"}}]
    assert msg["name"] == "test-name"
    assert msg["tool_call_id"] == "call_1"
    assert "_internal" not in msg


def test_handles_message_with_no_internal_markers():
    msg = {
        "role": "user",
        "content": "Hello",
    }
    _strip_internal_markers(msg)
    assert msg == {"role": "user", "content": "Hello"}


def test_handles_empty_message():
    msg = {}
    _strip_internal_markers(msg)
    assert msg == {}


def test_non_string_keys_pass_through():
    """Defensive: numeric or other non-string keys (unlikely but
    possible if a caller pre-serialized something weird) should not
    crash the strip."""
    msg = {
        "role": "user",
        "content": "x",
        42: "weird-numeric-key",
        "_strip_me": True,
    }
    _strip_internal_markers(msg)
    assert 42 in msg  # numeric key survives
    assert "_strip_me" not in msg


# ─── Source-level check ────────────────────────────────────────

def test_patch_landed_in_conversation_loop():
    """The strip logic must be present in conversation_loop.py's
    API-message preparation. Catches accidental reverts."""
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "agent" / "conversation_loop.py")
    text = src.read_text()
    # The strip-all-underscore-prefixed logic must be inline.
    assert "k.startswith(\"_\")" in text or "k.startswith('_')" in text
    # The hermes-agent#110 reference in the comment.
    assert "hermes-agent#110" in text
    # The individual ``_thinking_prefill`` legacy pop should be REMOVED
    # (subsumed by the generic strip).
    # Note: ``_thinking_prefill`` still appears elsewhere as a key
    # being set on messages — we only check that the api_msg.pop line
    # is gone from the prep block.
    prep_block_start = text.find("# Remove 'reasoning' field")
    prep_block_end = text.find("if agent._should_sanitize_tool_calls():")
    assert prep_block_start >= 0 and prep_block_end > prep_block_start
    prep_block = text[prep_block_start:prep_block_end]
    assert 'api_msg.pop("_thinking_prefill", None)' not in prep_block
