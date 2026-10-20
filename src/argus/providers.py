"""Provider selection, model defaults, and structured-output schemas."""

import copy
import os
from collections.abc import Mapping

ENV_PROVIDER = "ARGUS_PROVIDER"
GATEWAY = "gateway"
ANTHROPIC = "anthropic"
OPENROUTER = "openrouter"
OPENAI = "openai"
CLAUDE_CLI = "claude-cli"
CODEX_CLI = "codex-cli"
ALL_PROVIDERS = (GATEWAY, ANTHROPIC, OPENROUTER, OPENAI, CLAUDE_CLI, CODEX_CLI)
AUTO = "auto"

LEGACY_DEFAULT_MODEL = "gpt-6.1-sol"
PROVIDER_DEFAULT_MODELS = {
    GATEWAY: LEGACY_DEFAULT_MODEL,
    ANTHROPIC: "claude-sonnet-5-5",
    OPENROUTER: "openai/gpt-6.1-sol",
    OPENAI: LEGACY_DEFAULT_MODEL,
    CLAUDE_CLI: "claude-sonnet-5-5",
    CODEX_CLI: LEGACY_DEFAULT_MODEL,
}


class BackendNotConfigured(RuntimeError):
    pass


def explicit_provider(env: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    provider = env.get(ENV_PROVIDER, "").strip().lower()
    if provider in ("", AUTO):
        return None
    if provider not in ALL_PROVIDERS:
        valid = ", ".join((AUTO, *ALL_PROVIDERS))
        raise BackendNotConfigured(f"Unknown {ENV_PROVIDER}={provider!r}; choose from {valid}.")
    return provider


def auto_provider(env: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    if env.get("ANTHROPIC_BASE_URL", "").strip() and env.get("ANTHROPIC_AUTH_TOKEN", "").strip():
        return GATEWAY
    if env.get("ANTHROPIC_API_KEY", "").strip():
        return ANTHROPIC
    if env.get("OPENAI_API_KEY", "").strip() or env.get("OPENAI_BASE_URL", "").strip():
        return OPENAI
    if env.get("OPENROUTER_API_KEY", "").strip():
        return OPENROUTER
    return None


def not_configured_message() -> str:
    return (
        "No model provider configured. Set ANTHROPIC_BASE_URL and ANTHROPIC_AUTH_TOKEN "
        "for gateway, ANTHROPIC_API_KEY for anthropic, OPENROUTER_API_KEY for openrouter, "
        "or OPENAI_API_KEY or OPENAI_BASE_URL for openai. "
        "For subscription access, set ARGUS_PROVIDER=claude-cli (claude login or "
        "CLAUDE_CODE_OAUTH_TOKEN, optional ARGUS_CLAUDE_BIN and ARGUS_CLAUDE_CONFIG_DIR) or "
        "ARGUS_PROVIDER=codex-cli (CODEX_HOME=<dir> codex login, then set ARGUS_CODEX_HOME=<dir>; "
        "optional ARGUS_CODEX_BIN)."
    )


def resolve(model: str, env: Mapping[str, str] | None = None) -> str:
    provider = explicit_provider(env)
    if provider == OPENAI:
        return OPENAI
    if "/" in model:
        return OPENROUTER
    if provider is not None:
        return provider
    provider = auto_provider(env)
    if provider == OPENROUTER:
        raise BackendNotConfigured(
            "only OPENROUTER_API_KEY is set; use a vendor/model id such as openai/gpt-6.1-sol"
        )
    if provider is None:
        raise BackendNotConfigured(not_configured_message())
    return provider


def default_model(env: Mapping[str, str] | None = None) -> str:
    try:
        provider = explicit_provider(env) or auto_provider(env)
    except BackendNotConfigured:
        return LEGACY_DEFAULT_MODEL
    return PROVIDER_DEFAULT_MODELS.get(provider, LEGACY_DEFAULT_MODEL)


def _nullable(schema: dict) -> dict:
    if any(key in schema for key in ("$ref", "oneOf", "allOf", "const")):
        return {"anyOf": [schema, {"type": "null"}]}
    kind = schema.get("type")
    if isinstance(kind, str) and kind != "null":
        schema["type"] = [kind, "null"]
    elif isinstance(kind, list) and "null" not in kind:
        kind.append("null")
    if "anyOf" in schema and {"type": "null"} not in schema["anyOf"]:
        schema["anyOf"].append({"type": "null"})
    if "enum" in schema and None not in schema["enum"]:
        schema["enum"].append(None)
    if not any(key in schema for key in ("type", "anyOf", "enum")):
        return {"anyOf": [schema, {"type": "null"}]}
    return schema


def strict_schema(schema: dict) -> dict:
    """Close objects and represent optional properties as required nullable values."""
    result = copy.deepcopy(schema)

    def visit(node):
        if not isinstance(node, dict):
            return
        for key in ("properties", "$defs", "definitions"):
            for child in node.get(key, {}).values():
                visit(child)
        items = node.get("items")
        if isinstance(items, list):
            for child in items:
                visit(child)
        else:
            visit(items)
        for key in ("anyOf", "oneOf", "allOf"):
            for child in node.get(key, []):
                visit(child)
        kind = node.get("type")
        if kind == "object" or (isinstance(kind, list) and "object" in kind) or "properties" in node:
            properties = node.get("properties", {})
            required = node.get("required", [])
            for name, child in properties.items():
                if name not in required:
                    properties[name] = _nullable(child)
            node["required"] = list(properties)
            node["additionalProperties"] = False

    visit(result)
    return result


def drop_nulls(value, schema):
    """Restore optional fields using the original schema, preserving required nulls."""
    def expand(node, refs=frozenset()):
        if not isinstance(node, dict):
            return [None]
        nodes = [node]
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/") or ref in refs:
                return [None]
            target = schema
            for part in ref[2:].split("/"):
                key = part.replace("~1", "/").replace("~0", "~")
                if not isinstance(target, dict) or key not in target:
                    return [None]
                target = target[key]
            nodes.extend(expand(target, refs | {ref}))
        for keyword in ("anyOf", "oneOf", "allOf"):
            for branch in node.get(keyword, []):
                nodes.extend(expand(branch, refs))
        return nodes

    def visit(item, nodes):
        if not nodes or any(node is None for node in nodes):
            return item
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                definitions = [node["properties"][key] for node in nodes
                               if key in node.get("properties", {})]
                children = [branch for definition in definitions for branch in expand(definition)]
                # A union cannot lose a field required by any of its branches.
                required = any(key in node.get("required", []) for node in nodes)
                if (child is None and children and not required
                        and all(node is not None for node in children)):
                    continue
                result[key] = visit(child, children)
            return result
        if isinstance(item, list):
            result = []
            for index, child in enumerate(item):
                children = []
                for node in nodes:
                    items = node.get("items")
                    if isinstance(items, dict):
                        children.extend(expand(items))
                    elif isinstance(items, list) and index < len(items):
                        children.extend(expand(items[index]))
                result.append(visit(child, children))
            return result
        return item

    return visit(value, expand(schema))
