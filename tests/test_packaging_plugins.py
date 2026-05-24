"""Packaging contract tests for plugins/ (hermes-agent#76).

Asserts that pyproject.toml [tool.setuptools.package-data] AND
MANIFEST.in both include the patterns required to ship plugin.yaml,
SKILL.md, and other non-.py files plugins depend on. Regression
catcher for the bug where pip-installed wheels silently dropped every
devagentic-* plugin's yaml + the loader silently skipped them.

Does NOT actually build the wheel (too slow). Validates the CONTRACT:
1. Patterns exist in pyproject.toml package-data
2. Patterns exist in MANIFEST.in
3. Files that those patterns are MEANT to ship actually exist in-tree
   (so the patterns aren't degenerate / pointing at no files)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]


# ─── pyproject.toml package-data ─────────────────────────────

def _pyproject_text() -> str:
    return (REPO / "pyproject.toml").read_text()


def test_pyproject_plugins_includes_plugin_yaml_pattern():
    src = _pyproject_text()
    # The package-data block for `plugins` must reference plugin.yaml.
    # Allow either depth-2 or recursive ** form.
    assert "**/plugin.yaml" in src or "*/plugin.yaml" in src, (
        "pyproject.toml [tool.setuptools.package-data] for `plugins` "
        "must include plugin.yaml glob — otherwise wheel ships zero "
        "yaml files and every plugin is silently skipped at install."
    )


def test_pyproject_plugins_includes_md_pattern():
    src = _pyproject_text()
    assert "**/*.md" in src or "*/README.md" in src or "*/*.md" in src, (
        "pyproject.toml plugins package-data must ship per-plugin .md "
        "(README + SKILL.md within skills/ subdirs)"
    )


def test_pyproject_plugins_includes_skills_pattern():
    """Plugins like devagentic-canvas bundle skills/<name>/SKILL.md docs
    that the canvas tool surfaces via skill_view. Must ship."""
    src = _pyproject_text()
    assert "**/skills/**/*" in src or "*/skills/**/*" in src or "skills/" in src, (
        "plugins package-data must recursively include skills/ contents"
    )


# ─── MANIFEST.in ─────────────────────────────────────────────

def _manifest_text() -> str:
    return (REPO / "MANIFEST.in").read_text()


def test_manifest_in_recursive_includes_plugins_yaml():
    src = _manifest_text()
    # MANIFEST.in line for plugins must include yaml.
    lines = src.splitlines()
    rec = [l for l in lines if l.startswith("recursive-include plugins")]
    assert any("*.yaml" in l for l in rec), (
        "MANIFEST.in must contain `recursive-include plugins *.yaml ...` "
        "as belt-and-suspenders for sdist generation"
    )


def test_manifest_in_recursive_includes_plugins_md():
    src = _manifest_text()
    lines = src.splitlines()
    rec = [l for l in lines if l.startswith("recursive-include plugins")]
    assert any("*.md" in l for l in rec), (
        "MANIFEST.in must include plugins per-plugin .md files"
    )


# ─── In-tree files exist (patterns aren't degenerate) ────────

def test_at_least_one_plugin_yaml_exists():
    """If patterns are configured but no actual files exist, the
    packaging contract is vacuous. Catch that explicitly."""
    matches = list((REPO / "plugins").rglob("plugin.yaml"))
    assert len(matches) >= 5, (
        f"expected at least 5 plugin.yaml files in plugins/; found "
        f"{len(matches)}. Either tree is missing files (different bug) "
        f"OR packaging contract is pointing at nothing."
    )


def test_devagentic_g1_plugin_yaml_exists():
    """Specifically check the G1 plugin (devagentic-vertical-preamble)
    has its yaml — this was the root-cause plugin for the poly-explorer
    silent-failure that surfaced #76."""
    yaml = REPO / "plugins" / "devagentic-vertical-preamble" / "plugin.yaml"
    assert yaml.is_file(), (
        f"G1 plugin yaml missing at {yaml}; packaging fix would ship "
        f"nothing for that plugin even with patterns set correctly."
    )


def test_devagentic_g2_g3_g4_yamls_exist():
    """Same check for G2/G3/G4 plugins (the other silent-skipped ones)."""
    for plugin_dir in (
        "devagentic-mutations",      # G2
        "hermes-github",             # G3
        "devagentic-lane-h",         # G4
    ):
        yaml = REPO / "plugins" / plugin_dir / "plugin.yaml"
        assert yaml.is_file(), f"missing {yaml}"


# ─── Cross-check: ALL existing plugin.yaml will be matched by pattern ─

def test_all_plugin_yamls_covered_by_pyproject_glob():
    """Walk all plugin.yaml files in-tree; for each, verify the depth
    is covered by the configured glob patterns. Catches a regression
    where a deeply-nested plugin's yaml wouldn't match."""
    src = _pyproject_text()
    yamls = list((REPO / "plugins").rglob("plugin.yaml"))
    assert yamls, "no plugin.yaml files at all — packaging vacuous"

    # If pyproject has **/plugin.yaml, all depths covered.
    has_double_star = "**/plugin.yaml" in src
    if has_double_star:
        return  # Universal coverage; we're done.

    # Otherwise enumerate the depths and ensure each has a pattern.
    depths = set()
    for y in yamls:
        rel = y.relative_to(REPO / "plugins")
        depths.add(len(rel.parts) - 1)  # parts include filename

    for d in depths:
        prefix = "/".join(["*"] * d) + ("/" if d else "")
        pattern = f"{prefix}plugin.yaml"
        assert f'"{pattern}"' in src or f"'{pattern}'" in src, (
            f"depth-{d} plugin.yaml requires `{pattern}` glob in "
            f"pyproject [tool.setuptools.package-data]; not present"
        )
