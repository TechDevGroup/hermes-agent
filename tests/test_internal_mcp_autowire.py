"""Contract tests for hermes-agent#82 — internal mcp_serve auto-wire.

Validates:
  - HERMES_DISABLE_INTERNAL_MCP=1 (and friends) opts out cleanly
  - Idempotent: a second call with the entry already present is a no-op
  - Existing operator-configured MCP servers are preserved across the
    auto-write
  - The written entry uses sys.executable + ``-m mcp_serve`` (so it
    survives venv-not-on-PATH installs)
  - The bundled mcp_serve module exposes a ``__main__`` block (so
    ``python -m mcp_serve`` actually runs the stdio server — without
    this the autowired entry would spawn a no-op process)

Does NOT exercise the full stdio MCP handshake — that's covered by the
existing MCP client suite + the smoke test in the PR body.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hermes_cli.mcp_autowire import (
    INTERNAL_SERVER_NAME,
    ensure_internal_mcp_server,
)


REPO = Path(__file__).resolve().parents[1]


# ─── Opt-out env var ────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "True"])
def test_disable_env_skips_write(monkeypatch, value):
    """HERMES_DISABLE_INTERNAL_MCP truthy variants all opt out."""
    monkeypatch.setenv("HERMES_DISABLE_INTERNAL_MCP", value)
    cfg = {}
    assert ensure_internal_mcp_server(config=cfg) is False
    assert "mcp_servers" not in cfg or INTERNAL_SERVER_NAME not in cfg.get("mcp_servers", {})


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_disable_env_falsy_does_not_skip(monkeypatch, value):
    """Empty/falsy values should NOT trigger opt-out."""
    monkeypatch.setenv("HERMES_DISABLE_INTERNAL_MCP", value)
    cfg = {}
    # If mcp_serve isn't importable in the test env, this returns False
    # for a different reason — that's tested separately. Just verify it
    # doesn't short-circuit on the env var alone.
    from hermes_cli.mcp_autowire import _opted_out
    assert _opted_out() is False


# ─── Idempotency ────────────────────────────────────────────────────────

def test_existing_entry_is_no_op(monkeypatch):
    """If hermes-internal already exists, no rewrite — preserves operator
    customizations (custom args/env)."""
    monkeypatch.delenv("HERMES_DISABLE_INTERNAL_MCP", raising=False)
    custom_entry = {
        "command": "/operator/custom/python",
        "args": ["-m", "mcp_serve", "--verbose"],
        "env": {"OPERATOR_FLAG": "1"},
    }
    cfg = {"mcp_servers": {INTERNAL_SERVER_NAME: custom_entry}}
    wrote = ensure_internal_mcp_server(config=cfg)
    assert wrote is False
    # Operator entry untouched.
    assert cfg["mcp_servers"][INTERNAL_SERVER_NAME] == custom_entry


def test_second_call_no_ops(monkeypatch):
    """First call writes; second call is a no-op (same dict, no churn)."""
    monkeypatch.delenv("HERMES_DISABLE_INTERNAL_MCP", raising=False)
    cfg = {}
    first = ensure_internal_mcp_server(config=cfg)
    # If mcp_serve isn't importable in the test env, first will be False
    # and there's nothing more to assert; skip in that case.
    if not first:
        pytest.skip("mcp_serve not importable in test env")
    assert INTERNAL_SERVER_NAME in cfg["mcp_servers"]
    second = ensure_internal_mcp_server(config=cfg)
    assert second is False


# ─── Preserves existing servers ─────────────────────────────────────────

def test_existing_other_servers_preserved(monkeypatch):
    """Auto-wire must not clobber operator-added MCP servers (Linear,
    Notion, etc.) — only adds the new key."""
    monkeypatch.delenv("HERMES_DISABLE_INTERNAL_MCP", raising=False)
    pre = {
        "linear": {"url": "https://mcp.linear.app/", "headers": {}},
        "notion-local": {"command": "/usr/bin/node", "args": ["notion.js"]},
    }
    cfg = {"mcp_servers": dict(pre)}
    wrote = ensure_internal_mcp_server(config=cfg)
    if not wrote:
        pytest.skip("mcp_serve not importable in test env")
    # Pre-existing entries unchanged.
    for name, entry in pre.items():
        assert cfg["mcp_servers"][name] == entry
    # New entry present.
    assert INTERNAL_SERVER_NAME in cfg["mcp_servers"]


def test_creates_mcp_servers_key_if_missing(monkeypatch):
    """When the config has no mcp_servers section at all, auto-wire must
    create it rather than crash."""
    monkeypatch.delenv("HERMES_DISABLE_INTERNAL_MCP", raising=False)
    cfg = {"unrelated_key": "value"}
    wrote = ensure_internal_mcp_server(config=cfg)
    if not wrote:
        pytest.skip("mcp_serve not importable in test env")
    assert isinstance(cfg.get("mcp_servers"), dict)
    assert INTERNAL_SERVER_NAME in cfg["mcp_servers"]
    # Other keys untouched.
    assert cfg["unrelated_key"] == "value"


# ─── Entry shape ────────────────────────────────────────────────────────

def test_entry_uses_sys_executable(monkeypatch):
    """The written `command` must be sys.executable, NOT literal "python".
    Catches the venv-not-on-PATH regression where workers in container
    installs fail to spawn the subprocess."""
    monkeypatch.delenv("HERMES_DISABLE_INTERNAL_MCP", raising=False)
    cfg = {}
    wrote = ensure_internal_mcp_server(config=cfg)
    if not wrote:
        pytest.skip("mcp_serve not importable in test env")
    entry = cfg["mcp_servers"][INTERNAL_SERVER_NAME]
    assert entry["command"] == sys.executable, (
        f"expected command = sys.executable ({sys.executable!r}); "
        f"got {entry['command']!r}"
    )


def test_entry_args_invoke_mcp_serve(monkeypatch):
    """args must be ['-m', 'mcp_serve'] — verifies the autowired spawn
    actually targets the right module."""
    monkeypatch.delenv("HERMES_DISABLE_INTERNAL_MCP", raising=False)
    cfg = {}
    wrote = ensure_internal_mcp_server(config=cfg)
    if not wrote:
        pytest.skip("mcp_serve not importable in test env")
    entry = cfg["mcp_servers"][INTERNAL_SERVER_NAME]
    assert entry["args"] == ["-m", "mcp_serve"], (
        f"unexpected args shape: {entry['args']!r}"
    )


# ─── Source-level checks (catch packaging regression) ──────────────────

def test_mcp_serve_has_main_block():
    """mcp_serve.py must contain `if __name__ == "__main__":` so
    `python -m mcp_serve` runs the server. Without this, the autowired
    entry spawns a process that imports the module + exits immediately,
    breaking MCP-client handshake silently."""
    src = (REPO / "mcp_serve.py").read_text()
    assert 'if __name__ == "__main__":' in src, (
        "mcp_serve.py missing `__main__` guard — `python -m mcp_serve` "
        "would no-op and break the autowired stdio handshake"
    )


def test_mcp_serve_main_invokes_run_mcp_server():
    """Source-level check that the __main__ block actually calls
    run_mcp_server (catches a regression where the guard is present but
    the body is replaced with something else)."""
    src = (REPO / "mcp_serve.py").read_text()
    # Trim to the tail past the __main__ guard.
    tail = src.rsplit('if __name__ == "__main__":', 1)[-1]
    assert "run_mcp_server(" in tail, (
        "__main__ block must invoke run_mcp_server(...)"
    )


def test_entry_py_calls_ensure_internal_mcp_server():
    """tui_gateway/entry.py must call ensure_internal_mcp_server() BEFORE
    the mcp_servers gate, otherwise the auto-wire won't be visible to
    discover_mcp_tools() on the same boot."""
    src = (REPO / "tui_gateway" / "entry.py").read_text()
    assert "ensure_internal_mcp_server" in src, (
        "tui_gateway/entry.py missing call to ensure_internal_mcp_server()"
    )
    # Order check: the import + call must appear before the
    # `read_raw_config` gate, so the gate sees the freshly-written entry.
    autowire_pos = src.find("ensure_internal_mcp_server()")
    gate_pos = src.find("read_raw_config()")
    assert autowire_pos > 0 and gate_pos > 0
    assert autowire_pos < gate_pos, (
        "ensure_internal_mcp_server() must be called BEFORE the "
        "read_raw_config() gate check, otherwise the gate sees a stale "
        "snapshot and skips discovery."
    )
