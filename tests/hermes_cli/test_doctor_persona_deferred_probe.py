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


@pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
def test_falsy_emits_info_with_opt_out_note(monkeypatch, raw):
    """T1 of #143: falsy explicit values now opt out of the
    provider-default defer. Surfaced as ``check_info`` (legacy
    behavior, not an error)."""
    calls = _capture(monkeypatch)
    monkeypatch.setenv("HERMES_DEFER_PERSONA", raw)
    doctor_mod._check_persona_deferred_env()
    infos = [c for c in calls if c[0] == "info"]
    warns = [c for c in calls if c[0] == "warn"]
    assert infos, calls
    assert not warns
    title = infos[0][1] if isinstance(infos[0][1], str) else infos[0][2]
    assert "opt-out" in title.lower() or "legacy" in title.lower()


@pytest.mark.parametrize("raw", ["maybe", "kthx", "kinda"])
def test_unknown_value_emits_warn_with_full_legend(monkeypatch, raw):
    """T1: only genuinely-unknown values (not in truthy OR falsy
    sets) trigger check_warn now. The detail must include both
    truthy and falsy legends."""
    calls = _capture(monkeypatch)
    monkeypatch.setenv("HERMES_DEFER_PERSONA", raw)
    doctor_mod._check_persona_deferred_env()
    warns = [c for c in calls if c[0] == "warn"]
    assert warns
    title, detail = warns[0][1], warns[0][2]
    assert raw in title
    # Title flags it as unrecognized
    assert "not recognized" in title.lower()
    # Both truthy and falsy legends appear
    for val in ("1", "true", "yes", "on"):
        assert val in detail
    for val in ("0", "false", "no", "off"):
        assert val in detail
