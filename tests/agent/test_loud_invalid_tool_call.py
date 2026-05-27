"""Tests for the loud invalid_tool_call diagnostic (hermes-agent#127).

The verbose-only print at conversation_loop.py:3219 was the original
signal for "model emitted a tool name not in valid_tool_names" — but
operators rarely run with verbose enabled. Combined with model
hallucination ("the file has been created..." narration on the next
turn after the error result), the bug class became invisible.

This patch upgrades the diagnostic to WARNING log + ``_emit_status``
so operators see the mismatch + a sample of registered names without
running ``-v``.
"""
from __future__ import annotations

import pathlib


def test_loud_diagnostic_patch_landed():
    """The conversation_loop must contain the loud warning + status
    emit for invalid tool calls. Catches accidental reverts."""
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "agent" / "conversation_loop.py")
    text = src.read_text()
    # Warning log with the registered-tools sample
    assert "Invalid tool_call: model emitted" in text
    assert "_valid_sample" in text
    assert "_valid_count" in text
    # _emit_status surfacing the mismatch
    assert "Unknown tool {invalid_name!r}" in text
    assert "not in" in text
    assert "registered tools" in text
    # hermes-agent#127 reference
    assert "hermes-agent#127" in text


def test_emit_status_uses_invalid_name_and_count():
    """Source-level check that the emit_status template carries
    both the invalid name and the registered-tools count — so the
    operator can immediately tell whether (a) the model invented a
    name, or (b) the sandbox simply isn't loading the expected
    toolset."""
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "agent" / "conversation_loop.py")
    text = src.read_text()
    # The emit_status line includes the count + the name
    assert "_valid_count} registered tools" in text


def test_warning_includes_provider_and_model_for_correlation():
    """Operator needs model + provider in the WARN so they can
    correlate with devagentic-side dispatch logs."""
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "agent" / "conversation_loop.py")
    text = src.read_text()
    # The logger.warning includes both model and provider
    warn_block = text[text.find("Invalid tool_call: model emitted"):]
    warn_block = warn_block[:800]
    assert "agent.model" in warn_block
    assert "provider" in warn_block
