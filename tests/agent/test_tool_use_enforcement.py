"""Tests for ``HERMES_TOOL_USE_ENFORCEMENT`` injection in the
chat_completions transport (hermes-agent#115).

When set to ``required`` (the only recognized value), every
``chat.completions.create()`` call where tools are attached has
``tool_choice: "required"`` injected. Default behavior unchanged
when unset.
"""
from __future__ import annotations

import pytest

from agent.transports import chat_completions as cc


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("HERMES_TOOL_USE_ENFORCEMENT", raising=False)


# ─── Resolver ──────────────────────────────────────────────────

def test_resolve_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("HERMES_TOOL_USE_ENFORCEMENT", raising=False)
    assert cc._resolve_tool_use_enforcement() is None


def test_resolve_returns_none_when_empty(monkeypatch):
    monkeypatch.setenv("HERMES_TOOL_USE_ENFORCEMENT", "  ")
    assert cc._resolve_tool_use_enforcement() is None


@pytest.mark.parametrize("raw", ["required", "REQUIRED", "  Required  "])
def test_resolve_returns_required(monkeypatch, raw):
    monkeypatch.setenv("HERMES_TOOL_USE_ENFORCEMENT", raw)
    assert cc._resolve_tool_use_enforcement() == "required"


@pytest.mark.parametrize("raw", ["auto", "none", "any", "force", "kthx"])
def test_resolve_returns_none_for_unknown(monkeypatch, raw):
    monkeypatch.setenv("HERMES_TOOL_USE_ENFORCEMENT", raw)
    assert cc._resolve_tool_use_enforcement() is None


# ─── Injection behavior ────────────────────────────────────────

def test_inject_required_when_env_set_and_tools_present(monkeypatch):
    monkeypatch.setenv("HERMES_TOOL_USE_ENFORCEMENT", "required")
    api_kwargs = {"model": "x", "tools": [{"name": "foo"}]}
    cc._maybe_inject_required_tool_choice(api_kwargs, [{"name": "foo"}])
    assert api_kwargs["tool_choice"] == "required"


def test_no_inject_when_env_unset(monkeypatch):
    monkeypatch.delenv("HERMES_TOOL_USE_ENFORCEMENT", raising=False)
    api_kwargs = {"model": "x"}
    cc._maybe_inject_required_tool_choice(api_kwargs, [{"name": "foo"}])
    assert "tool_choice" not in api_kwargs


def test_no_inject_when_no_tools(monkeypatch):
    """Even with env=required, no tools attached → no injection.
    Sending ``tool_choice: required`` with empty tools is a 400 on
    most providers."""
    monkeypatch.setenv("HERMES_TOOL_USE_ENFORCEMENT", "required")
    api_kwargs = {"model": "x"}
    cc._maybe_inject_required_tool_choice(api_kwargs, None)
    assert "tool_choice" not in api_kwargs
    cc._maybe_inject_required_tool_choice(api_kwargs, [])
    assert "tool_choice" not in api_kwargs


def test_does_not_clobber_existing_tool_choice(monkeypatch):
    """A caller-supplied tool_choice is the dispatcher-tier signal;
    the env var is a session-tier default. Caller wins."""
    monkeypatch.setenv("HERMES_TOOL_USE_ENFORCEMENT", "required")
    api_kwargs = {"model": "x", "tool_choice": "auto"}
    cc._maybe_inject_required_tool_choice(api_kwargs, [{"name": "foo"}])
    assert api_kwargs["tool_choice"] == "auto"


def test_no_inject_when_env_unknown_value(monkeypatch):
    monkeypatch.setenv("HERMES_TOOL_USE_ENFORCEMENT", "auto")  # not "required"
    api_kwargs = {"model": "x"}
    cc._maybe_inject_required_tool_choice(api_kwargs, [{"name": "foo"}])
    assert "tool_choice" not in api_kwargs


# ─── Doctor probe ──────────────────────────────────────────────

def _capture(monkeypatch):
    import hermes_cli.doctor as doctor_mod
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


def test_doctor_silent_when_unset(monkeypatch):
    import hermes_cli.doctor as doctor_mod
    calls = _capture(monkeypatch)
    monkeypatch.delenv("HERMES_TOOL_USE_ENFORCEMENT", raising=False)
    doctor_mod._check_tool_use_enforcement_env()
    assert calls == []


def test_doctor_check_ok_on_required(monkeypatch):
    import hermes_cli.doctor as doctor_mod
    calls = _capture(monkeypatch)
    monkeypatch.setenv("HERMES_TOOL_USE_ENFORCEMENT", "required")
    doctor_mod._check_tool_use_enforcement_env()
    ok = [c for c in calls if c[0] == "ok"]
    assert ok
    assert "required" in ok[0][1]
    assert "tool_choice" in ok[0][2]


def test_doctor_check_warn_on_unknown(monkeypatch):
    import hermes_cli.doctor as doctor_mod
    calls = _capture(monkeypatch)
    monkeypatch.setenv("HERMES_TOOL_USE_ENFORCEMENT", "auto")
    doctor_mod._check_tool_use_enforcement_env()
    warns = [c for c in calls if c[0] == "warn"]
    assert warns
    assert "auto" in warns[0][1]
    assert "required" in warns[0][2]
