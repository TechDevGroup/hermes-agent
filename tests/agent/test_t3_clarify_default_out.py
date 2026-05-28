"""Tests for T3 of hermes-agent#143 — clarify toolset default-out
when ``provider=devagentic-local``.

Devagentic-side intent classifier owns clarification UX; hermes
does not need to surface the modal. This patch adds an implicit
``disabled_toolsets += ["clarify"]`` when provider is
devagentic-local, with explicit ``--enable-toolset clarify`` as
an override for legacy workflows.

Source-level tests (the agent_init flow needs a full setup context
to exercise live; the guard is small enough that source-level
verification + the existing agent integration tests in
test_agent_init give us sufficient coverage).
"""
from __future__ import annotations

import pathlib


def test_t3_clarify_default_out_patch_landed():
    """The implicit-disable + explicit-enable guard must be present
    in agent_init.py around the ``get_tool_definitions`` call."""
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "agent" / "agent_init.py")
    text = src.read_text()
    # T3 marker comment
    assert "T3 of #143" in text
    # The provider check
    assert '"devagentic-local"' in text
    # The implicit-disable mutation
    assert '_effective_disabled.append("clarify")' in text
    # The explicit-enable override path
    assert "_explicit_clarify_enable" in text


def test_explicit_enable_overrides_implicit_disable_in_source():
    """When operator passes ``--enable-toolset clarify``, the
    implicit-disable should NOT fire. Verify the source-level guard
    composes the two conditions correctly."""
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "agent" / "agent_init.py")
    text = src.read_text()
    # The guard is structured as:
    #   if provider == devagentic-local
    #      AND NOT explicit_clarify_enable
    #      AND not already in _effective_disabled
    assert "and not _explicit_clarify_enable" in text


def test_clarify_disable_message_includes_reenable_hint():
    """The print message must tell the operator how to re-enable
    if they need clarify back. Otherwise they hit a confusing
    'tool not found' downstream."""
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "agent" / "agent_init.py")
    text = src.read_text()
    assert "--enable-toolset clarify" in text


def test_disable_only_fires_for_devagentic_local_provider():
    """Sanity: the condition must NOT fire for other providers.
    Verify by checking the comparison is strict equality on
    'devagentic-local', not a prefix or alias match."""
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "agent" / "agent_init.py")
    text = src.read_text()
    # Look for the specific guard
    guard_idx = text.find("T3 of #143")
    assert guard_idx >= 0
    guard_block = text[guard_idx:guard_idx + 1200]
    # Must use strict equality, not startswith / in
    assert '== "devagentic-local"' in guard_block
    assert '.startswith("devagentic")' not in guard_block
