"""Tests that the background review agent restricts tools at runtime, not at schema time.

Regression coverage for issue #15204 (the background skill-review agent must
not perform non-skill side effects like terminal, send_message, delegate_task)
combined with issue #25322 / PR #17276 (the review fork must hit the parent's
Anthropic/OpenRouter prefix cache).

Reconciling the two: the fork now inherits the parent's full ``tools`` schema
so the cache-key matches, and enforces the memory+skills restriction at
runtime via a thread-local whitelist on the existing
``get_pre_tool_call_block_message`` gate. Safety is preserved mechanically
(any non-whitelisted dispatch is blocked) without the schema-level narrowing
that caused the prefix-cache miss.
"""

import threading
from unittest.mock import patch


def _make_agent_stub(agent_cls):
    """Create a minimal AIAgent-like object with just enough state for _spawn_background_review."""
    agent = object.__new__(agent_cls)
    agent.model = "test-model"
    agent.platform = "test"
    agent.provider = "openai"
    agent.session_id = "sess-123"
    agent.quiet_mode = True
    agent._memory_store = None
    agent._memory_enabled = True
    agent._user_profile_enabled = False
    agent._memory_nudge_interval = 5
    agent._skill_nudge_interval = 5
    agent.background_review_callback = None
    agent.status_callback = None
    agent._cached_system_prompt = None
    import datetime as _dt
    agent.session_start = _dt.datetime(2026, 1, 1, 12, 0, 0)
    agent._MEMORY_REVIEW_PROMPT = "review memory"
    agent._SKILL_REVIEW_PROMPT = "review skills"
    agent._COMBINED_REVIEW_PROMPT = "review both"
    return agent


class _SyncThread:
    """Drop-in replacement for threading.Thread that runs the target inline."""

    def __init__(self, *, target=None, daemon=None, name=None):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def test_background_review_does_not_narrow_toolset_schema():
    """The review fork must NOT pass enabled_toolsets to AIAgent.

    Narrowing the schema diverges the ``tools`` cache key from the parent's,
    which sits above ``system`` in Anthropic's cache hierarchy and forces a
    full prefix-cache miss on every review (see #25322, PR #17276).
    """
    import run_agent

    agent = _make_agent_stub(run_agent.AIAgent)
    captured = {}

    def _capture_init(self, *args, **kwargs):
        captured["enabled_toolsets"] = kwargs.get("enabled_toolsets", "UNSET")
        raise RuntimeError("stop after capturing init args")

    with patch.object(run_agent.AIAgent, "__init__", _capture_init), \
         patch("threading.Thread", _SyncThread):
        agent._spawn_background_review(
            messages_snapshot=[],
            review_memory=True,
            review_skills=False,
        )

    assert "enabled_toolsets" in captured, "AIAgent.__init__ was not called"
    # The kwarg must be absent — letting AIAgent inherit the default full
    # toolset so the schema bytes match the parent's.
    assert captured["enabled_toolsets"] == "UNSET", (
        f"Review fork narrowed the toolset schema (got {captured['enabled_toolsets']!r}), "
        "which breaks prefix-cache parity with the parent."
    )


def test_background_review_installs_thread_local_whitelist():
    """The review fork must install a memory/skills-only thread-local whitelist.

    The schema-level toolset narrowing was lifted (for prefix-cache parity),
    so #15204's safety contract now relies on the runtime whitelist gate to
    deny terminal/send_message/delegate_task at dispatch time. Verify the
    whitelist is set with exactly the memory+skills tool names.
    """
    import run_agent
    from hermes_cli import plugins as _plugins

    captured = {}

    def _capture_whitelist(whitelist, deny_msg_fmt=None):
        captured["whitelist"] = set(whitelist)
        captured["deny_msg_fmt"] = deny_msg_fmt
        # Stop here — we just want to see what gets installed.
        raise RuntimeError("stop after capturing whitelist")

    agent = _make_agent_stub(run_agent.AIAgent)

    def _no_init(self, *args, **kwargs):
        # Don't crash AIAgent.__init__; let execution flow reach
        # set_thread_tool_whitelist.
        return None

    with patch.object(run_agent.AIAgent, "__init__", _no_init), \
         patch.object(_plugins, "set_thread_tool_whitelist", _capture_whitelist), \
         patch("threading.Thread", _SyncThread):
        agent._spawn_background_review(
            messages_snapshot=[],
            review_memory=True,
            review_skills=False,
        )

    assert "whitelist" in captured, "set_thread_tool_whitelist was not called"
    whitelist = captured["whitelist"]
    # memory + skills tools must be allowed
    assert "memory" in whitelist
    assert "skill_manage" in whitelist
    assert "skill_view" in whitelist
    assert "skills_list" in whitelist
    # dangerous tools must NOT be in the whitelist
    assert "terminal" not in whitelist
    assert "send_message" not in whitelist
    assert "delegate_task" not in whitelist
    assert "web_search" not in whitelist
    assert "execute_code" not in whitelist


def test_background_review_agent_tools_are_limited():
    """Verify the resolved memory+skills toolsets only contain memory and skill tools.

    Sanity check on the source of truth for what the runtime whitelist is
    derived from — if a future PR adds e.g. `terminal` to the `memory`
    toolset, the review-fork safety contract silently breaks.
    """
    from toolsets import resolve_multiple_toolsets

    expected_tools = set(resolve_multiple_toolsets(["memory", "skills"]))

    assert "memory" in expected_tools
    assert "skill_manage" in expected_tools
    assert "skill_view" in expected_tools
    assert "skills_list" in expected_tools

    assert "terminal" not in expected_tools
    assert "send_message" not in expected_tools
    assert "delegate_task" not in expected_tools
    assert "web_search" not in expected_tools
    assert "execute_code" not in expected_tools


# ── hermes-agent#163: front-loaded curation-only preamble ────────────


def test_background_review_user_message_is_front_loaded_with_curation_framing():
    """#163: the user_message handed to the forked review agent must START
    with the MEMORY/SKILL CURATION framing — NOT have it tacked on at the
    end after the review body. The main turn's tool-using context primes
    the model strongly; a trailer is easy to miss against that priming,
    which is why the fork kept attempting write_file/patch and generating
    recurring denial 'error' lines."""
    import run_agent
    from hermes_cli import plugins as _plugins

    captured = {}

    def _capture_run(self, *, user_message, conversation_history):
        captured["user_message"] = user_message
        # Stop the harness — we've got what we came for.
        raise RuntimeError("stop after capturing user_message")

    def _no_init(self, *args, **kwargs):
        return None

    def _passthrough_whitelist(whitelist, deny_msg_fmt=None):
        # Let execution flow into run_conversation; just record nothing here.
        return None

    agent = _make_agent_stub(run_agent.AIAgent)

    with patch.object(run_agent.AIAgent, "__init__", _no_init), \
         patch.object(run_agent.AIAgent, "run_conversation", _capture_run), \
         patch.object(_plugins, "set_thread_tool_whitelist",
                      _passthrough_whitelist), \
         patch.object(_plugins, "clear_thread_tool_whitelist",
                      lambda: None), \
         patch("threading.Thread", _SyncThread):
        agent._spawn_background_review(
            messages_snapshot=[],
            review_memory=True,
            review_skills=False,
        )

    assert "user_message" in captured, "run_conversation was not reached"
    msg = captured["user_message"]
    # Framing must be at the TOP, not the bottom — it must precede the
    # review prompt body (which contains the literal "review memory" stub
    # set up by _make_agent_stub).
    framing_idx = msg.find("MEMORY/SKILL CURATION PASS")
    body_idx = msg.find("review memory")
    assert framing_idx >= 0, "curation framing absent from user_message"
    assert body_idx > framing_idx, (
        "framing must come BEFORE the review prompt body (front-loaded), "
        f"got framing@{framing_idx} body@{body_idx}"
    )
    # Disarm the main-turn priming explicitly.
    assert "already completed any file edits" in msg, (
        "user_message must explicitly disarm the main turn's file/code "
        "priming so the model doesn't try to repeat it"
    )
    # Name the actually-allowed curation tools so the model knows its
    # exact surface (derived from the runtime whitelist — see the source).
    for name in ("memory", "skill_manage", "skill_view", "skills_list"):
        assert name in msg, (
            f"curation preamble must list allowed tool {name!r}"
        )
    # The legacy weak trailer is gone — confirmation that the front-loaded
    # preamble REPLACED it, not just supplemented it.
    assert "You can only call memory and skill management tools" not in msg, (
        "the old weak trailer should be removed in favor of the "
        "front-loaded preamble (#163)"
    )


def test_background_review_preamble_landed_in_source():
    """Source-level regression for #163: prevent silent drift back to the
    weak trailer if a later refactor accidentally restores it."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2]
           / "agent" / "background_review.py").read_text()
    assert "hermes-agent#163" in src, "#163 marker absent — patch may be gone"
    assert "MEMORY/SKILL CURATION PASS" in src
    assert "_curation_preamble" in src
    # Anchor on the precise old trailer text — if anything re-introduces
    # the lone-trailer phrasing, this fails loudly.
    assert (
        "You can only call memory and skill "
        "management tools. Other tools will be denied "
        "at runtime — do not attempt them."
    ) not in src, (
        "the old weak trailer is back — #163 was supposed to remove it"
    )
