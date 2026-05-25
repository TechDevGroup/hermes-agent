"""Tests for the provider-env-var probe in ``hermes doctor``.

Surfaces typos in ``HERMES_DEFAULT_PROVIDER`` /
``HERMES_INFERENCE_PROVIDER`` at boot instead of letting the worker
silently fall through to ``auto``. Silent when neither is set
(silent-when-irrelevant pattern from #88/#53/#54).
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


def test_silent_when_neither_env_set(monkeypatch):
    """No row when both env vars are unset — most operators don't
    pin the provider via env."""
    calls = _capture(monkeypatch)
    monkeypatch.delenv("HERMES_DEFAULT_PROVIDER", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    doctor_mod._check_provider_env_vars()
    assert calls == []


def test_silent_when_both_env_empty(monkeypatch):
    """Empty-string values behave the same as unset (stripped to ''
    by the probe's normalization)."""
    calls = _capture(monkeypatch)
    monkeypatch.setenv("HERMES_DEFAULT_PROVIDER", "")
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "   ")
    doctor_mod._check_provider_env_vars()
    assert calls == []


def test_known_provider_emits_check_ok(monkeypatch):
    """A valid provider name (one in PROVIDER_REGISTRY or plugin-
    registered) surfaces as check_ok."""
    calls = _capture(monkeypatch)
    monkeypatch.setenv("HERMES_DEFAULT_PROVIDER", "openrouter")
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    doctor_mod._check_provider_env_vars()
    section = [c for c in calls if c[0] == "section"]
    ok = [c for c in calls if c[0] == "ok"]
    warn = [c for c in calls if c[0] == "warn"]
    assert section and section[0][1] == "Provider env vars"
    assert ok and "HERMES_DEFAULT_PROVIDER" in ok[0][1]
    assert "openrouter" in ok[0][1]
    assert not warn


def test_typo_emits_check_warn_with_sample(monkeypatch):
    """A typo'd provider name surfaces as check_warn with a sample
    of valid names so the operator can spot the fix."""
    calls = _capture(monkeypatch)
    monkeypatch.setenv("HERMES_DEFAULT_PROVIDER", "devagentic-locol")
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    doctor_mod._check_provider_env_vars()
    warns = [c for c in calls if c[0] == "warn"]
    fails = [c for c in calls if c[0] == "fail"]
    assert warns, calls
    assert not fails  # warn, not fail — typos are diagnosed, not blocked
    title, detail = warns[0][1], warns[0][2]
    assert "devagentic-locol" in title
    assert "doesn't match" in title
    # Sample of known names visible in the detail line.
    assert "known providers include" in detail


def test_both_env_vars_checked_independently(monkeypatch):
    """When both env vars are set, each gets its own row — a partial
    mismatch (one valid, one typo) is surfaced precisely."""
    calls = _capture(monkeypatch)
    monkeypatch.setenv("HERMES_DEFAULT_PROVIDER", "openrouter")
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "openroutr")  # typo
    doctor_mod._check_provider_env_vars()
    ok_titles = [c[1] for c in calls if c[0] == "ok"]
    warn_titles = [c[1] for c in calls if c[0] == "warn"]
    assert any("HERMES_DEFAULT_PROVIDER" in t for t in ok_titles)
    assert any("HERMES_INFERENCE_PROVIDER" in t for t in warn_titles)


def test_case_insensitive_match(monkeypatch):
    """Env var values are lowercased before comparison so the
    operator's casing convention doesn't matter."""
    calls = _capture(monkeypatch)
    monkeypatch.setenv("HERMES_DEFAULT_PROVIDER", "OpenRouter")
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    doctor_mod._check_provider_env_vars()
    assert any(c[0] == "ok" for c in calls)


def test_plugin_registered_provider_is_known(monkeypatch):
    """Plugin-registered providers (like devagentic-local) should
    surface in the known set after providers.list_providers() runs.
    Mock the import to make the test deterministic regardless of
    plugin-load state."""
    calls = _capture(monkeypatch)

    class _FakeProfile:
        def __init__(self, name):
            self.name = name

    fake_list = lambda: [_FakeProfile("devagentic-local"),
                         _FakeProfile("nous")]
    import providers as _providers
    monkeypatch.setattr(_providers, "list_providers", fake_list)

    monkeypatch.setenv("HERMES_DEFAULT_PROVIDER", "devagentic-local")
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    doctor_mod._check_provider_env_vars()
    assert any(c[0] == "ok" for c in calls), calls


def test_provider_list_import_failure_does_not_crash(monkeypatch):
    """Defense in depth: if ``providers.list_providers`` blows up
    (e.g., a plugin module raises at import time), the probe should
    still complete using just the built-in PROVIDER_REGISTRY +
    standard aliases. No crash."""
    calls = _capture(monkeypatch)

    import providers as _providers
    def _broken():
        raise RuntimeError("plugin import failed")
    monkeypatch.setattr(_providers, "list_providers", _broken)

    monkeypatch.setenv("HERMES_DEFAULT_PROVIDER", "openrouter")
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    doctor_mod._check_provider_env_vars()
    # openrouter is in the standard alias set, so still check_ok.
    assert any(c[0] == "ok" for c in calls), calls
