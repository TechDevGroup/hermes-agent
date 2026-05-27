"""OpenAI Chat Completions transport.

Handles the default api_mode ('chat_completions') used by ~16 OpenAI-compatible
providers (OpenRouter, Nous, NVIDIA, Qwen, Ollama, DeepSeek, xAI, Kimi, etc.).

Messages and tools are already in OpenAI format — convert_messages and
convert_tools are near-identity.  The complexity lives in build_kwargs
which has provider-specific conditionals for max_tokens defaults,
reasoning configuration, temperature handling, and extra_body assembly.
"""

import copy
import os
from typing import Any, Dict, List, Optional

from agent.lmstudio_reasoning import resolve_lmstudio_effort
from agent.moonshot_schema import is_moonshot_model, sanitize_moonshot_tools
from agent.prompt_builder import DEVELOPER_ROLE_MODELS
from agent.transports.base import ProviderTransport
from agent.transports.types import NormalizedResponse, ToolCall, Usage


# Issue #115 — companion to devagentic#315 (initiative preamble).
# When set to ``required`` (the only currently-recognized value),
# every chat.completions.create() call where tools are attached is
# forced to ``tool_choice: "required"`` — closing the model-layer
# enforcement gap that the devagentic-side preamble's soft signal
# leaves open. Operator-controlled per session via env var; default
# behavior unchanged.
TOOL_USE_ENFORCEMENT_ENV = "HERMES_TOOL_USE_ENFORCEMENT"
_TOOL_USE_REQUIRED_VALUES = frozenset({"required"})


def _resolve_tool_use_enforcement() -> Optional[str]:
    """Return the active ``HERMES_TOOL_USE_ENFORCEMENT`` value when
    set to a recognized value, else ``None``. Unknown values fall
    through to None (doctor probe surfaces the typo separately so
    the runtime doesn't silently misbehave)."""
    raw = os.environ.get(TOOL_USE_ENFORCEMENT_ENV, "").strip().lower()
    if not raw:
        return None
    return raw if raw in _TOOL_USE_REQUIRED_VALUES else None


# Issue #118 — companion to devagentic#324 (server-side recovery
# cascade). Devagentic emits HTTP 200 with an error envelope of the
# shape ``{"error": {"message": ..., "code": "devagentic_cascade_exhausted",
# "devagentic": {"trace_id": ..., "steps_attempted": [...],
# "terminal_outcome": ...}}}`` when its 4-step recovery cascade fails.
# Hermes must NOT retry on this — devagentic already walked 4
# alternates inside its own cascade; another 3 hermes retries would
# stack 3 × 4 = 12 dispatches per user request (rate-budget blowout
# per devagentic#321).
CASCADE_EXHAUSTED_CODE = "devagentic_cascade_exhausted"


# Issue #121 — OpenAI-spec ``tool_call.type`` is a required string with
# exactly one valid value (``"function"``). Some providers (mistral
# observed; possibly others) omit the field on emitted tool_calls. If
# the OpenAI Python SDK's strict Pydantic validation drops these
# entries from ``response.choices[0].message.tool_calls``, hermes
# sees an empty list + finish_reason=tool_calls and falls into the
# #108 synthetic-recovery path — but the tool the model wanted to
# call is lost. Fall back to inspecting the raw response dict (via
# model_dump / model_extra) for tool_calls the SDK dropped, default
# missing ``type`` to ``"function"``, and reconstitute them.
_DEFAULT_TOOL_CALL_TYPE = "function"


def _raw_tool_calls_from_response(response: Any) -> list[dict]:
    """Read tool_calls from the raw response shape.

    Tries ``response.model_dump()`` (Pydantic v2), then attribute
    walk, then ``__dict__``. Returns a list of dicts; each entry is
    a raw OpenAI-shape tool_call (``id``, ``function`` /
    ``function.name`` / ``function.arguments``, optionally ``type``,
    ``index``).

    Fail-soft on attribute / parse errors — returns ``[]`` so the
    caller falls back to whatever was already parsed.
    """
    if response is None:
        return []
    # Pydantic v2 dump path
    dumped: dict | None = None
    if hasattr(response, "model_dump"):
        try:
            dumped = response.model_dump()
        except Exception:  # noqa: BLE001
            dumped = None
    if dumped is None and isinstance(response, dict):
        dumped = response
    if isinstance(dumped, dict):
        choices = dumped.get("choices") or []
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            if isinstance(msg, dict):
                tcs = msg.get("tool_calls") or []
                if isinstance(tcs, list):
                    return [tc for tc in tcs if isinstance(tc, dict)]
    return []


def _normalize_raw_tool_call(
    tc_raw: dict,
) -> Optional["ToolCall"]:
    """Build a ``ToolCall`` from a raw dict-shape entry, defaulting
    missing ``type`` to ``"function"`` per OpenAI-spec semantics
    (the field is a marker for future polymorphism with exactly one
    currently-valid value).

    Returns ``None`` when the entry is unrecoverable (missing
    function.name) so the caller skips it cleanly instead of
    constructing a half-built tool_call.
    """
    if not isinstance(tc_raw, dict):
        return None
    fn = tc_raw.get("function") or {}
    if not isinstance(fn, dict):
        return None
    name = fn.get("name")
    if not name:
        return None
    arguments = fn.get("arguments")
    if arguments is None:
        arguments = ""
    if not isinstance(arguments, str):
        # Arguments may already be a dict if the response is parsed
        # tolerantly elsewhere; serialize back to JSON string per
        # OpenAI contract.
        import json as _json
        try:
            arguments = _json.dumps(arguments)
        except Exception:  # noqa: BLE001
            arguments = str(arguments)
    return ToolCall(
        id=tc_raw.get("id"),
        name=name,
        arguments=arguments,
        provider_data=None,
    )


def _extract_cascade_exhausted(response: Any) -> Optional[Dict[str, Any]]:
    """Detect the ``devagentic_cascade_exhausted`` sentinel on a
    chat-completion response.

    Returns the error envelope dict when present (caller surfaces
    the ``trace_id`` + ``terminal_outcome`` to the user), else
    ``None``. Inspection order mirrors how OpenAI-SDK might park an
    unknown top-level field (Pydantic v2 ``model_extra``, plain
    attribute, ``__dict__``) so different SDK versions all surface
    the sentinel without hand-wiring per shape.

    Fail-soft on attribute errors — a malformed or non-dict
    ``error`` value collapses to None so the runtime falls back to
    the existing empty-retry path.
    """
    if response is None:
        return None
    err: Any = None
    try:
        err = getattr(response, "error", None)
    except Exception:  # noqa: BLE001
        err = None
    if err is None:
        # Pydantic v2 stores unknown fields in model_extra; check
        # there as a secondary path.
        try:
            extra = getattr(response, "model_extra", None)
            if isinstance(extra, dict):
                err = extra.get("error")
        except Exception:  # noqa: BLE001
            err = None
    if err is None and isinstance(response, dict):
        err = response.get("error")
    if not isinstance(err, dict):
        return None
    code = err.get("code")
    if code == CASCADE_EXHAUSTED_CODE:
        return err
    return None


def _maybe_inject_required_tool_choice(
    api_kwargs: Dict[str, Any], tools: Any,
) -> None:
    """Inject ``tool_choice: "required"`` into ``api_kwargs`` in
    place when (a) tools are attached and (b)
    ``HERMES_TOOL_USE_ENFORCEMENT=required`` is set.

    Does NOT override a caller-supplied ``tool_choice`` already on
    api_kwargs — the operator-set env is a default, not a clobber.
    Per devagentic#203 §1.3, session-tier defaults compose with
    dispatcher-tier role overrides; this respects that contract.
    """
    if not tools:
        return
    if api_kwargs.get("tool_choice") is not None:
        return  # caller already set it — don't clobber
    enforcement = _resolve_tool_use_enforcement()
    if enforcement == "required":
        api_kwargs["tool_choice"] = "required"


def _build_gemini_thinking_config(model: str, reasoning_config: dict | None) -> dict | None:
    """Translate Hermes/OpenRouter-style reasoning config to Gemini thinkingConfig."""
    if reasoning_config is None or not isinstance(reasoning_config, dict):
        return None

    normalized_model = (model or "").strip().lower()
    if normalized_model.startswith("google/"):
        normalized_model = normalized_model.split("/", 1)[1]

    # ``thinking_config`` is a Gemini-only request parameter. The same
    # ``gemini`` provider also serves Gemma (and historically PaLM/Bard);
    # those reject the field with HTTP 400 "Unknown name 'thinking_config':
    # Cannot find field" — including the polite ``{"includeThoughts": False}``
    # form. Omit the field entirely on non-Gemini models. (#17426)
    if not normalized_model.startswith("gemini"):
        return None

    if reasoning_config.get("enabled") is False:
        # Gemini can hide thought parts even when internal thinking still
        # happens; omit thinkingLevel to avoid model-specific validation quirks.
        return {"includeThoughts": False}

    effort = str(reasoning_config.get("effort", "medium") or "medium").strip().lower()
    if effort == "none":
        return {"includeThoughts": False}

    thinking_config: Dict[str, Any] = {"includeThoughts": True}

    # Gemini 2.5 accepts thinkingBudget; don't guess a budget from Hermes'
    # coarse effort levels. ``includeThoughts`` alone is enough to surface
    # thought parts without risking request validation errors.
    if normalized_model.startswith("gemini-2.5-"):
        return thinking_config

    if effort not in {"minimal", "low", "medium", "high", "xhigh"}:
        effort = "medium"

    # Gemini 3 Flash documents low/medium/high thinking levels; Gemini 3 Pro
    # is stricter (low/high). Clamp Hermes' wider effort set to what each
    # family accepts so we never forward an undocumented level verbatim.
    if normalized_model.startswith(("gemini-3", "gemini-3.1")):
        if "flash" in normalized_model:
            if effort in {"minimal", "low"}:
                thinking_config["thinkingLevel"] = "low"
            elif effort in {"high", "xhigh"}:
                thinking_config["thinkingLevel"] = "high"
            else:
                thinking_config["thinkingLevel"] = "medium"
        elif "pro" in normalized_model:
            thinking_config["thinkingLevel"] = (
                "high" if effort in {"high", "xhigh"} else "low"
            )

    return thinking_config


def _snake_case_gemini_thinking_config(config: dict | None) -> dict | None:
    """Convert Gemini thinking config keys to the OpenAI-compat field names."""
    if not isinstance(config, dict) or not config:
        return None

    translated: Dict[str, Any] = {}
    if isinstance(config.get("includeThoughts"), bool):
        translated["include_thoughts"] = config["includeThoughts"]
    if isinstance(config.get("thinkingLevel"), str) and config["thinkingLevel"].strip():
        translated["thinking_level"] = config["thinkingLevel"].strip().lower()
    if isinstance(config.get("thinkingBudget"), (int, float)):
        translated["thinking_budget"] = int(config["thinkingBudget"])
    return translated or None


def _is_gemini_openai_compat_base_url(base_url: Any) -> bool:
    normalized = str(base_url or "").strip().rstrip("/").lower()
    if not normalized:
        return False
    if "generativelanguage.googleapis.com" not in normalized:
        return False
    return normalized.endswith("/openai")


class ChatCompletionsTransport(ProviderTransport):
    """Transport for api_mode='chat_completions'.

    The default path for OpenAI-compatible providers.
    """

    @property
    def api_mode(self) -> str:
        return "chat_completions"

    def convert_messages(
        self, messages: list[dict[str, Any]], **kwargs
    ) -> list[dict[str, Any]]:
        """Messages are already in OpenAI format — sanitize Codex leaks only.

        Strips Codex Responses API fields (``codex_reasoning_items`` /
        ``codex_message_items`` on the message, ``call_id``/``response_item_id``
        on tool_calls) that strict chat-completions providers reject with 400/422.
        """
        needs_sanitize = False
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if "codex_reasoning_items" in msg or "codex_message_items" in msg:
                needs_sanitize = True
                break
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict) and (
                        "call_id" in tc or "response_item_id" in tc
                    ):
                        needs_sanitize = True
                        break
                if needs_sanitize:
                    break

        if not needs_sanitize:
            return messages

        sanitized = copy.deepcopy(messages)
        for msg in sanitized:
            if not isinstance(msg, dict):
                continue
            msg.pop("codex_reasoning_items", None)
            msg.pop("codex_message_items", None)
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        tc.pop("call_id", None)
                        tc.pop("response_item_id", None)
        return sanitized

    def convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Tools are already in OpenAI format — identity."""
        return tools

    def build_kwargs(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **params,
    ) -> dict[str, Any]:
        """Build chat.completions.create() kwargs.

        params (all optional):
            timeout: float — API call timeout
            max_tokens: int | None — user-configured max tokens
            ephemeral_max_output_tokens: int | None — one-shot override
            max_tokens_param_fn: callable — returns {max_tokens: N} or {max_completion_tokens: N}
            reasoning_config: dict | None
            request_overrides: dict | None
            session_id: str | None
            model_lower: str — lowercase model name for pattern matching
            # Provider profile path (all per-provider quirks live in providers/)
            provider_profile: ProviderProfile | None — when present, delegates to
                _build_kwargs_from_profile(); all flag params below are bypassed.
            # Legacy-path flags — only used when provider_profile is None
            # (i.e. custom / unregistered providers). Known providers all go
            # through provider_profile.
            is_openrouter: bool
            is_nous: bool
            is_qwen_portal: bool
            is_github_models: bool
            is_nvidia_nim: bool
            is_kimi: bool
            is_tokenhub: bool
            is_lmstudio: bool
            is_custom_provider: bool
            ollama_num_ctx: int | None
            # Provider routing
            provider_preferences: dict | None
            # Qwen-specific
            qwen_prepare_fn: callable | None — runs AFTER codex sanitization
            qwen_prepare_inplace_fn: callable | None — in-place variant for deepcopied lists
            qwen_session_metadata: dict | None
            # Temperature
            fixed_temperature: Any — from _fixed_temperature_for_model()
            omit_temperature: bool
            # Reasoning
            supports_reasoning: bool
            github_reasoning_extra: dict | None
            lmstudio_reasoning_options: list[str] | None  # raw allowed_options from /api/v1/models
            # Claude on OpenRouter/Nous max output
            anthropic_max_output: int | None
            extra_body_additions: dict | None
        """
        # Codex sanitization: drop reasoning_items / call_id / response_item_id
        sanitized = self.convert_messages(messages)

        # ── Provider profile: single-path when present ──────────────────
        _profile = params.get("provider_profile")
        if _profile:
            return self._build_kwargs_from_profile(
                _profile, model, sanitized, tools, params
            )

        # ── Legacy fallback (unregistered / unknown provider) ───────────
        # Reached only when get_provider_profile() returned None.
        # Known providers always go through the profile path above.

        # Developer role swap for GPT-5/Codex models
        model_lower = params.get("model_lower", (model or "").lower())
        if (
            sanitized
            and isinstance(sanitized[0], dict)
            and sanitized[0].get("role") == "system"
            and any(p in model_lower for p in DEVELOPER_ROLE_MODELS)
        ):
            sanitized = list(sanitized)
            sanitized[0] = {**sanitized[0], "role": "developer"}

        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": sanitized,
        }

        timeout = params.get("timeout")
        if timeout is not None:
            api_kwargs["timeout"] = timeout

        # Tools
        if tools:
            # Moonshot/Kimi uses a stricter flavored JSON Schema.  Rewriting
            # tool parameters here keeps aggregator routes (Nous, OpenRouter,
            # etc.) compatible, in addition to direct moonshot.ai endpoints.
            if is_moonshot_model(model):
                tools = sanitize_moonshot_tools(tools)
            api_kwargs["tools"] = tools

        # Issue #115: operator-set HERMES_TOOL_USE_ENFORCEMENT=required
        # forces tool_choice on every dispatch where tools are attached.
        _maybe_inject_required_tool_choice(api_kwargs, tools)

        # max_tokens resolution — priority: ephemeral > user > provider default
        max_tokens_fn = params.get("max_tokens_param_fn")
        ephemeral = params.get("ephemeral_max_output_tokens")
        max_tokens = params.get("max_tokens")
        anthropic_max_out = params.get("anthropic_max_output")
        is_nvidia_nim = params.get("is_nvidia_nim", False)
        is_kimi = params.get("is_kimi", False)
        is_tokenhub = params.get("is_tokenhub", False)
        reasoning_config = params.get("reasoning_config")

        if ephemeral is not None and max_tokens_fn:
            api_kwargs.update(max_tokens_fn(ephemeral))
        elif max_tokens is not None and max_tokens_fn:
            api_kwargs.update(max_tokens_fn(max_tokens))
        elif anthropic_max_out is not None:
            api_kwargs["max_tokens"] = anthropic_max_out

        # Kimi: top-level reasoning_effort (unless thinking disabled)
        if is_kimi:
            _kimi_thinking_off = bool(
                reasoning_config
                and isinstance(reasoning_config, dict)
                and reasoning_config.get("enabled") is False
            )
            if not _kimi_thinking_off:
                _kimi_effort = "medium"
                if reasoning_config and isinstance(reasoning_config, dict):
                    _e = (reasoning_config.get("effort") or "").strip().lower()
                    if _e in {"low", "medium", "high"}:
                        _kimi_effort = _e
                api_kwargs["reasoning_effort"] = _kimi_effort

        # Tencent TokenHub: top-level reasoning_effort (unless thinking disabled)
        if is_tokenhub:
            _tokenhub_thinking_off = bool(
                reasoning_config
                and isinstance(reasoning_config, dict)
                and reasoning_config.get("enabled") is False
            )
            if not _tokenhub_thinking_off:
                _tokenhub_effort = "high"
                if reasoning_config and isinstance(reasoning_config, dict):
                    _e = (reasoning_config.get("effort") or "").strip().lower()
                    if _e in {"low", "medium", "high"}:
                        _tokenhub_effort = _e
                api_kwargs["reasoning_effort"] = _tokenhub_effort

        # LM Studio: top-level reasoning_effort. Only emit when the model
        # declares reasoning support via /api/v1/models capabilities (gated
        # upstream by params["supports_reasoning"]). resolve_lmstudio_effort
        # is shared with run_agent's summary path so both stay in sync.
        if params.get("is_lmstudio", False) and params.get("supports_reasoning", False):
            _lm_effort = resolve_lmstudio_effort(
                reasoning_config,
                params.get("lmstudio_reasoning_options"),
            )
            if _lm_effort is not None:
                api_kwargs["reasoning_effort"] = _lm_effort

        # extra_body assembly
        extra_body: dict[str, Any] = {}

        is_openrouter = params.get("is_openrouter", False)
        is_nous = params.get("is_nous", False)
        is_github_models = params.get("is_github_models", False)
        provider_name = str(params.get("provider_name") or "").strip().lower()
        base_url = params.get("base_url")

        provider_prefs = params.get("provider_preferences")
        if provider_prefs and is_openrouter:
            extra_body["provider"] = provider_prefs

        # Pareto Code router plugin — model-gated. Same shape as the
        # profile path in plugins/model-providers/openrouter/__init__.py;
        # this branch only runs when the OpenRouter profile isn't loaded.
        if is_openrouter and model == "openrouter/pareto-code":
            _pareto_score = params.get("openrouter_min_coding_score")
            if _pareto_score is not None and _pareto_score != "":
                try:
                    _pareto_score_f = float(_pareto_score)
                except (TypeError, ValueError):
                    _pareto_score_f = None
                if _pareto_score_f is not None and 0.0 <= _pareto_score_f <= 1.0:
                    extra_body["plugins"] = [
                        {"id": "pareto-router", "min_coding_score": _pareto_score_f}
                    ]

        # Kimi extra_body.thinking
        if is_kimi:
            _kimi_thinking_enabled = True
            if reasoning_config and isinstance(reasoning_config, dict):
                if reasoning_config.get("enabled") is False:
                    _kimi_thinking_enabled = False
            extra_body["thinking"] = {
                "type": "enabled" if _kimi_thinking_enabled else "disabled",
            }

        # Reasoning. LM Studio is handled above via top-level reasoning_effort,
        # so skip emitting extra_body.reasoning for it.
        if params.get("supports_reasoning", False) and not params.get("is_lmstudio", False):
            if is_github_models:
                gh_reasoning = params.get("github_reasoning_extra")
                if gh_reasoning is not None:
                    extra_body["reasoning"] = gh_reasoning
            else:
                extra_body["reasoning"] = {"enabled": True, "effort": "medium"}

        if provider_name == "gemini":
            raw_thinking_config = _build_gemini_thinking_config(model, reasoning_config)
            if _is_gemini_openai_compat_base_url(base_url):
                thinking_config = _snake_case_gemini_thinking_config(raw_thinking_config)
                if thinking_config:
                    openai_compat_extra = extra_body.get("extra_body", {})
                    google_extra = openai_compat_extra.get("google", {})
                    google_extra["thinking_config"] = thinking_config
                    openai_compat_extra["google"] = google_extra
                    extra_body["extra_body"] = openai_compat_extra
            elif raw_thinking_config:
                extra_body["thinking_config"] = raw_thinking_config
        elif provider_name == "google-gemini-cli":
            thinking_config = _build_gemini_thinking_config(model, reasoning_config)
            if thinking_config:
                extra_body["thinking_config"] = thinking_config

        # Merge any pre-built extra_body additions
        additions = params.get("extra_body_additions")
        if additions:
            extra_body.update(additions)

        if extra_body:
            api_kwargs["extra_body"] = extra_body

        # Request overrides last (service_tier etc.)
        overrides = params.get("request_overrides")
        if overrides:
            api_kwargs.update(overrides)

        return api_kwargs

    def _build_kwargs_from_profile(self, profile, model, sanitized, tools, params):
        """Build API kwargs using a ProviderProfile — single path, no legacy flags.

        This method replaces the entire flag-based kwargs assembly when a
        provider_profile is passed. Every quirk comes from the profile object.
        """
        from providers.base import OMIT_TEMPERATURE

        # Message preprocessing
        sanitized = profile.prepare_messages(sanitized)

        # Developer role swap — model-name-based, applies to all providers
        _model_lower = (model or "").lower()
        if (
            sanitized
            and isinstance(sanitized[0], dict)
            and sanitized[0].get("role") == "system"
            and any(p in _model_lower for p in DEVELOPER_ROLE_MODELS)
        ):
            sanitized = list(sanitized)
            sanitized[0] = {**sanitized[0], "role": "developer"}

        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": sanitized,
        }

        # Temperature
        if profile.fixed_temperature is OMIT_TEMPERATURE:
            pass  # Don't include temperature at all
        elif profile.fixed_temperature is not None:
            api_kwargs["temperature"] = profile.fixed_temperature
        else:
            # Use caller's temperature if provided
            temp = params.get("temperature")
            if temp is not None:
                api_kwargs["temperature"] = temp

        # Timeout
        timeout = params.get("timeout")
        if timeout is not None:
            api_kwargs["timeout"] = timeout

        # Tools — apply Moonshot/Kimi schema sanitization regardless of path
        if tools:
            if is_moonshot_model(model):
                tools = sanitize_moonshot_tools(tools)
            api_kwargs["tools"] = tools

        # Issue #115: operator-set HERMES_TOOL_USE_ENFORCEMENT=required
        # forces tool_choice on every dispatch where tools are attached.
        _maybe_inject_required_tool_choice(api_kwargs, tools)

        # max_tokens resolution — priority: ephemeral > user > profile default
        max_tokens_fn = params.get("max_tokens_param_fn")
        ephemeral = params.get("ephemeral_max_output_tokens")
        user_max = params.get("max_tokens")
        anthropic_max = params.get("anthropic_max_output")

        if ephemeral is not None and max_tokens_fn:
            api_kwargs.update(max_tokens_fn(ephemeral))
        elif user_max is not None and max_tokens_fn:
            api_kwargs.update(max_tokens_fn(user_max))
        elif profile.default_max_tokens and max_tokens_fn:
            api_kwargs.update(max_tokens_fn(profile.default_max_tokens))
        elif anthropic_max is not None:
            api_kwargs["max_tokens"] = anthropic_max

        # Provider-specific api_kwargs extras (reasoning_effort, metadata, etc.)
        reasoning_config = params.get("reasoning_config")
        extra_body_from_profile, top_level_from_profile = (
            profile.build_api_kwargs_extras(
                reasoning_config=reasoning_config,
                supports_reasoning=params.get("supports_reasoning", False),
                qwen_session_metadata=params.get("qwen_session_metadata"),
                model=model,
                ollama_num_ctx=params.get("ollama_num_ctx"),
                session_id=params.get("session_id"),
            )
        )
        api_kwargs.update(top_level_from_profile)

        # extra_body assembly
        extra_body: dict[str, Any] = {}

        # Profile's extra_body (tags, provider prefs, vl_high_resolution, etc.)
        profile_body = profile.build_extra_body(
            session_id=params.get("session_id"),
            provider_preferences=params.get("provider_preferences"),
            model=model,
            base_url=params.get("base_url"),
            reasoning_config=reasoning_config,
            openrouter_min_coding_score=params.get("openrouter_min_coding_score"),
        )
        if profile_body:
            extra_body.update(profile_body)

        # Profile's reasoning/thinking extra_body entries
        if extra_body_from_profile:
            extra_body.update(extra_body_from_profile)

        # Merge any pre-built extra_body additions from the caller
        additions = params.get("extra_body_additions")
        if additions:
            extra_body.update(additions)

        # Request overrides (user config)
        overrides = params.get("request_overrides")
        if overrides:
            for k, v in overrides.items():
                if k == "extra_body" and isinstance(v, dict):
                    extra_body.update(v)
                else:
                    api_kwargs[k] = v

        if extra_body:
            api_kwargs["extra_body"] = extra_body

        return api_kwargs

    def normalize_response(self, response: Any, **kwargs) -> NormalizedResponse:
        """Normalize OpenAI ChatCompletion to NormalizedResponse.

        For chat_completions, this is near-identity — the response is already
        in OpenAI format.  extra_content on tool_calls (Gemini thought_signature)
        is preserved via ToolCall.provider_data.  reasoning_details (OpenRouter
        unified format) and reasoning_content (DeepSeek/Moonshot) are also
        preserved for downstream replay.

        Issue #118: when devagentic-local returns the
        ``devagentic_cascade_exhausted`` sentinel (HTTP 200 with an
        error envelope + no choices), short-circuit to a NormalizedResponse
        carrying the envelope in ``provider_data["cascade_exhausted"]``.
        Callers (conversation_loop) check this and skip the retry path.
        """
        cascade_err = _extract_cascade_exhausted(response)
        if cascade_err is not None:
            # No choices to normalize — return a sentinel-bearing
            # NormalizedResponse. ``content`` carries the human-
            # readable error message so chat surfaces show something
            # useful even if the caller doesn't unpack provider_data.
            return NormalizedResponse(
                content=str(cascade_err.get("message") or
                            "devagentic cascade exhausted"),
                tool_calls=None,
                finish_reason="stop",
                reasoning=None,
                usage=None,
                provider_data={"cascade_exhausted": cascade_err},
            )

        choice = response.choices[0]
        msg = choice.message
        finish_reason = choice.finish_reason or "stop"

        tool_calls = None
        if msg.tool_calls:
            tool_calls = []
            for tc in msg.tool_calls:
                # Preserve provider-specific extras on the tool call.
                # Gemini 3 thinking models attach extra_content with
                # thought_signature — without replay on the next turn the API
                # rejects the request with 400.
                tc_provider_data: dict[str, Any] = {}
                extra = getattr(tc, "extra_content", None)
                if extra is None and hasattr(tc, "model_extra"):
                    extra = (tc.model_extra or {}).get("extra_content")
                if extra is not None:
                    if hasattr(extra, "model_dump"):
                        try:
                            extra = extra.model_dump()
                        except Exception:
                            pass
                    tc_provider_data["extra_content"] = extra
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=tc.function.arguments,
                        provider_data=tc_provider_data or None,
                    )
                )

        # hermes-agent#121: when the SDK's strict Pydantic validation
        # dropped tool_call entries that omitted the OpenAI-spec-
        # required ``type`` field (mistral-large emits this shape),
        # ``msg.tool_calls`` reaches us empty even though the wire
        # response carried tool_calls. Recover them from the raw
        # response dict; default missing ``type`` to ``"function"``
        # (the only valid value per spec). Only fires when the
        # finish_reason explicitly signals tool emission AND the
        # parsed list is empty — strictly additive recovery.
        if not tool_calls and finish_reason in {"tool_calls", "function_call"}:
            raw_tcs = _raw_tool_calls_from_response(response)
            if raw_tcs:
                recovered: list[ToolCall] = []
                for raw_tc in raw_tcs:
                    tc_norm = _normalize_raw_tool_call(raw_tc)
                    if tc_norm is not None:
                        recovered.append(tc_norm)
                if recovered:
                    tool_calls = recovered

        usage = None
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            usage = Usage(
                prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(u, "completion_tokens", 0) or 0,
                total_tokens=getattr(u, "total_tokens", 0) or 0,
            )

        # Preserve reasoning fields separately.  DeepSeek/Moonshot use
        # ``reasoning_content``; others use ``reasoning``.  Downstream code
        # (_extract_reasoning, thinking-prefill retry) reads both distinctly,
        # so keep them apart in provider_data rather than merging.
        reasoning = getattr(msg, "reasoning", None)
        reasoning_content = getattr(msg, "reasoning_content", None)
        if reasoning_content is None and hasattr(msg, "model_extra"):
            model_extra = getattr(msg, "model_extra", None) or {}
            if isinstance(model_extra, dict) and "reasoning_content" in model_extra:
                reasoning_content = model_extra["reasoning_content"]

        provider_data: Dict[str, Any] = {}
        if reasoning_content is not None:
            provider_data["reasoning_content"] = reasoning_content
        rd = getattr(msg, "reasoning_details", None)
        if rd:
            provider_data["reasoning_details"] = rd

        return NormalizedResponse(
            content=msg.content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning=reasoning,
            usage=usage,
            provider_data=provider_data or None,
        )

    def validate_response(self, response: Any) -> bool:
        """Check that response has valid choices."""
        if response is None:
            return False
        if not hasattr(response, "choices") or response.choices is None:
            return False
        if not response.choices:
            return False
        return True

    def extract_cache_stats(self, response: Any) -> dict[str, int] | None:
        """Extract OpenRouter/OpenAI cache stats from prompt_tokens_details."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        details = getattr(usage, "prompt_tokens_details", None)
        if details is None:
            return None
        cached = getattr(details, "cached_tokens", 0) or 0
        written = getattr(details, "cache_write_tokens", 0) or 0
        if cached or written:
            return {"cached_tokens": cached, "creation_tokens": written}
        return None


# Auto-register on import
from agent.transports import register_transport  # noqa: E402

register_transport("chat_completions", ChatCompletionsTransport)
