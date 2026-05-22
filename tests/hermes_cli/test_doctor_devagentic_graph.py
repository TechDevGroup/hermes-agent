"""Tests for the `Devagentic Graph` section in `hermes doctor`
(see #17). The probe is silent when both graph_enabled() flags are
False, and surfaces the specific failure kind when either is True."""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO

import pytest

from hermes_cli import doctor as doctor_mod


def _capture_check_calls(monkeypatch):
    """Replace check_ok / check_warn / check_fail / check_info with
    capture stubs and return the recorder list. Each entry is
    ("ok"|"warn"|"fail"|"info", text, detail)."""
    calls: list[tuple[str, str, str]] = []

    def _ok(text, detail=""):
        calls.append(("ok", text, detail))

    def _warn(text, detail=""):
        calls.append(("warn", text, detail))

    def _fail(text, detail=""):
        calls.append(("fail", text, detail))

    def _info(text):
        calls.append(("info", text, ""))

    def _section(title):
        calls.append(("section", title, ""))

    monkeypatch.setattr(doctor_mod, "check_ok", _ok)
    monkeypatch.setattr(doctor_mod, "check_warn", _warn)
    monkeypatch.setattr(doctor_mod, "check_fail", _fail)
    monkeypatch.setattr(doctor_mod, "check_info", _info)
    monkeypatch.setattr(doctor_mod, "_section", _section)
    return calls


def _enable_graph_modes(monkeypatch, skills=True, memory=False):
    import agent.devagentic_skills as _skills
    import agent.devagentic_memory as _memory
    monkeypatch.setattr(_skills, "graph_enabled",
                        lambda: skills, raising=False)
    monkeypatch.setattr(_memory, "graph_enabled",
                        lambda: memory, raising=False)


def test_silent_when_both_graph_modes_disabled(monkeypatch):
    calls = _capture_check_calls(monkeypatch)
    _enable_graph_modes(monkeypatch, skills=False, memory=False)
    doctor_mod._check_devagentic_graph()
    assert calls == []


def test_reports_unresolved_user_id(monkeypatch):
    calls = _capture_check_calls(monkeypatch)
    _enable_graph_modes(monkeypatch, skills=True)
    monkeypatch.delenv("DEVAGENTIC_USER_ID", raising=False)
    import sys as _sys
    fake = type("F", (), {"get_active_profile_name": staticmethod(
        lambda: None)})()
    monkeypatch.setitem(_sys.modules, "hermes_cli.profiles", fake)

    doctor_mod._check_devagentic_graph()

    kinds = [c[0] for c in calls]
    texts = [c[1] for c in calls]
    assert "section" in kinds
    assert any("DEVAGENTIC_USER_ID unresolved" in t for t in texts)


def test_reports_auth_failure_on_401(monkeypatch):
    calls = _capture_check_calls(monkeypatch)
    _enable_graph_modes(monkeypatch, skills=True)
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")

    def _raise(*a, **k):
        raise urllib.error.HTTPError(
            "http://x/graphql", 401, "Unauthorized", {}, None)

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", _raise)

    doctor_mod._check_devagentic_graph()

    fail = [c for c in calls if c[0] == "fail"]
    assert fail, calls
    assert any("auth failed" in c[1] for c in fail)
    assert any("DEVAGENTIC_API_KEY" in c[2] for c in fail)


def test_reports_not_found_on_404(monkeypatch):
    calls = _capture_check_calls(monkeypatch)
    _enable_graph_modes(monkeypatch, memory=True, skills=False)
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")

    def _raise(*a, **k):
        raise urllib.error.HTTPError(
            "http://x/graphql", 404, "Not Found", {}, None)

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", _raise)

    doctor_mod._check_devagentic_graph()

    fail = [c for c in calls if c[0] == "fail"]
    assert any("not found" in c[1].lower() for c in fail), calls


def test_reports_unreachable_on_urlerror(monkeypatch):
    calls = _capture_check_calls(monkeypatch)
    _enable_graph_modes(monkeypatch, skills=True)
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")

    def _raise(*a, **k):
        raise urllib.error.URLError("connection refused")

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", _raise)

    doctor_mod._check_devagentic_graph()

    fail = [c for c in calls if c[0] == "fail"]
    assert any("unreachable" in c[1].lower() for c in fail), calls


def test_reports_ok_on_clean_200(monkeypatch):
    calls = _capture_check_calls(monkeypatch)
    _enable_graph_modes(monkeypatch, skills=True, memory=True)
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {"data": {"__typename": "Query"}}).encode("utf-8")

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", lambda *a, **k: _Resp())

    doctor_mod._check_devagentic_graph()

    ok = [c for c in calls if c[0] == "ok"]
    assert ok, calls
    assert any("reachable" in c[1].lower() for c in ok)
    info = [c for c in calls if c[0] == "info"]
    assert any("skills" in c[1] and "memory" in c[1] for c in info), \
        "Both modes should be listed in the info row"
