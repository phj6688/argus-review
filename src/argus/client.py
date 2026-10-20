"""Model client for gateway, anthropic, openrouter, openai, and CLI providers.

The gateway and anthropic lanes use the messages API with a gateway token or
ANTHROPIC_API_KEY. The openrouter and openai lanes use chat completions, including
local compatible endpoints. The claude-cli and codex-cli lanes dispatch lazily
to subscription-backed command-line providers. ARGUS_PROVIDER and credentials
select the lane; slash ids use openrouter unless openai is explicitly selected.
"""

import json
import logging
import os
import re
import time
import threading
from contextlib import contextmanager
from contextvars import ContextVar

from . import providers
from .providers import BackendNotConfigured
from .repo_policy import env_int
from dataclasses import dataclass

import httpx
from anthropic import Anthropic, BadRequestError

log = logging.getLogger("argus.client")

DEFAULT_MODEL = os.environ.get("ARGUS_MODEL") or providers.default_model()
ENV_BASE_URL = "ANTHROPIC_BASE_URL"
ENV_AUTH_TOKEN = "ANTHROPIC_AUTH_TOKEN"
ENV_ANTHROPIC_KEY = "ANTHROPIC_API_KEY"
ENV_OPENAI_KEY = "OPENAI_API_KEY"
ENV_OPENAI_BASE_URL = "OPENAI_BASE_URL"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
# Passed explicitly: the SDK re-reads ANTHROPIC_BASE_URL when given None, and compose
# sets that variable to an empty string for API-key users.
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ENV_OPENAI_TOKEN_PARAM = "ARGUS_OPENAI_TOKEN_PARAM"
PING_MAX_TOKENS = 16

# Resilience against a briefly-overloaded gateway. A shared gateway can serialize
# many apps' traffic, so transient 429/5xx/529/timeout is the dominant failure
# mode (not diff content). The SDK already backs off on those with jitter and
# honours Retry-After; it just needs more headroom than the default 2 retries.
# Timeout is generous because a REVIEW_MAX_TOKENS (8k) completion through the
# router can legitimately run a couple of minutes; too tight a timeout would
# manufacture the very errors we are trying to kill. Both env-tunable so the
# operator can adjust without a redeploy.
DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_MAX_RETRIES = 4
ENV_TIMEOUT = "ARGUS_LLM_TIMEOUT"
ENV_MAX_RETRIES = "ARGUS_LLM_MAX_RETRIES"


LLM_MAX_CONCURRENCY = env_int("ARGUS_LLM_MAX_CONCURRENCY", 3)
_llm_semaphore = threading.BoundedSemaphore(LLM_MAX_CONCURRENCY)
_llm_lock = threading.Lock()
_llm_active = 0
_prompt_cache: ContextVar[tuple[str, str] | None] = ContextVar("prompt_cache", default=None)


@contextmanager
def prompt_cache_scope(key: str, prefix: str = ""):
    token = _prompt_cache.set((key, prefix))
    try:
        yield
    finally:
        _prompt_cache.reset(token)


def llm_in_flight() -> int:
    with _llm_lock:
        return _llm_active


@contextmanager
def llm_slot():
    global _llm_active
    with _llm_semaphore:
        with _llm_lock:
            _llm_active += 1
        try:
            yield
        finally:
            with _llm_lock:
                _llm_active -= 1


class ToolCallMismatch(ValueError):
    """The reply did not hold exactly one call to the requested tool."""


class CliError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _env_number(name: str, default, cast):
    """Parse a positive numeric env override, falling back on absent/garbage."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def build_client(provider: str | None = None) -> Anthropic:
    base_url = os.environ.get(ENV_BASE_URL, "").strip()
    auth_token = os.environ.get(ENV_AUTH_TOKEN, "").strip()
    api_key = os.environ.get(ENV_ANTHROPIC_KEY, "").strip()
    timeout = _env_number(ENV_TIMEOUT, DEFAULT_TIMEOUT_SECONDS, float)
    max_retries = _env_number(ENV_MAX_RETRIES, DEFAULT_MAX_RETRIES, int)
    if provider == providers.GATEWAY and not (base_url and auth_token):
        raise BackendNotConfigured(f"Set {ENV_BASE_URL} and {ENV_AUTH_TOKEN} for gateway access.")
    if provider == providers.ANTHROPIC and not api_key:
        raise BackendNotConfigured(f"Set {ENV_ANTHROPIC_KEY} for direct API access.")
    if provider in (None, providers.GATEWAY) and base_url and auth_token:
        return Anthropic(
            base_url=base_url, auth_token=auth_token,
            timeout=timeout, max_retries=max_retries,
        )
    if api_key:
        return Anthropic(
            api_key=api_key, base_url=base_url or DEFAULT_ANTHROPIC_BASE_URL,
            timeout=timeout, max_retries=max_retries,
        )
    raise BackendNotConfigured(
        f"Set {ENV_BASE_URL} and {ENV_AUTH_TOKEN} for gateway access, "
        f"or {ENV_ANTHROPIC_KEY} for direct API access."
    )


def ping(model: str = DEFAULT_MODEL) -> str:
    provider = providers.resolve(model)
    if provider in (providers.CLAUDE_CLI, providers.CODEX_CLI):
        from . import cli_providers
        return cli_providers.get(provider).ping(model)
    if provider == providers.OPENROUTER:
        data = _openrouter_request({
            "model": model, "max_tokens": PING_MAX_TOKENS, "temperature": 0,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        })
        return (data["choices"][0]["message"].get("content") or "").strip()
    if provider == providers.OPENAI:
        data = _openai_request({
            "model": model, _openai_token_param(): PING_MAX_TOKENS,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        })
        return (data["choices"][0]["message"].get("content") or "").strip()
    with llm_slot():
        response = build_client(provider).messages.create(
            model=model,
            max_tokens=PING_MAX_TOKENS,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        )
    return "".join(block.text for block in response.content or [] if block.type == "text").strip()


@dataclass(frozen=True)
class ToolSpec:
    name: str
    input_schema: dict
    max_tokens: int


def _ask(user: str, tool: ToolSpec) -> str:
    """The user turn plus an instruction that names the one tool to call.

    It repeats the one instruction the call cannot work without, naming the
    tool on the turn every model reads.
    """
    return f"{user}\n\nAnswer by calling the {tool.name} tool exactly once. Do not reply with text."


def complete_tool(model: str, system: str, user: str, tool: ToolSpec) -> dict:
    """Ask the model to call one tool and return its input (structured output).

    API lanes use auto tool choice because some models reject forced calls.
    The prompt names the tool and each request asks for at most one call.
    A missing or split result gets one more try before the error path takes over.
    """
    try:
        return _complete_tool_once(model, system, user, tool)
    except ToolCallMismatch as error:
        log.warning("%s; asking once more", error)
    return _complete_tool_once(model, system, user, tool)


def _complete_tool_once(model: str, system: str, user: str, tool: ToolSpec) -> dict:
    provider = providers.resolve(model)
    if provider == providers.OPENROUTER:
        return _complete_tool_openrouter(model, system, user, tool)
    if provider == providers.OPENAI:
        return _complete_tool_openai(model, system, user, tool)
    if provider in (providers.CLAUDE_CLI, providers.CODEX_CLI):
        from . import cli_providers
        return cli_providers.get(provider).complete_tool(model, system, user, tool)
    return _complete_tool_anthropic(model, system, user, tool, provider=provider)


def _complete_tool_anthropic(
    model: str, system: str, user: str, tool: ToolSpec, provider: str | None = None,
) -> dict:
    definition = {"name": tool.name, "description": f"Report the structured result for {tool.name}.",
                  "input_schema": tool.input_schema, "strict": True}
    client = build_client(provider)
    scope = _prompt_cache.get()
    content = _ask(user, tool)
    kwargs = {
        "model": model,
        "max_tokens": tool.max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "tools": [definition],
        "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
    }
    if scope is not None:
        key, prefix = scope
        if not model.startswith("claude"):
            kwargs["extra_headers"] = {"X-Claude-Code-Session-Id": key}
            kwargs["extra_body"] = {"prompt_cache_key": key}
        elif prefix and user.startswith(prefix):
            kwargs["messages"][0]["content"] = [
                {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": content[len(prefix):]},
            ]

    def request():
        return client.messages.create(**kwargs)
    try:
        with llm_slot():
            response = request()
    except BadRequestError as error:
        if "strict" not in str(error).lower():
            raise
        log.warning("%s rejected strict tools; retrying without strict", model)
        kwargs["tools"] = [{key: value for key, value in definition.items() if key != "strict"}]
        with llm_slot():
            response = request()
    usage = getattr(response, "usage", None)
    log.info(
        "llm usage model=%s input=%s cache_read=%s cache_write=%s output=%s cache_key=%s",
        model, getattr(usage, "input_tokens", 0) or 0,
        getattr(usage, "cache_read_input_tokens", 0) or 0,
        getattr(usage, "cache_creation_input_tokens", 0) or 0,
        getattr(usage, "output_tokens", 0) or 0,
        scope[0][:8] if scope is not None else "-",
    )
    # The gateway has returned a reply with no content list at all; that is a missed
    # call to retry, not a crash of the whole review.
    blocks = response.content or []
    calls = [b for b in blocks if b.type == "tool_use"]
    if len(calls) == 1 and calls[0].name == tool.name:
        return calls[0].input
    stop = f"stop_reason: {getattr(response, 'stop_reason', None)}"
    if len(calls) > 1:
        raise ToolCallMismatch(f"model made {len(calls)} tool calls, expected one {tool.name}; {stop}")
    raise ToolCallMismatch(
        f"model did not call {tool.name}; blocks: {[b.type for b in blocks]}; {stop}")


# --------------------------------------------------------------------------
# Chat-completions lanes
# --------------------------------------------------------------------------

ENV_OPENROUTER_KEY = "OPENROUTER_API_KEY"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# 4xx client errors are permanent for this request (402 until the operator tops
# up, 401 bad key, 400/422 bad payload); retrying them only burns time.
_NO_RETRY_STATUSES = frozenset({400, 401, 402, 403, 404, 422})
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def is_openrouter_model(model: str) -> bool:
    """Whether an id uses the vendor/model syntax, regardless of provider override."""
    return "/" in model


def _openrouter_request(payload: dict) -> dict:
    key = os.environ.get(ENV_OPENROUTER_KEY, "").strip()
    if not key:
        raise BackendNotConfigured(
            f"Set {ENV_OPENROUTER_KEY} to review with OpenRouter models "
            f"(requested {payload.get('model')!r})."
        )
    return _chat_request(OPENROUTER_URL, key, payload, "openrouter", key_required=True)


def _openai_token_param() -> str:
    if os.environ.get(ENV_OPENAI_TOKEN_PARAM) == "max_tokens":
        return "max_tokens"
    return "max_completion_tokens"


def _openai_request(payload: dict) -> dict:
    base_url = os.environ.get(ENV_OPENAI_BASE_URL, "").strip() or DEFAULT_OPENAI_BASE_URL
    key = os.environ.get(ENV_OPENAI_KEY, "").strip()
    return _chat_request(base_url.rstrip("/") + "/chat/completions", key, payload,
                         "openai", key_required=False)


def _chat_request(url: str, key: str | None, payload: dict, label: str, key_required: bool) -> dict:
    if key_required and not key:
        raise BackendNotConfigured(f"Set an API key for {label}.")
    timeout = _env_number(ENV_TIMEOUT, DEFAULT_TIMEOUT_SECONDS, float)
    attempts = _env_number(ENV_MAX_RETRIES, DEFAULT_MAX_RETRIES, int) + 1
    last_error: Exception = RuntimeError(f"{label} request never attempted")
    for attempt in range(attempts):
        retry_after = None
        try:
            with llm_slot():
                response = httpx.post(
                    url, timeout=timeout, json=payload,
                    headers={"Authorization": f"Bearer {key}"} if key else {},
                )
        except httpx.HTTPError as error:
            last_error = error
        else:
            if response.status_code < 400:
                return response.json()
            if response.status_code in _NO_RETRY_STATUSES:
                response.raise_for_status()
            retry_after = response.headers.get("retry-after")
            last_error = httpx.HTTPStatusError(
                f"{label} {response.status_code}", request=response.request,
                response=response,
            )
        if attempt < attempts - 1:
            wait = min(30.0, 2.0 ** attempt)
            if retry_after:
                try:
                    wait = min(60.0, float(retry_after))
                except ValueError:
                    pass
            time.sleep(wait)
    raise last_error


def _complete_tool_openrouter(model: str, system: str, user: str, tool: ToolSpec) -> dict:
    data = _openrouter_request({
        "model": model,
        "max_tokens": tool.max_tokens,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": _ask(user, tool)},
        ],
        "tools": [{"type": "function", "function": {
            "name": tool.name,
            "description": f"Report the structured result for {tool.name}.",
            "parameters": tool.input_schema,
        }}],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
    })
    return _parse_chat_tool_reply(model, data, tool)


def _complete_tool_openai(model: str, system: str, user: str, tool: ToolSpec) -> dict:
    data = _openai_request({
        "model": model,
        _openai_token_param(): tool.max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": _ask(user, tool)},
        ],
        "tools": [{"type": "function", "function": {
            "name": tool.name,
            "description": f"Report the structured result for {tool.name}.",
            "parameters": tool.input_schema,
        }}],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
    })
    return _parse_chat_tool_reply(model, data, tool)


def _parse_chat_tool_reply(model: str, data: dict, tool: ToolSpec) -> dict:
    message = data["choices"][0]["message"]
    calls = [(call.get("function") or {}) for call in message.get("tool_calls") or []]
    if len(calls) > 1:
        raise ToolCallMismatch(f"{model} made {len(calls)} tool calls, expected one {tool.name}")
    if calls:
        if calls[0].get("name") != tool.name:
            raise ToolCallMismatch(f"{model} did not call {tool.name}; it called {calls[0].get('name')}")
        arguments = calls[0].get("arguments")
        return arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
    # Some routed models answer with a JSON body instead of the call; salvage the
    # object rather than failing the whole review on formatting. Only when no call
    # came back at all, so a stray call is never papered over by prose.
    content = message.get("content") or ""
    match = _JSON_OBJECT.search(content)
    if match:
        return json.loads(match.group(0))
    raise ToolCallMismatch(f"{model} did not call {tool.name} and returned no JSON object")
