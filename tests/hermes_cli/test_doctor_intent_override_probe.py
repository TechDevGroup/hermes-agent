"""Tests for the ``HERMES_INTENT_OVERRIDE`` probe in ``hermes doctor``
(issue #97 / #89 Direction A).

Silent when unset. check_ok for valid intent keys; check_warn for
typos. Mirrors the silent-when-irrelevant pattern from PR #95 / #96.
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
    monkeypatch.delenv("HERMES_INTENT_OVERRIDE", raising=False)
    doctor_mod._check_intent_override_env()
    assert calls == []


def test_silent_when_empty(monkeypatch):
    calls = _capture(monkeypatch)
    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", "   ")
    doctor_mod._check_intent_override_env()
    assert calls == []


def test_code_intent_emits_ok_with_narrowing_note(monkeypatch):
    calls = _capture(monkeypatch)
    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", "code")
    doctor_mod._check_intent_override_env()
    ok = [c for c in calls if c[0] == "ok"]
    assert ok
    title, detail = ok[0][1], ok[0][2]
    assert "code" in title
    assert "narrows" in detail.lower()


@pytest.mark.parametrize(
    "intent", ["confer", "planning", "exploration", "refinement", "generic"])
def test_non_code_valid_intents_emit_ok_pass_through(monkeypatch, intent):
    calls = _capture(monkeypatch)
    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", intent)
    doctor_mod._check_intent_override_env()
    ok = [c for c in calls if c[0] == "ok"]
    assert ok
    detail = ok[0][2]
    assert "no narrowing" in detail.lower()


def test_typo_emits_warn_with_valid_sample(monkeypatch):
    calls = _capture(monkeypatch)
    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", "kode")
    doctor_mod._check_intent_override_env()
    warns = [c for c in calls if c[0] == "warn"]
    fails = [c for c in calls if c[0] == "fail"]
    assert warns and not fails
    title, detail = warns[0][1], warns[0][2]
    assert "kode" in title
    assert "not a known" in title
    # All 6 valid intents should appear in the detail (sorted).
    for key in ("code", "confer", "planning",
                "exploration", "refinement", "generic"):
        assert key in detail


def test_case_insensitive_valid(monkeypatch):
    """The runtime resolver lowercases — the probe should too."""
    calls = _capture(monkeypatch)
    monkeypatch.setenv("HERMES_INTENT_OVERRIDE", "Code")
    doctor_mod._check_intent_override_env()
    assert any(c[0] == "ok" for c in calls)
