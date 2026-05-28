"""Tests for T2 of hermes-agent#143 — env-gated removal of
empty-content recovery.

Default-flipped to OFF in one ship. The four hermes-side
recovery paths (post-tool nudge, finish_reason=tool_calls
synthetic, structural-empty synthetic, 3-retry loop) now
short-circuit unless ``HERMES_LEGACY_EMPTY_RECOVERY`` is set
truthy.

Devagentic-side cascade (#324) + runaway detector (#345-348) +
exec-terminus (#349-354) cover this layer with full intent / role
/ dispatch-trace context.

PRESERVED through T2 (verified by separate tests):
- PR #119 cascade_exhausted short-circuit (hermes deferring to
  devagentic sentinel; NOT recovery)
- PR #122/#125 raw tool_calls fallback in normalize_response
- PR #131/#136/#138/#141 diagnostics (env-gated via
  HERMES_DIAG_RAW_CAPTURE)
"""
from __future__ import annotations

import pathlib

import pytest


# ─── _resolve_legacy_empty_recovery ───────────────────────────

def test_resolver_returns_false_when_unset(monkeypatch):
    from agent.conversation_loop import _resolve_legacy_empty_recovery
    monkeypatch.delenv("HERMES_LEGACY_EMPTY_RECOVERY", raising=False)
    assert _resolve_legacy_empty_recovery() is False


def test_resolver_returns_false_when_empty(monkeypatch):
    from agent.conversation_loop import _resolve_legacy_empty_recovery
    monkeypatch.setenv("HERMES_LEGACY_EMPTY_RECOVERY", "   ")
    assert _resolve_legacy_empty_recovery() is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "Yes", "ON", "  yes  "])
def test_resolver_returns_true_for_truthy(monkeypatch, raw):
    from agent.conversation_loop import _resolve_legacy_empty_recovery
    monkeypatch.setenv("HERMES_LEGACY_EMPTY_RECOVERY", raw)
    assert _resolve_legacy_empty_recovery() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "maybe", "kthx"])
def test_resolver_returns_false_for_falsy_or_unknown(monkeypatch, raw):
    from agent.conversation_loop import _resolve_legacy_empty_recovery
    monkeypatch.setenv("HERMES_LEGACY_EMPTY_RECOVERY", raw)
    assert _resolve_legacy_empty_recovery() is False


# ─── Source-level gating ──────────────────────────────────────

def _src() -> str:
    return (pathlib.Path(__file__).resolve().parents[2]
            / "agent" / "conversation_loop.py").read_text()


def test_env_var_name_and_constants_present():
    text = _src()
    assert 'LEGACY_EMPTY_RECOVERY_ENV = "HERMES_LEGACY_EMPTY_RECOVERY"' in text
    assert "_LEGACY_RECOVERY_TRUTHY = frozenset" in text
    assert "def _resolve_legacy_empty_recovery" in text


def test_finish_wants_tools_branch_gated():
    """#108 finish_reason=tool_calls recovery gated by
    _legacy_recovery_on."""
    text = _src()
    # The branch condition must include the gate term.
    branch_idx = text.find(
        "if (_legacy_recovery_on\n                        and _finish_wants_tools")
    assert branch_idx >= 0, "finish_wants_tools branch missing legacy gate"


def test_post_tool_nudge_branch_gated():
    """Post-tool empty-response nudge gated by
    _legacy_recovery_on."""
    text = _src()
    branch_idx = text.find(
        "if (\n                        _legacy_recovery_on\n                        and _prior_was_tool")
    assert branch_idx >= 0, "post_tool nudge branch missing legacy gate"


def test_structural_empty_branch_gated():
    """#67/#69 structural-empty recovery gated by
    _legacy_recovery_on (incorporated into the condition
    composition itself)."""
    text = _src()
    # _structural_empty = (
    #     _legacy_recovery_on
    #     and _truly_empty
    #     ...
    # )
    block_start = text.find("_structural_empty = (")
    assert block_start >= 0
    block = text[block_start:block_start + 400]
    assert "_legacy_recovery_on" in block


def test_three_retry_loop_gated():
    """The silent 3-retry-on-empty loop gated by
    _legacy_recovery_on."""
    text = _src()
    # Single-line if; must lead with _legacy_recovery_on
    gate_idx = text.find(
        "if _legacy_recovery_on and _truly_empty and (not _has_structured")
    assert gate_idx >= 0, "three-retry loop missing legacy gate"


# ─── Preserved paths — NOT gated by legacy ────────────────────

def test_cascade_exhausted_short_circuit_preserved():
    """PR #119 cascade_exhausted short-circuit is NOT gated by
    _legacy_recovery_on. It's hermes correctly deferring to
    devagentic's sentinel — not recovery."""
    text = _src()
    block_start = text.find("if _cascade_err:")
    assert block_start >= 0
    # Find the next 200 chars around this block — verify no
    # _legacy_recovery_on prefix
    pre = text[max(0, block_start - 50):block_start]
    assert "_legacy_recovery_on" not in pre, (
        "cascade_exhausted must NOT be behind the legacy gate")


def test_normalize_response_recovery_preserved():
    """PR #122/#125 raw tool_calls fallback in normalize_response
    is in chat_completions.py, not in this gate. Sanity: not
    accidentally added here."""
    text = _src()
    # The phrase should NOT appear in conversation_loop (it lives
    # in transports/chat_completions.py).
    assert "_raw_tool_calls_from_response" not in text


# ─── Default behavior end-to-end ──────────────────────────────

def test_default_off_no_synthetic_recovery_appended(monkeypatch):
    """When env is unset (default), the four recovery branches
    must not modify ``messages`` with synthetic recovery pairs.

    We can't easily exercise the full conversation_loop without a
    real model client, but we can verify the resolver returns
    False — combined with the source-level gate tests, that
    proves the recovery paths are short-circuited."""
    from agent.conversation_loop import _resolve_legacy_empty_recovery
    monkeypatch.delenv("HERMES_LEGACY_EMPTY_RECOVERY", raising=False)
    assert _resolve_legacy_empty_recovery() is False


def test_opt_in_truthy_re_enables_legacy(monkeypatch):
    from agent.conversation_loop import _resolve_legacy_empty_recovery
    monkeypatch.setenv("HERMES_LEGACY_EMPTY_RECOVERY", "1")
    assert _resolve_legacy_empty_recovery() is True
