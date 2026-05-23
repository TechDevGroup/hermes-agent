"""Tests for ``HolographicMemoryProvider.health_check`` (#42 step 2h).

Override verifies db_path's parent directory is writable. Unlike
the cloud-backed providers, there's no remote service to ping —
but a read-only HERMES_HOME (RO mount, wrong perms) would still
break the provider at runtime, so the probe is meaningful.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from plugins.memory.holographic import HolographicMemoryProvider


def _provider(config: dict | None = None) -> HolographicMemoryProvider:
    return HolographicMemoryProvider(config=config or {})


def test_returns_true_when_parent_writable(tmp_path, monkeypatch):
    db_path = tmp_path / "memory_store.db"
    healthy, reason = _provider({"db_path": str(db_path)}).health_check()
    assert healthy is True
    assert reason == ""


def test_creates_missing_parent_directory(tmp_path, monkeypatch):
    """When the configured db_path's parent doesn't yet exist,
    health_check should mkdir it (parents=True) and report healthy."""
    db_path = tmp_path / "subdir" / "memory_store.db"
    assert not db_path.parent.exists()
    healthy, reason = _provider({"db_path": str(db_path)}).health_check()
    assert healthy is True
    assert db_path.parent.is_dir()


def test_returns_unreachable_when_parent_not_writable(
        tmp_path, monkeypatch):
    """Simulate a read-only HERMES_HOME by patching os.access to
    deny W_OK for the relevant path."""
    db_path = tmp_path / "memory_store.db"
    import os as _os
    real_access = _os.access
    target = db_path.parent

    def _fake_access(path, mode):
        # Deny write access on the configured parent specifically.
        if mode & _os.W_OK and Path(path).resolve() == target.resolve():
            return False
        return real_access(path, mode)

    monkeypatch.setattr(_os, "access", _fake_access)
    healthy, reason = _provider({"db_path": str(db_path)}).health_check()
    assert healthy is False
    assert reason.startswith("unreachable:")
    assert "not writable" in reason


def test_returns_unreachable_on_mkdir_failure(tmp_path, monkeypatch):
    """When mkdir raises OSError (e.g. parent directory is a file
    rather than a dir), surface as unreachable, not propagate."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir")
    db_path = blocker / "memory_store.db"  # parent is a file → OSError
    healthy, reason = _provider({"db_path": str(db_path)}).health_check()
    assert healthy is False
    assert reason.startswith("unreachable:")
    assert "unwritable" in reason


def test_config_error_when_hermes_constants_import_fails(
        monkeypatch, tmp_path):
    """RFC #42 contract: health_check MUST NOT raise. Even when
    hermes_constants becomes unimportable between construction
    and probe time, the method returns a tuple."""
    import sys
    # Construct with a real config so _load_plugin_config doesn't fire.
    prov = _provider({"db_path": str(tmp_path / "memory_store.db")})
    # Now break the hermes_constants import that health_check itself
    # performs.
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    healthy, reason = prov.health_check()
    assert healthy is False
    assert reason.startswith("config_error:")


def test_hermes_home_expansion_works(tmp_path, monkeypatch):
    """The legacy `${HERMES_HOME}` template in db_path values is
    substituted before the writability check."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # The provider reads HERMES_HOME via get_hermes_home() which
    # respects the env var — verify the template substitution path.
    healthy, reason = _provider(
        {"db_path": "${HERMES_HOME}/memory_store.db"}).health_check()
    assert healthy is True
    assert reason == ""
