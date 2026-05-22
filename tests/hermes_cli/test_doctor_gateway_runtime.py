"""Tests for the `Gateway Runtime` section in `hermes doctor` (#30).

Pattern mirrors test_doctor_devagentic_graph.py + test_doctor_cron_scheduler.py:
capture check_* calls, stub the gateway.status / hermes_cli.gateway
imports, assert the right kinds of output for each gateway state.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from hermes_cli import doctor as doctor_mod


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


def _install_gateway_stubs(monkeypatch, *,
                           status: dict | None,
                           pids: list | None = None,
                           read_raises: Exception | None = None):
    import sys

    def _read_runtime_status():
        if read_raises is not None:
            raise read_raises
        return status

    fake_status = SimpleNamespace(read_runtime_status=_read_runtime_status)
    fake_gateway = SimpleNamespace(
        find_gateway_pids=lambda: list(pids or []))
    monkeypatch.setitem(sys.modules, "gateway.status", fake_status)
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway", fake_gateway)


# ── Format helper ─────────────────────────────────────────────

def test_format_uptime_seconds():
    assert doctor_mod._format_uptime(45) == "45s"


def test_format_uptime_minutes():
    assert doctor_mod._format_uptime(125).startswith("2m")


def test_format_uptime_hours():
    out = doctor_mod._format_uptime(3 * 3600 + 30 * 60)
    assert out == "3h 30m"


# ── Probe behavior ────────────────────────────────────────────

def test_silent_when_no_status_file(monkeypatch):
    calls = _capture(monkeypatch)
    _install_gateway_stubs(monkeypatch, status=None)
    doctor_mod._check_gateway_runtime()
    assert calls == []


def test_running_state_with_pid_reports_ok(monkeypatch):
    calls = _capture(monkeypatch)
    _install_gateway_stubs(monkeypatch, pids=[12345],
                            status={
                                "gateway_state": "running",
                                "start_time": time.time() - 120,
                                "active_agents": 2,
                            })
    doctor_mod._check_gateway_runtime()
    oks = [c for c in calls if c[0] == "ok"]
    assert any("Gateway running" in c[1] for c in oks)
    assert any("PID 12345" in c[2] for c in oks)
    assert any("uptime" in c[2] for c in oks)
    assert any("2 active agent" in c[2] for c in oks)


def test_startup_failed_reports_fail_with_exit_reason(monkeypatch):
    calls = _capture(monkeypatch)
    _install_gateway_stubs(monkeypatch, pids=[],
                            status={
                                "gateway_state": "startup_failed",
                                "exit_reason": "missing TELEGRAM_BOT_TOKEN",
                                "updated_at": "2026-05-22T22:01:00",
                            })
    doctor_mod._check_gateway_runtime()
    fails = [c for c in calls if c[0] == "fail"]
    assert any("startup_failed" in c[1] for c in fails)
    assert any("TELEGRAM_BOT_TOKEN" in c[2] for c in fails)
    assert any("2026-05-22" in c[2] for c in fails)


def test_degraded_state_reports_warn(monkeypatch):
    calls = _capture(monkeypatch)
    _install_gateway_stubs(monkeypatch, pids=[5555],
                            status={
                                "gateway_state": "degraded",
                                "start_time": time.time() - 300,
                            })
    doctor_mod._check_gateway_runtime()
    warns = [c for c in calls if c[0] == "warn"]
    assert any("Gateway degraded" in c[1] for c in warns)
    assert any("uptime" in c[2] for c in warns)


def test_stopped_state_reports_info(monkeypatch):
    calls = _capture(monkeypatch)
    _install_gateway_stubs(monkeypatch, pids=[],
                            status={
                                "gateway_state": "stopped",
                                "exit_reason": "SIGTERM from systemd",
                                "updated_at": "2026-05-22T20:00:00",
                            })
    doctor_mod._check_gateway_runtime()
    infos = [c for c in calls if c[0] == "info"]
    assert any("Gateway stopped" in c[1] for c in infos)
    assert any("SIGTERM" in c[1] for c in infos)


def test_starting_state_shows_pending(monkeypatch):
    calls = _capture(monkeypatch)
    _install_gateway_stubs(monkeypatch, pids=[2],
                            status={"gateway_state": "starting"})
    doctor_mod._check_gateway_runtime()
    infos = [c for c in calls if c[0] == "info"]
    assert any("Gateway starting" in c[1] for c in infos)


def test_platform_fatal_reported_as_fail(monkeypatch):
    calls = _capture(monkeypatch)
    _install_gateway_stubs(monkeypatch, pids=[12],
                            status={
                                "gateway_state": "degraded",
                                "platforms": {
                                    "slack": {
                                        "platform_state": "fatal",
                                        "error_message": "token rejected",
                                    },
                                    "discord": {
                                        "platform_state": "connected",
                                    },
                                },
                            })
    doctor_mod._check_gateway_runtime()
    fails = [c for c in calls if c[0] == "fail"]
    assert any("slack" in c[1] and "fatal" in c[1] for c in fails)
    assert any("token rejected" in c[2] for c in fails)
    infos = [c for c in calls if c[0] == "info"]
    assert any("discord" in c[1] and "connected" in c[1].lower()
               for c in infos)


def test_platform_paused_reported_as_warn(monkeypatch):
    calls = _capture(monkeypatch)
    _install_gateway_stubs(monkeypatch, pids=[12],
                            status={
                                "gateway_state": "running",
                                "start_time": time.time() - 60,
                                "platforms": {
                                    "telegram": {
                                        "platform_state": "paused",
                                        "error_message": "circuit broken",
                                    },
                                },
                            })
    doctor_mod._check_gateway_runtime()
    warns = [c for c in calls if c[0] == "warn"]
    assert any("telegram" in c[1] and "paused" in c[1] for c in warns)


def test_pid_present_but_no_state_warns(monkeypatch):
    calls = _capture(monkeypatch)
    _install_gateway_stubs(monkeypatch, pids=[99],
                            status={"updated_at": "stale"})
    doctor_mod._check_gateway_runtime()
    warns = [c for c in calls if c[0] == "warn"]
    assert any("no recorded state" in c[1] for c in warns)


def test_read_raises_surfaces_warn(monkeypatch):
    calls = _capture(monkeypatch)
    _install_gateway_stubs(
        monkeypatch, status=None,
        read_raises=RuntimeError("status corrupt"))
    doctor_mod._check_gateway_runtime()
    warns = [c for c in calls if c[0] == "warn"]
    assert any("unreadable" in c[1].lower() for c in warns)
