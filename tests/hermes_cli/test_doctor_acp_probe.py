"""Tests for the ACP (Agent-Client Protocol) probe in `hermes doctor`.

Silent when both `acp` and `acp_adapter.server.HermesACPAgent` are
importable — most operators don't use ACP and don't need a row each
run. Surfaces _fail_and_issue with a pip-install hint when ImportError;
check_warn for other import-time exceptions.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

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


def test_silent_when_acp_and_adapter_importable(monkeypatch):
    """No row when both imports succeed — most operators don't use
    ACP and don't need to see a status row each `hermes doctor`."""
    calls = _capture(monkeypatch)
    # The `acp` package is an optional dependency that may not be
    # installed in the test environment. Stub both modules so the
    # silent-on-success path is exercised regardless.
    monkeypatch.setitem(sys.modules, "acp", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules, "acp_adapter.server",
        SimpleNamespace(HermesACPAgent=type("_HermesACPAgent", (), {})))
    issues: list[str] = []
    doctor_mod._check_acp_installation(issues)
    assert calls == []
    assert issues == []


def test_import_error_emits_fail_and_issue(monkeypatch):
    calls = _capture(monkeypatch)
    # Force `import acp` to fail.
    monkeypatch.setitem(sys.modules, "acp", None)
    issues: list[str] = []
    doctor_mod._check_acp_installation(issues)
    section = [c for c in calls if c[0] == "section"]
    fail = [c for c in calls if c[0] == "fail"]
    assert section and section[0][1] == "ACP (IDE integration)"
    assert fail and "agent-client-protocol" in fail[0][1]
    assert "pip install agent-client-protocol" in fail[0][2]
    assert issues, "expected an issue appended"
    assert "agent-client-protocol" in issues[0]


def test_adapter_import_error_also_emits_fail(monkeypatch):
    """When `acp` itself is importable but `acp_adapter.server` is
    broken, the probe still surfaces a fail row (the runtime path
    is broken either way)."""
    calls = _capture(monkeypatch)
    # acp imports fine (it's a real module), but stub
    # acp_adapter.server to break.
    monkeypatch.setitem(sys.modules, "acp_adapter.server", None)
    issues: list[str] = []
    doctor_mod._check_acp_installation(issues)
    assert any(c[0] == "fail" for c in calls)


def test_non_import_exception_emits_warn(monkeypatch):
    """Defense in depth: if some downstream import raises e.g.
    a SyntaxError or a runtime ValueError during module init, surface
    as warn (less clear-cut fix than ImportError)."""
    calls = _capture(monkeypatch)

    class _ExplodingModule:
        def __getattr__(self, _name):
            raise ValueError("module init failed")

    # The `from acp_adapter.server import HermesACPAgent` import
    # triggers attribute lookup; making the module raise ValueError
    # on attribute access reproduces a non-ImportError failure mode.
    monkeypatch.setitem(sys.modules, "acp", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "acp_adapter.server",
                        _ExplodingModule())
    issues: list[str] = []
    doctor_mod._check_acp_installation(issues)
    warns = [c for c in calls if c[0] == "warn"]
    fails = [c for c in calls if c[0] == "fail"]
    assert warns, calls
    assert not fails, calls
    assert "ValueError" in warns[0][2]
    # No issue appended — warns don't pollute the actionable-fix list.
    assert issues == []
