"""Tests for ``ByteRoverMemoryProvider.health_check`` (#42 step 2c).

The override runs ``brv status`` to verify the CLI is both
installed AND logged in — ``is_available()`` only confirmed the
binary was on PATH, so an unauthenticated user previously saw a
false-positive green check from doctor. Reasons follow the RFC #42
prefix taxonomy (auth: / unreachable: / sdk_missing).
"""
from __future__ import annotations

from typing import Optional

import pytest

from plugins.memory.byterover import ByteRoverMemoryProvider
import plugins.memory.byterover as bv


# ── Helpers ────────────────────────────────────────────────────

def _stub_brv(monkeypatch, *, brv_path: Optional[str] = "/usr/bin/brv",
              run_result: Optional[dict] = None,
              run_raises: Optional[Exception] = None):
    """Patch the brv-resolve + _run_brv helpers."""
    monkeypatch.setattr(bv, "_resolve_brv_path", lambda: brv_path)

    def _run(args, timeout=10, cwd=None):
        if run_raises is not None:
            raise run_raises
        return run_result if run_result is not None else {"success": True, "output": ""}

    monkeypatch.setattr(bv, "_run_brv", _run)


# ── Tests ──────────────────────────────────────────────────────


def test_returns_true_when_brv_status_succeeds(monkeypatch):
    _stub_brv(monkeypatch, run_result={"success": True, "output": "logged in as alice"})
    healthy, reason = ByteRoverMemoryProvider().health_check()
    assert healthy is True
    assert reason == ""


def test_sdk_missing_when_brv_absent(monkeypatch):
    _stub_brv(monkeypatch, brv_path=None)
    healthy, reason = ByteRoverMemoryProvider().health_check()
    assert healthy is False
    assert reason == "sdk_missing"


@pytest.mark.parametrize("err", [
    "not signed in. run: brv auth login",
    "Unauthorized: token expired",
    "auth required",
    "401: missing token",
    "login required",
])
def test_auth_classified_correctly(monkeypatch, err):
    _stub_brv(monkeypatch, run_result={"success": False, "error": err})
    healthy, reason = ByteRoverMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("auth:"), reason
    assert err[:30] in reason


def test_timeout_classified_as_unreachable(monkeypatch):
    _stub_brv(monkeypatch, run_result={"success": False, "error": "command timed out"})
    healthy, reason = ByteRoverMemoryProvider().health_check()
    assert healthy is False
    assert reason == "unreachable: timeout"


def test_generic_failure_classified_as_unreachable(monkeypatch):
    _stub_brv(monkeypatch,
              run_result={"success": False, "error": "unknown subcommand 'status'"})
    healthy, reason = ByteRoverMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("unreachable:")
    assert "unknown subcommand" in reason


def test_never_raises_even_when_run_brv_throws(monkeypatch):
    """RFC #42 contract: health_check MUST NOT raise. Defense in
    depth — _run_brv already catches subprocess errors and returns
    a dict, but if for some reason it propagates, the override
    must still return a tuple."""
    _stub_brv(monkeypatch, run_raises=RuntimeError("brv exploded"))
    healthy, reason = ByteRoverMemoryProvider().health_check()
    assert healthy is False
    assert reason.startswith("unreachable:")
    assert "brv exploded" in reason


def test_truncates_long_error_messages(monkeypatch):
    long_err = "x" * 500
    _stub_brv(monkeypatch, run_result={"success": False, "error": long_err})
    healthy, reason = ByteRoverMemoryProvider().health_check()
    assert healthy is False
    # prefix ("unreachable: ") + 200 = 213
    assert len(reason) <= 220
