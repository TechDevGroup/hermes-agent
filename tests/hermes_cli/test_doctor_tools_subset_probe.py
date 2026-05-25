"""Tests for the ``HERMES_TOOLS_SUBSET`` probe in ``hermes doctor``.

Surfaces the active tool-subset narrowing config at boot so the
operator can confirm the filter parsed as expected. Silent when
unset; check_ok with names + count when set; check_info reminder
when no entry looks MCP-prefixed but some entries look structured
(common failure mode: operator forgot the prefix).
"""
from __future__ import annotations

import pytest

import hermes_cli.doctor as doctor_mod


def _capture(monkeypatch):
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(doctor_mod, "check_ok",
                        lambda t, d="": calls.append(("ok", t, d)))
    monkeypatch.setattr(doctor_mod, "check_warn",
                        lambda t, d="": calls.append(("warn", t, d)))
    monkeypatch.setattr(doctor_mod, "check_fail",
                        lambda t, d="": calls.append(("fail", t, d)))
    monkeypatch.setattr(doctor_mod, "check_info",
                        lambda t: calls.append(("info", t, "")))
    monkeypatch.setattr(doctor_mod, "_section",
                        lambda t: calls.append(("section", t, "")))
    return calls


def test_silent_when_unset(monkeypatch):
    calls = _capture(monkeypatch)
    monkeypatch.delenv("HERMES_TOOLS_SUBSET", raising=False)
    doctor_mod._check_tools_subset_env()
    assert calls == []


def test_silent_when_empty(monkeypatch):
    calls = _capture(monkeypatch)
    monkeypatch.setenv("HERMES_TOOLS_SUBSET", "")
    doctor_mod._check_tools_subset_env()
    assert calls == []


def test_silent_when_whitespace_only(monkeypatch):
    calls = _capture(monkeypatch)
    monkeypatch.setenv("HERMES_TOOLS_SUBSET", "  ,  ,   ")
    doctor_mod._check_tools_subset_env()
    assert calls == []


def test_count_and_sample_surfaced(monkeypatch):
    """Operator sets a 4-tool subset — verify count + all names
    show in the OK row (since 4 < the 6-item sample cap)."""
    calls = _capture(monkeypatch)
    monkeypatch.setenv(
        "HERMES_TOOLS_SUBSET",
        "mcp_hermes-internal_silo_query, "
        "mcp_hermes-internal_confer_run, "
        "messages_send, conversations_list",
    )
    doctor_mod._check_tools_subset_env()
    section = [c for c in calls if c[0] == "section"]
    ok = [c for c in calls if c[0] == "ok"]
    assert section and section[0][1] == "HERMES_TOOLS_SUBSET"
    assert ok
    title, detail = ok[0][1], ok[0][2]
    assert "4 tools allowed" in title
    # All names appear in the detail sample.
    assert "silo_query" in detail
    assert "messages_send" in detail


def test_long_list_truncated_with_more_suffix(monkeypatch):
    """A subset with > 6 tools shows the first 6 + `+N more` so the
    row stays readable."""
    calls = _capture(monkeypatch)
    names = ["mcp_x_tool_" + str(i) for i in range(10)]
    monkeypatch.setenv("HERMES_TOOLS_SUBSET", ",".join(names))
    doctor_mod._check_tools_subset_env()
    ok = [c for c in calls if c[0] == "ok"]
    assert ok
    detail = ok[0][2]
    assert "10 tools allowed" in ok[0][1]
    assert "+4 more" in detail


def test_no_mcp_prefix_reminder(monkeypatch):
    """When NO entry uses the mcp_ prefix but at least one entry
    has an underscore (suggesting the operator probably MEANT MCP
    tools), the probe surfaces an info reminder."""
    calls = _capture(monkeypatch)
    monkeypatch.setenv(
        "HERMES_TOOLS_SUBSET", "silo_query, confer_run, file_issue")
    doctor_mod._check_tools_subset_env()
    infos = [c for c in calls if c[0] == "info"]
    assert infos, calls
    assert "prefix" in infos[0][1].lower()


def test_no_reminder_when_mcp_prefix_present(monkeypatch):
    """At least one MCP-prefixed entry → operator knows the
    convention → no info row added."""
    calls = _capture(monkeypatch)
    monkeypatch.setenv(
        "HERMES_TOOLS_SUBSET",
        "mcp_hermes-internal_silo_query, conversations_list",
    )
    doctor_mod._check_tools_subset_env()
    infos = [c for c in calls if c[0] == "info"]
    assert not infos


def test_no_reminder_when_only_bare_simple_names(monkeypatch):
    """Bare simple names (no underscore) with no mcp_ prefix don't
    trigger the reminder — those are common built-in tools."""
    calls = _capture(monkeypatch)
    monkeypatch.setenv(
        "HERMES_TOOLS_SUBSET", "messages, conversations, channels")
    doctor_mod._check_tools_subset_env()
    infos = [c for c in calls if c[0] == "info"]
    assert not infos
