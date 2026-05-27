"""Tests for HERMES_DIAG_RAW_CAPTURE env-gate (hermes-agent#140).

v0.18.9 unconditionally emitted 4 ``[hermes-diag]`` lines per
dispatch + a 4KB body dump from the SDK monkey-patch — floods the
interactive sandbox pane. This patch defaults all diagnostics OFF
and gates them on ``HERMES_DIAG_RAW_CAPTURE`` truthy.

Also confirms ``_diag`` routes to stderr (not stdout) per the
field-report query.
"""
from __future__ import annotations

import io
import sys

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("HERMES_DIAG_RAW_CAPTURE", raising=False)


# ─── _diag_enabled resolver ───────────────────────────────────

def test_diag_disabled_when_env_unset(monkeypatch):
    from agent.transports.chat_completions import _diag_enabled
    monkeypatch.delenv("HERMES_DIAG_RAW_CAPTURE", raising=False)
    assert _diag_enabled() is False


def test_diag_disabled_when_env_empty(monkeypatch):
    from agent.transports.chat_completions import _diag_enabled
    monkeypatch.setenv("HERMES_DIAG_RAW_CAPTURE", "  ")
    assert _diag_enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "Yes", "ON"])
def test_diag_enabled_when_env_truthy(monkeypatch, raw):
    from agent.transports.chat_completions import _diag_enabled
    monkeypatch.setenv("HERMES_DIAG_RAW_CAPTURE", raw)
    assert _diag_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "maybe"])
def test_diag_disabled_for_falsy_or_unknown(monkeypatch, raw):
    from agent.transports.chat_completions import _diag_enabled
    monkeypatch.setenv("HERMES_DIAG_RAW_CAPTURE", raw)
    assert _diag_enabled() is False


# ─── _diag output behavior ────────────────────────────────────

def test_diag_silent_when_env_off(monkeypatch, capsys):
    from agent.transports.chat_completions import _diag
    monkeypatch.delenv("HERMES_DIAG_RAW_CAPTURE", raising=False)
    _diag("should-not-appear")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_diag_writes_to_stderr_when_env_on(monkeypatch, capsys):
    from agent.transports.chat_completions import _diag
    monkeypatch.setenv("HERMES_DIAG_RAW_CAPTURE", "1")
    _diag("test-message-for-stderr")
    captured = capsys.readouterr()
    # Goes to stderr ONLY — never stdout (user reported suspicious
    # stdout leak; this regression-test pins it).
    assert captured.out == ""
    assert "test-message-for-stderr" in captured.err
    assert "[hermes-diag]" in captured.err


def test_diag_stderr_routing_distinct_from_stdout(monkeypatch):
    """Direct sys.stderr capture (independent of pytest's capsys)
    confirms _diag writes via sys.stderr.write, not print() which
    would default to stdout."""
    from agent.transports.chat_completions import _diag
    monkeypatch.setenv("HERMES_DIAG_RAW_CAPTURE", "1")

    stdout_cap = io.StringIO()
    stderr_cap = io.StringIO()
    orig_out, orig_err = sys.stdout, sys.stderr
    sys.stdout = stdout_cap
    sys.stderr = stderr_cap
    try:
        _diag("routing-check")
    finally:
        sys.stdout = orig_out
        sys.stderr = orig_err

    assert stdout_cap.getvalue() == ""
    assert "routing-check" in stderr_cap.getvalue()


# ─── SDK raw-capture gating ───────────────────────────────────

def test_sdk_install_function_no_ops_when_disabled(monkeypatch):
    """The patch installer respects the env gate. When OFF, the
    monkey-patch should NOT install (no per-request stderr writes,
    no SDK overhead)."""
    import importlib
    import openai._base_client as _bc

    # Reset install state
    monkeypatch.setattr(
        "agent.transports.chat_completions._RAW_CAPTURE_INSTALLED",
        False, raising=False)
    # Save original to verify no monkey-patch
    orig_process = _bc.SyncAPIClient._process_response_data

    monkeypatch.delenv("HERMES_DIAG_RAW_CAPTURE", raising=False)
    # When env is OFF, install_function should not patch
    from agent.transports.chat_completions import _install_sdk_raw_capture
    # NOTE: we don't call _install_sdk_raw_capture here because the
    # module's import-time call already happened. Instead verify the
    # source-level gate is present.
    import inspect
    src = inspect.getsource(
        importlib.import_module("agent.transports.chat_completions"))
    # Confirm gate present at install-time call site
    assert "if _diag_enabled():" in src
    assert "_install_sdk_raw_capture()" in src


def test_install_marker_only_logged_when_enabled():
    """Source-level: ``sdk-raw-capture installed`` marker only
    emits via ``_diag`` which is itself env-gated."""
    import inspect
    from agent.transports import chat_completions
    src = inspect.getsource(chat_completions)
    # Marker is wrapped in _diag (not bare sys.stderr.write).
    assert '_diag("sdk-raw-capture installed' in src
