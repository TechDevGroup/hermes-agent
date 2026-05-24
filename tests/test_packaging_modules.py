"""Packaging contract tests for top-level modules (hermes-agent#80).

Regression catcher for the bug where mcp_serve.py was at repo root but
not in pyproject.toml's [tool.setuptools] py-modules list, so the wheel
silently dropped it → MCP server couldn't be started from a pip-install.

Asserts:
1. py-modules list includes every non-private root .py that's a runtime
   module (NOT setup.py, NOT _private modules).
2. mcp_serve specifically is in py-modules (the original gap that
   surfaced this issue).
3. Each listed module name actually has a corresponding .py file
   (catches the inverse: dangling entries in py-modules pointing
   nowhere).

Validates the CONTRACT in source. End-to-end wheel inclusion verified
separately via `pip wheel . --no-deps` build during PR development
(documented in the PR body).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]


def _pyproject_py_modules() -> set[str]:
    src = (REPO / "pyproject.toml").read_text()
    m = re.search(r"py-modules\s*=\s*\[(.*?)\]", src, re.DOTALL)
    assert m, "pyproject.toml missing py-modules = [...]"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _root_py_files() -> set[str]:
    """Top-level .py files that should ship as runtime modules.
    Excludes:
      - setup.py (build-time, not runtime)
      - private modules (_*)
    """
    return {
        f[:-3] for f in os.listdir(REPO)
        if f.endswith(".py")
        and not f.startswith("_")
        and f != "setup.py"
    }


def test_mcp_serve_in_py_modules():
    """The actual fix — mcp_serve.py must be shipped. Without it,
    `from mcp_serve import run_mcp_server` raises ImportError at
    runtime + no MCP server can be started from the install."""
    assert "mcp_serve" in _pyproject_py_modules(), (
        "mcp_serve.py is at repo root but not in pyproject.toml "
        "py-modules — wheel silently drops it (hermes-agent#80)"
    )


def test_mini_swe_runner_in_py_modules():
    """mini_swe_runner.py is also a runtime module at root and was
    missing from py-modules. Catch the same shape of bug."""
    assert "mini_swe_runner" in _pyproject_py_modules(), (
        "mini_swe_runner.py at repo root but not in py-modules"
    )


def test_every_root_py_listed_in_py_modules():
    """Catches the next module someone adds at root without updating
    py-modules. setuptools' packages.find won't pick it up."""
    listed = _pyproject_py_modules()
    on_disk = _root_py_files()
    missing = on_disk - listed
    assert not missing, (
        f"top-level .py files missing from pyproject.toml py-modules: "
        f"{sorted(missing)}. Either add them or document why they're "
        f"excluded (e.g. test-only scripts)."
    )


def test_no_dangling_py_modules_entries():
    """py-modules entries should each correspond to an actual .py file
    at repo root. Catches typos / stale entries after a rename."""
    listed = _pyproject_py_modules()
    on_disk = _root_py_files()
    dangling = listed - on_disk
    assert not dangling, (
        f"py-modules lists names without matching .py files at root: "
        f"{sorted(dangling)}. Rename or remove."
    )


def test_mcp_serve_actually_exists_at_root():
    """Defensive: catches the case where mcp_serve is listed but the
    file was deleted/moved without updating py-modules."""
    assert (REPO / "mcp_serve.py").is_file(), (
        "mcp_serve.py listed in py-modules but file is missing"
    )
