"""Tests for the mistral-shaped tool_call recovery (hermes-agent#121).

OpenAI-spec ``tool_call.type`` is required with exactly one valid
value (``"function"``). Some providers (mistral observed; possibly
others) omit it. If the OpenAI Python SDK's strict Pydantic
validation drops these entries from
``response.choices[0].message.tool_calls``, hermes sees empty
tool_calls + finish_reason=tool_calls and burns the empty-retry
loop instead of executing the tool the model wanted to call.

This patch falls back to inspecting the raw response (via
``model_dump`` / dict shape) when the SDK's parsed list is empty
but the finish_reason signals tools. Missing ``type`` defaults to
``"function"`` per spec semantics.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.transports.chat_completions import (
    ChatCompletionsTransport,
    _DEFAULT_TOOL_CALL_TYPE,
    _normalize_raw_tool_call,
    _raw_tool_calls_from_response,
)
from agent.transports.types import ToolCall


# ─── _normalize_raw_tool_call ──────────────────────────────────

def test_normalize_raw_with_type_present():
    raw = {
        "id": "abc",
        "type": "function",
        "function": {"name": "write_file", "arguments": '{"x": 1}'},
    }
    out = _normalize_raw_tool_call(raw)
    assert isinstance(out, ToolCall)
    assert out.id == "abc"
    assert out.name == "write_file"
    assert out.arguments == '{"x": 1}'


def test_normalize_raw_with_type_missing_defaults_function():
    """The mistral-shape — type field absent. Should still parse."""
    raw = {
        "id": "5oTo1Uia0",
        "function": {"name": "write_file",
                     "arguments": '{"path": "/tmp/x"}'},
        "index": 0,
    }
    out = _normalize_raw_tool_call(raw)
    assert isinstance(out, ToolCall)
    assert out.id == "5oTo1Uia0"
    assert out.name == "write_file"
    # ToolCall's ``type`` property always returns "function".
    assert out.type == _DEFAULT_TOOL_CALL_TYPE


def test_normalize_raw_returns_none_when_function_missing():
    """Unrecoverable entry: no function block."""
    raw = {"id": "x", "type": "function"}
    assert _normalize_raw_tool_call(raw) is None


def test_normalize_raw_returns_none_when_name_missing():
    raw = {"id": "x", "function": {"arguments": "{}"}}
    assert _normalize_raw_tool_call(raw) is None


def test_normalize_raw_returns_none_when_not_dict():
    assert _normalize_raw_tool_call("not-a-dict") is None
    assert _normalize_raw_tool_call(None) is None


def test_normalize_raw_serializes_dict_arguments():
    """Some lenient parsers may already deserialize arguments to a
    dict — we re-serialize back to JSON string to satisfy the
    OpenAI contract (``arguments`` is a string)."""
    raw = {
        "id": "x",
        "function": {"name": "foo", "arguments": {"k": "v"}},
    }
    out = _normalize_raw_tool_call(raw)
    assert isinstance(out, ToolCall)
    assert isinstance(out.arguments, str)
    import json
    assert json.loads(out.arguments) == {"k": "v"}


def test_normalize_raw_empty_arguments_when_missing():
    raw = {"id": "x", "function": {"name": "foo"}}
    out = _normalize_raw_tool_call(raw)
    assert isinstance(out, ToolCall)
    assert out.arguments == ""


# ─── _raw_tool_calls_from_response ─────────────────────────────

def test_raw_extraction_via_model_dump():
    """Pydantic-v2 model with model_dump()."""
    class _FakeResp:
        def model_dump(self):
            return {
                "choices": [{
                    "message": {
                        "tool_calls": [
                            {"id": "5oTo1Uia0",
                             "function": {"name": "write_file",
                                          "arguments": "{}"},
                             "index": 0}
                        ],
                    }
                }]
            }
    raw_tcs = _raw_tool_calls_from_response(_FakeResp())
    assert len(raw_tcs) == 1
    assert raw_tcs[0]["function"]["name"] == "write_file"


def test_raw_extraction_via_dict():
    """Plain dict response (some test scaffolding shapes)."""
    resp = {
        "choices": [{
            "message": {
                "tool_calls": [{"id": "t", "function": {"name": "fn",
                                                         "arguments": "{}"}}]
            }
        }]
    }
    raw_tcs = _raw_tool_calls_from_response(resp)
    assert len(raw_tcs) == 1


def test_raw_extraction_returns_empty_when_no_tool_calls():
    class _FakeResp:
        def model_dump(self):
            return {"choices": [{"message": {"tool_calls": []}}]}
    assert _raw_tool_calls_from_response(_FakeResp()) == []


def test_raw_extraction_returns_empty_on_no_choices():
    class _FakeResp:
        def model_dump(self):
            return {"choices": []}
    assert _raw_tool_calls_from_response(_FakeResp()) == []


def test_raw_extraction_fail_soft_when_model_dump_raises():
    class _FakeResp:
        def model_dump(self):
            raise RuntimeError("dump blew up")
    assert _raw_tool_calls_from_response(_FakeResp()) == []


def test_raw_extraction_filters_non_dict_entries():
    """Defensive: malformed tool_calls list entries are silently
    skipped instead of crashing the recovery."""
    resp = {
        "choices": [{
            "message": {
                "tool_calls": ["string-entry", None,
                               {"id": "t", "function": {"name": "fn",
                                                         "arguments": "{}"}}],
            }
        }]
    }
    raw_tcs = _raw_tool_calls_from_response(resp)
    assert len(raw_tcs) == 1
    assert raw_tcs[0]["id"] == "t"


# ─── normalize_response end-to-end ─────────────────────────────

def _mistral_shaped_response(content="", finish_reason="tool_calls"):
    """Build a response object mimicking what mistral-large returns —
    SDK has dropped the type-less tool_call from
    ``message.tool_calls``, but the raw model_dump still has it."""
    _content = content  # capture for nested scopes
    _fr = finish_reason

    msg = SimpleNamespace(
        tool_calls=None,
        content=_content,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason=_fr)

    class _Resp:
        choices = [choice]
        usage = None
        def model_dump(self):
            return {
                "choices": [{
                    "message": {
                        "content": _content,
                        "tool_calls": [
                            {
                                "id": "5oTo1Uia0",
                                "function": {
                                    "name": "write_file",
                                    "arguments": '{"path": "/tmp/x"}',
                                },
                                "index": 0,
                                # NOTE: ``type`` field deliberately absent
                            }
                        ],
                    },
                    "finish_reason": _fr,
                }]
            }
    return _Resp()


def test_normalize_recovers_mistral_shaped_tool_calls():
    """End-to-end: SDK stripped the tool_call (msg.tool_calls=None)
    but the raw response still has it. normalize_response should
    pick it up via the #121 fallback."""
    transport = ChatCompletionsTransport()
    resp = _mistral_shaped_response(finish_reason="tool_calls")
    norm = transport.normalize_response(resp)
    assert norm.tool_calls is not None
    assert len(norm.tool_calls) == 1
    assert norm.tool_calls[0].name == "write_file"
    assert norm.tool_calls[0].id == "5oTo1Uia0"


def test_normalize_does_not_fall_back_on_stop_finish_reason():
    """When finish_reason is stop (not tool_calls), the recovery
    branch shouldn't fire — that's the #67 structural-empty case,
    not a tool-call SDK gap."""
    transport = ChatCompletionsTransport()
    resp = _mistral_shaped_response(finish_reason="stop")
    norm = transport.normalize_response(resp)
    # Empty tool_calls + stop → no recovery here. (#67/#108 path
    # handles upstream in conversation_loop.)
    assert norm.tool_calls is None or norm.tool_calls == []


def test_normalize_does_not_double_up_when_sdk_already_parsed():
    """If the SDK successfully parsed the tool_call, don't ALSO
    fall back to raw extraction — would produce duplicates."""
    transport = ChatCompletionsTransport()
    # Synthesize a response where msg.tool_calls is populated AND
    # model_dump also has the tool_calls. SDK-parsed wins.
    class _TcFunc:
        name = "write_file"
        arguments = '{"x": 1}'
    class _Tc:
        id = "sdk-id"
        function = _TcFunc()
        type = "function"
    class _Msg:
        tool_calls = [_Tc()]
        content = ""
        reasoning = None
        reasoning_content = None
        reasoning_details = None
    class _Choice:
        message = _Msg()
        finish_reason = "tool_calls"
    class _Resp:
        choices = [_Choice()]
        usage = None
        def model_dump(self):
            return {"choices": [{"message": {
                "tool_calls": [{"id": "raw-id",
                                "function": {"name": "write_file",
                                             "arguments": "{}"}}]}}]}
    norm = transport.normalize_response(_Resp())
    assert norm.tool_calls is not None
    assert len(norm.tool_calls) == 1
    # SDK-parsed entry kept — id "sdk-id" not "raw-id".
    assert norm.tool_calls[0].id == "sdk-id"


# ─── Source-level patch-landed check ───────────────────────────

def test_patch_landed_in_chat_completions():
    """The #121 recovery must be present in normalize_response.
    Catches accidental reverts."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "agent" / "transports" / "chat_completions.py")
    text = src.read_text()
    assert "hermes-agent#121" in text
    assert "_raw_tool_calls_from_response" in text
    assert "_normalize_raw_tool_call" in text
    assert "_DEFAULT_TOOL_CALL_TYPE" in text
