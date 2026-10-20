"""Check provider configuration before making one small model call."""

import os

import httpx
from anthropic import AuthenticationError

from . import client, providers

_PING_TOOL = client.ToolSpec(
    name="ping",
    input_schema={
        "type": "object",
        "properties": {"reply": {"type": "string"}},
        "required": ["reply"],
        "additionalProperties": False,
    },
    max_tokens=64,
)


def _error_detail(error: Exception) -> str:
    if isinstance(error, AuthenticationError):
        return "bad key"
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403):
        return "bad key"
    return f"{type(error).__name__}: {error}"


def _credential_check(provider: str) -> tuple[str, bool, str] | None:
    required = {
        providers.GATEWAY: (client.ENV_BASE_URL, client.ENV_AUTH_TOKEN),
        providers.ANTHROPIC: (client.ENV_ANTHROPIC_KEY,),
        providers.OPENROUTER: (client.ENV_OPENROUTER_KEY,),
    }.get(provider, ())
    if not required:
        return None
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    detail = f"{', '.join(missing)} not set" if missing else f"{', '.join(required)} set"
    return ("credentials", not missing, detail)


def configuration_problem(model: str | None = None) -> str | None:
    """Why a review on this model cannot start, or None when it can.

    Offline: it reads the environment only and makes no network call. The text is
    what `argus doctor` prints for the same failure, so the console, the API and
    the CLI all give one answer.
    """
    model = model or client.DEFAULT_MODEL
    try:
        provider = providers.resolve(model)
    except providers.BackendNotConfigured as error:
        return str(error)
    credentials = _credential_check(provider)
    if credentials is not None and not credentials[1]:
        return credentials[2]
    return None


def run_checks(model: str | None = None) -> list[tuple[str, bool, str]]:
    model = model or client.DEFAULT_MODEL
    try:
        provider = providers.resolve(model)
    except providers.BackendNotConfigured as error:
        return [("provider", False, str(error))]

    if provider in (providers.CLAUDE_CLI, providers.CODEX_CLI):
        from . import cli_providers
        return cli_providers.get(provider).doctor(model)

    checks = []
    credentials = _credential_check(provider)
    if credentials is not None:
        checks.append(credentials)
        if not credentials[1]:
            return checks
    if provider == providers.ANTHROPIC:
        if not os.environ[client.ENV_ANTHROPIC_KEY].strip().startswith("sk-ant-"):
            checks.append(("key format", True, "warning: ANTHROPIC_API_KEY does not start with sk-ant-"))

    if provider in (providers.OPENROUTER, providers.OPENAI):
        if provider == providers.OPENROUTER:
            url = "https://openrouter.ai/api/v1/models"
            key = os.environ[client.ENV_OPENROUTER_KEY].strip()
        else:
            base = (os.environ.get(client.ENV_OPENAI_BASE_URL) or "").strip() or client.DEFAULT_OPENAI_BASE_URL
            url = base.rstrip("/") + "/models"
            key = os.environ.get(client.ENV_OPENAI_KEY, "").strip()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            response = httpx.get(url, headers=headers, timeout=httpx.Timeout(10.0, connect=5.0))
            response.raise_for_status()
            if provider == providers.OPENROUTER:
                models = response.json()["data"]
                entry = next((entry for entry in models if entry.get("id") == model), None)
                if entry is None:
                    warning = model.startswith("~")
                    detail = f"{model} not found in the model catalog"
                    checks.append(("model", warning, f"warning: {detail}" if warning else detail))
                    if not warning:
                        return checks
                elif "tools" not in (entry.get("supported_parameters") or []):
                    checks.append(("model", False, f"{model} does not support tools"))
                    return checks
                else:
                    checks.append(("model", True, f"{model} supports tools"))
            else:
                checks.append(("models", True, f"{url} reachable"))
        except Exception as error:
            checks.append(("models", False, _error_detail(error)))
            return checks

    try:
        if provider == providers.OPENAI:
            result = client.complete_tool(
                model, "Return a structured reply.", "Reply with the single word: ok", _PING_TOOL,
            )
            reply = result.get("reply")
            if not isinstance(reply, str):
                raise client.ToolCallMismatch("ping did not return a reply string")
        else:
            reply = client.ping(model)
        checks.append(("ping", True, reply))
    except Exception as error:
        checks.append(("ping", False, _error_detail(error)))
    return checks
