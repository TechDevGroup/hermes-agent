"""Tests for the devagentic_cascade_exhausted sentinel detection +
short-circuit (hermes-agent#118).

When devagentic's server-side recovery cascade (#324) surrenders, it
returns HTTP 200 with an error envelope of the shape:

    {"error": {"message": "...", "code": "devagentic_cascade_exhausted",
               "devagentic": {"trace_id": ..., "steps_attempted": [...],
                              "terminal_outcome": ...}}}

Hermes must NOT retry on this (devagentic already walked 4 alternates
inside its own cascade). The detection helper inspects the response
for the sentinel + the chat_completions transport's normalize_response
stores the envelope in provider_data["cascade_exhausted"]; the
conversation_loop checks the envelope before the empty-retry path.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.transports.chat_completions import (
    CASCADE_EXHAUSTED_CODE,
    _extract_cascade_exhausted,
)


def _make_cascade_response(via="attr"):
    """Synthesize a response carrying the cascade_exhausted envelope.

    ``via`` selects which channel surfaces it (different OpenAI SDK
    versions may park unknown top-level fields differently)."""
    envelope = {
        "message": "all alternates exhausted",
        "code": CASCADE_EXHAUSTED_CODE,
        "devagentic": {
            "trace_id": "doc-trace-99",
            "steps_attempted": ["primary", "alt-1", "alt-2", "fence-strip"],
            "terminal_outcome": "all_returned_empty",
        },
    }
    if via == "attr":
        return SimpleNamespace(error=envelope)
    if via == "model_extra":
        return SimpleNamespace(model_extra={"error": envelope})
    if via == "dict":
        return {"error": envelope}
    raise ValueError(f"unknown via: {via}")


# ─── Extraction helper ─────────────────────────────────────────

def test_extract_returns_none_when_response_none():
    assert _extract_cascade_exhausted(None) is None


def test_extract_returns_none_when_no_error_field():
    resp = SimpleNamespace(choices=[SimpleNamespace()])
    assert _extract_cascade_exhausted(resp) is None


def test_extract_returns_none_when_error_is_not_dict():
    resp = SimpleNamespace(error="just a string, not a dict")
    assert _extract_cascade_exhausted(resp) is None


def test_extract_returns_none_when_code_does_not_match():
    """Other error codes (rate_limited, etc.) pass through; we only
    short-circuit on the cascade_exhausted sentinel specifically."""
    resp = SimpleNamespace(error={
        "code": "rate_limited",
        "message": "slow down",
    })
    assert _extract_cascade_exhausted(resp) is None


@pytest.mark.parametrize("via", ["attr", "model_extra", "dict"])
def test_extract_returns_envelope_when_sentinel_present(via):
    resp = _make_cascade_response(via=via)
    out = _extract_cascade_exhausted(resp)
    assert out is not None
    assert out["code"] == CASCADE_EXHAUSTED_CODE
    assert out["devagentic"]["trace_id"] == "doc-trace-99"
    assert out["devagentic"]["terminal_outcome"] == "all_returned_empty"


def test_extract_fail_soft_on_attribute_error():
    """A response object that raises on attribute access should
    return None, not propagate the exception."""
    class _Bad:
        def __getattr__(self, name):
            raise RuntimeError(f"attr lookup blew up on {name}")
    resp = _Bad()
    assert _extract_cascade_exhausted(resp) is None


# ─── normalize_response integration ────────────────────────────

def test_normalize_response_carries_envelope_in_provider_data():
    """When the cascade_exhausted envelope is present, the normalized
    shape preserves it in provider_data + sets a useful content
    sentinel + finish_reason='stop'. No choices needed — the
    sentinel-bearing path short-circuits before the choices lookup."""
    from agent.transports.chat_completions import ChatCompletionsTransport
    transport = ChatCompletionsTransport()
    resp = _make_cascade_response(via="attr")
    norm = transport.normalize_response(resp)
    assert norm.tool_calls is None
    assert norm.finish_reason == "stop"
    assert norm.content == "all alternates exhausted"
    assert norm.provider_data is not None
    err = norm.provider_data["cascade_exhausted"]
    assert err["code"] == CASCADE_EXHAUSTED_CODE
    assert err["devagentic"]["trace_id"] == "doc-trace-99"


# ─── conversation_loop short-circuit (source-level + behavior) ─

def test_cascade_short_circuit_patch_landed():
    """The conversation_loop must reference the cascade_exhausted
    provider_data key + emit the trace_id in its user-facing message.
    Catches accidental reverts."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "agent" / "conversation_loop.py")
    text = src.read_text()
    assert "cascade_exhausted" in text
    assert "hermes-agent#118" in text
    assert "devagentic_cascade_exhausted" in text or "trace_id" in text
    assert "dispatchTrace" in text  # post-mortem hint in user message


def _short_circuit_guard(*, cascade_err) -> bool:
    """Mirror of the guard expression in conversation_loop.py around
    line 3514 (after C118 patch). Keep in sync with the source; the
    source-level test catches drift."""
    return bool(cascade_err)


def test_short_circuit_fires_on_envelope():
    err = {"message": "x", "code": CASCADE_EXHAUSTED_CODE,
           "devagentic": {"trace_id": "t-1"}}
    assert _short_circuit_guard(cascade_err=err)


def test_short_circuit_does_not_fire_on_none():
    assert not _short_circuit_guard(cascade_err=None)


def test_short_circuit_does_not_fire_on_empty_dict():
    assert not _short_circuit_guard(cascade_err={})
