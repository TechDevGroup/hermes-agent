"""Tests for the `Cron Scheduler` section in `hermes doctor` (#26).

Pattern mirrors `tests/hermes_cli/test_doctor_devagentic_graph.py`:
capture check_* calls, drive `_check_cron_scheduler` with stubbed
`cron.jobs` + `hermes_cli.gateway` imports, assert the right kinds
of output for each failure mode.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import doctor as doctor_mod


def _capture_check_calls(monkeypatch):
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


def _install_cron_stubs(monkeypatch, *, jobs_file_exists: bool,
                       jobs: list | None = None,
                       pids: list | None = None,
                       load_raises: Exception | None = None):
    import sys

    class _JobsFile:
        @staticmethod
        def exists():
            return jobs_file_exists

    def _load_jobs():
        if load_raises is not None:
            raise load_raises
        return jobs or []

    fake_cron_jobs = SimpleNamespace(
        load_jobs=_load_jobs, JOBS_FILE=_JobsFile())
    fake_gateway = SimpleNamespace(
        find_gateway_pids=lambda: list(pids or []))
    monkeypatch.setitem(sys.modules, "cron.jobs", fake_cron_jobs)
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway", fake_gateway)


def test_silent_when_jobs_file_absent(monkeypatch):
    calls = _capture_check_calls(monkeypatch)
    _install_cron_stubs(monkeypatch, jobs_file_exists=False)
    doctor_mod._check_cron_scheduler()
    assert calls == []


def test_silent_when_jobs_file_empty(monkeypatch):
    calls = _capture_check_calls(monkeypatch)
    _install_cron_stubs(monkeypatch, jobs_file_exists=True, jobs=[])
    doctor_mod._check_cron_scheduler()
    assert calls == []


def test_corrupted_jobs_file_surfaces_fail(monkeypatch):
    calls = _capture_check_calls(monkeypatch)
    _install_cron_stubs(
        monkeypatch, jobs_file_exists=True,
        load_raises=RuntimeError("jobs.json unreadable"))
    doctor_mod._check_cron_scheduler()
    kinds = [c[0] for c in calls]
    assert "section" in kinds
    assert "fail" in kinds
    fail = [c for c in calls if c[0] == "fail"][0]
    assert "Could not read jobs.json" in fail[1]
    assert "unreadable" in fail[2]


def test_gateway_running_with_active_jobs_reports_ok(monkeypatch):
    calls = _capture_check_calls(monkeypatch)
    _install_cron_stubs(
        monkeypatch, jobs_file_exists=True, pids=[12345],
        jobs=[{"id": "a", "next_run_at": "2026-05-22T23:55:00",
               "last_status": "ok"}])
    doctor_mod._check_cron_scheduler()
    oks = [c for c in calls if c[0] == "ok"]
    assert any("Gateway running" in c[1] for c in oks)
    assert any("12345" in c[2] for c in oks)
    assert any("1 active job" in c[1] for c in oks)
    assert any("2026-05-22T23:55:00" in c[2] for c in oks)


def test_gateway_not_running_reports_fail(monkeypatch):
    calls = _capture_check_calls(monkeypatch)
    _install_cron_stubs(
        monkeypatch, jobs_file_exists=True, pids=[],
        jobs=[{"id": "a", "next_run_at": "2026-05-22T23:55:00"}])
    doctor_mod._check_cron_scheduler()
    fails = [c for c in calls if c[0] == "fail"]
    assert any("Gateway not running" in c[1] for c in fails)
    assert any("hermes gateway" in c[2] for c in fails)


def test_failed_jobs_surface_summary_and_examples(monkeypatch):
    calls = _capture_check_calls(monkeypatch)
    _install_cron_stubs(
        monkeypatch, jobs_file_exists=True, pids=[1],
        jobs=[
            {"id": "good-1", "last_status": "ok"},
            {"id": "good-2", "last_status": "ok"},
            {"id": "bad-1", "name": "audit-prs",
             "last_status": "error",
             "last_run_at": "2026-05-22T22:01:00",
             "last_error": "authentication failed — set API key"},
            {"id": "bad-2", "name": "nightly-sync",
             "last_status": "timeout",
             "last_run_at": "2026-05-22T22:33:00",
             "last_error": "process exceeded 600s"},
        ])
    doctor_mod._check_cron_scheduler()
    warns = [c for c in calls if c[0] == "warn"]
    assert any("2 of 4 job(s)" in c[1] and "failing" in c[1]
               for c in warns), warns
    infos = [c for c in calls if c[0] == "info"]
    info_text = " ".join(c[1] for c in infos)
    assert "audit-prs" in info_text
    assert "authentication failed" in info_text
    assert "nightly-sync" in info_text


def test_disabled_and_paused_jobs_excluded_from_active_count(
        monkeypatch):
    calls = _capture_check_calls(monkeypatch)
    _install_cron_stubs(
        monkeypatch, jobs_file_exists=True, pids=[1],
        jobs=[
            {"id": "a", "disabled": True},
            {"id": "b", "paused": True},
            {"id": "c", "next_run_at": "2026-05-22T23:00:00"},
        ])
    doctor_mod._check_cron_scheduler()
    oks = [c for c in calls if c[0] == "ok"]
    assert any("1 active job" in c[1] for c in oks)


def test_all_jobs_disabled_reports_info_not_ok(monkeypatch):
    calls = _capture_check_calls(monkeypatch)
    _install_cron_stubs(
        monkeypatch, jobs_file_exists=True, pids=[1],
        jobs=[
            {"id": "a", "disabled": True},
            {"id": "b", "paused": True},
        ])
    doctor_mod._check_cron_scheduler()
    infos = [c for c in calls if c[0] == "info"]
    assert any("0 active" in c[1] for c in infos)
