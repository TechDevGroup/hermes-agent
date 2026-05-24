"""Contract tests for G1/G2/G3/G4 plugin register() functions (hermes-agent#78).

For each of the four devagentic-* plugins, asserts:
1. `__init__.py` contains a `register(ctx)` function at module level.
2. The function is importable + callable.
3. G1 specifically: calling register() wires the pre_llm_call hook to
   preamble.on_pre_llm_call (the actual fix — without this the worker
   boots with no preamble).
4. G2/G3/G4: calling register() doesn't raise; doesn't register
   tools/hooks via the loader (MCP-only by design).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO / "plugins"


class _StubCtx:
    """Records what register() asks for. Mirrors the PluginContext
    surface we test against (hook + tool + command + skill)."""
    def __init__(self):
        self.hooks: list[tuple[str, object]] = []
        self.tools: list[str] = []
        self.commands: list[str] = []
        self.skills: list[str] = []

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def register_tool(self, *, name, **kw):
        self.tools.append(name)

    def register_command(self, *, name, **kw):
        self.commands.append(name)

    def register_skill(self, *, name, **kw):
        self.skills.append(name)


def _load_plugin_module(plugin_dir_name: str):
    """Load `plugins/<plugin_dir_name>/__init__.py` by path. Uses a
    unique sys.modules key per test so the relative import inside the
    plugin doesn't collide across tests."""
    plugin_dir = PLUGINS_DIR / plugin_dir_name
    pkg_name = f"plugin_under_test_{plugin_dir_name.replace('-', '_')}"

    # Set up a synthetic package so `from . import preamble` resolves.
    pkg = SimpleNamespace(__path__=[str(plugin_dir)], __name__=pkg_name)
    sys.modules[pkg_name] = pkg  # type: ignore[assignment]

    # Pre-load sibling modules so the package's relative imports find them.
    for sib in plugin_dir.glob("*.py"):
        if sib.name == "__init__.py":
            continue
        sib_name = f"{pkg_name}.{sib.stem}"
        spec = importlib.util.spec_from_file_location(sib_name, sib)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[sib_name] = mod
        spec.loader.exec_module(mod)

    init_spec = importlib.util.spec_from_file_location(
        pkg_name, plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    assert init_spec is not None and init_spec.loader is not None
    init_mod = importlib.util.module_from_spec(init_spec)
    sys.modules[pkg_name] = init_mod
    init_spec.loader.exec_module(init_mod)
    return init_mod


# ─── G1 vertical-preamble — the actual fix ────────────────────

def test_g1_register_wires_pre_llm_call_hook():
    """register() must wire pre_llm_call → preamble.on_pre_llm_call.
    Without this hook, the worker boots with no vertical preamble
    (the symptom that surfaced #78)."""
    mod = _load_plugin_module("devagentic-vertical-preamble")
    assert callable(getattr(mod, "register", None)), "G1 missing register()"
    ctx = _StubCtx()
    mod.register(ctx)
    hook_names = [h[0] for h in ctx.hooks]
    assert "pre_llm_call" in hook_names, (
        f"G1 register() must wire pre_llm_call hook; got {hook_names}"
    )
    # And specifically to preamble.on_pre_llm_call:
    pre_llm_hook = [h[1] for h in ctx.hooks if h[0] == "pre_llm_call"][0]
    assert getattr(pre_llm_hook, "__name__", "") == "on_pre_llm_call", (
        f"hook callback must be on_pre_llm_call; got {pre_llm_hook!r}"
    )


# ─── G2/G3/G4 — stub register() doesn't raise ───────────────

@pytest.mark.parametrize("plugin_name,description", [
    ("devagentic-mutations", "G2 mutations"),
    ("hermes-github", "G3 github"),
    ("devagentic-lane-h", "G4 lane-h"),
])
def test_g2_g3_g4_register_stub_is_callable_and_no_op(plugin_name, description):
    """MCP-only plugins should have register() that loads cleanly but
    doesn't wire anything via the plugin loader (tools are server-side
    in mcp_serve.py)."""
    mod = _load_plugin_module(plugin_name)
    assert callable(getattr(mod, "register", None)), (
        f"{description} missing register()"
    )
    ctx = _StubCtx()
    # Must not raise.
    mod.register(ctx)
    # MCP-only: nothing registered via the loader.
    assert ctx.hooks == [], f"{description} unexpectedly registered hooks: {ctx.hooks}"
    assert ctx.tools == [], f"{description} unexpectedly registered tools: {ctx.tools}"
    assert ctx.commands == [], f"{description} unexpectedly registered commands"


# ─── All four contain register() at module level (regression catcher) ─

@pytest.mark.parametrize("plugin_name", [
    "devagentic-vertical-preamble",
    "devagentic-mutations",
    "hermes-github",
    "devagentic-lane-h",
])
def test_plugin_init_contains_register_function(plugin_name):
    """Source-level regression: __init__.py must literally contain
    `def register(ctx)`. Catches the bug where the function silently
    disappears from the source (the original cause of this issue)."""
    init = PLUGINS_DIR / plugin_name / "__init__.py"
    src = init.read_text()
    assert "def register(ctx" in src, (
        f"{plugin_name}/__init__.py must define register(ctx) at "
        f"module level; loader skips plugin silently otherwise."
    )
