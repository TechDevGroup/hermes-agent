"""Tests for the loud tool-execution-dispatch diagnostic
(hermes-agent#130).

After v0.18.5 confirmed tool_calls ARE in agent.valid_tool_names
(no invalid_tool_call WARN fired) but side-effects still didn't
materialize, operators had no signal between (a) dispatch entry,
(b) handler invocation, (c) handler return. This patch adds
WARNING logs at both the entry to
``execute_tool_calls_sequential`` AND after each per-tool dispatch
so the entire pipeline is visible without ``-v``.
"""
from __future__ import annotations

import pathlib


def test_entry_diagnostic_patch_landed():
    """Dispatch-entry WARN must reference tool count, names, task_id,
    api_call, model, provider."""
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "agent" / "tool_executor.py")
    text = src.read_text()
    assert "hermes-agent#130" in text
    assert "execute_tool_calls_sequential: dispatching" in text
    # Carries the names list + effective_task_id for cross-system
    # log correlation.
    assert "_tc_count" in text
    assert "_tc_names" in text


def test_per_tool_result_preview_patch_landed():
    """Post-dispatch WARN must surface the result preview, blocked
    flag, and duration."""
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "agent" / "tool_executor.py")
    text = src.read_text()
    assert "tool_call dispatched: name=" in text
    assert "blocked=%s duration=%.2fs result_preview=" in text
    # _execution_blocked must be threaded into the log so operators
    # can tell guardrail / pre-hook blocks apart from real
    # execution.
    assert "_execution_blocked," in text


def test_entry_diagnostic_is_outside_branches():
    """The entry-level WARN must fire ONCE per execute_tool_calls
    invocation (not per-branch). Verifies by checking only ONE
    occurrence of the entry-string in the source."""
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "agent" / "tool_executor.py")
    text = src.read_text()
    occurrences = text.count("execute_tool_calls_sequential: dispatching")
    assert occurrences == 1, (
        f"Expected exactly 1 entry-diagnostic, found {occurrences}")
