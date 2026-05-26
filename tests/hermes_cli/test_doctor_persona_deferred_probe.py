"""Tests for the ``HERMES_DEFER_PERSONA`` probe in ``hermes doctor``
(issue #105).

Silent when unset. check_ok for truthy values; check_warn for typos.
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
    monkeypatch.delenv("HERMES_DEFER_PERSONA", raising=False)
    doctor_mod._check_persona_deferred_env()
    assert calls == []


def test_silent_when_empty(monkeypatch):
    calls = _capture(monkeypatch)
    monkeypatch.setenv("HERMES_DEFER_PERSONA", "   ")
    doctor_mod._check_persona_deferred_env()
    assert calls == []


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", "Yes"])
def test_truthy_emits_ok_with_deferral_note(monkeypatch, raw):
    calls = _capture(monkeypatch)
    monkeypatch.setenv("HERMES_DEFER_PERSONA", raw)
    doctor_mod._check_persona_deferred_env()
    ok = [c for c in calls if c[0] == "ok"]
    assert ok
    title, detail = ok[0][1], ok[0][2]
    assert "HERMES_DEFER_PERSONA" in title
    assert "deferred" in detail.lower()


@pytest.mark.parametrize("raw", ["0", "false", "no", "maybe", "kthx"])
def test_non_truthy_emits_warn_with_valid_sample(monkeypatch, raw):
    calls = _capture(monkeypatch)
    monkeypatch.setenv("HERMES_DEFER_PERSONA", raw)
    doctor_mod._check_persona_deferred_env()
    warns = [c for c in calls if c[0] == "warn"]
    assert warns
    title, detail = warns[0][1], warns[0][2]
    assert raw in title
    assert "not a truthy value" in title
    for val in ("1", "true", "yes", "on"):
        assert val in detail
