"""Provider routing and schema conversion without host credentials."""

import copy
import itertools

import pytest

from argus import providers
from argus.review import REVIEW_TOOL


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "ARGUS_PROVIDER", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENROUTER_API_KEY", "ARGUS_OPENAI_TOKEN_PARAM",
        "ARGUS_CLI_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("env, expected", [
    ({"ANTHROPIC_BASE_URL": "https://gateway.test", "ANTHROPIC_AUTH_TOKEN": "token"}, "gateway"),
    ({"ANTHROPIC_API_KEY": "key"}, "anthropic"),
    ({"ANTHROPIC_BASE_URL": "https://api.test", "ANTHROPIC_API_KEY": "key"}, "anthropic"),
    ({"OPENAI_API_KEY": "key"}, "openai"),
    ({"OPENAI_BASE_URL": "http://localhost:11434/v1"}, "openai"),
])
def test_auto_selection(env, expected):
    assert providers.resolve("model", env) == expected


def test_auto_precedence():
    env = {name: "set" for name in (
        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY", "OPENROUTER_API_KEY",
    )}
    assert providers.resolve("model", env) == "gateway"
    del env["ANTHROPIC_AUTH_TOKEN"]
    assert providers.resolve("model", env) == "anthropic"
    del env["ANTHROPIC_API_KEY"]
    assert providers.resolve("model", env) == "openai"


def test_openrouter_only_requires_vendor_model():
    env = {"OPENROUTER_API_KEY": "key"}
    assert providers.resolve("openai/gpt-6.1-sol", env) == "openrouter"
    with pytest.raises(providers.BackendNotConfigured, match="use a vendor/model"):
        providers.resolve("gpt-6.1-sol", env)


def test_unconfigured_error_lists_all_options():
    with pytest.raises(providers.BackendNotConfigured) as caught:
        providers.resolve("model", {})
    message = str(caught.value)
    for option in (
        "gateway", "anthropic", "openrouter", "openai", "claude-cli", "codex-cli",
        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENROUTER_API_KEY", "ARGUS_PROVIDER",
    ):
        assert option in message


AUTO_KEYS = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "OPENAI_BASE_URL", "OPENROUTER_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_HOME",
)


@pytest.mark.parametrize("present", list(itertools.product((False, True), repeat=len(AUTO_KEYS))))
def test_auto_never_selects_a_subscription(present):
    env = {name: "set" for name, enabled in zip(AUTO_KEYS, present) if enabled}
    assert providers.auto_provider(env) not in ("claude-cli", "codex-cli")
    try:
        selected = providers.resolve("model", env)
    except providers.BackendNotConfigured:
        return
    assert selected not in ("claude-cli", "codex-cli")


@pytest.mark.parametrize("provider", ["gateway", "anthropic", "claude-cli", "codex-cli", "openrouter"])
def test_explicit_provider_for_bare_model_needs_no_credential_to_resolve(provider):
    assert providers.resolve("model", {"ARGUS_PROVIDER": provider}) == provider


@pytest.mark.parametrize("provider", ["gateway", "anthropic", "claude-cli", "codex-cli", "openrouter"])
def test_slash_models_keep_openrouter_routing(provider):
    assert providers.resolve("vendor/model", {"ARGUS_PROVIDER": provider}) == "openrouter"


@pytest.mark.parametrize("model", ["model", "vendor/model"])
def test_explicit_openai_owns_every_model(model):
    assert providers.resolve(model, {"ARGUS_PROVIDER": " OPENAI "}) == "openai"


@pytest.mark.parametrize("setting", ["auto", "", "  ", " AUTO "])
def test_auto_and_empty_provider_use_credentials(setting):
    assert providers.resolve("model", {"ARGUS_PROVIDER": setting, "OPENAI_API_KEY": "k"}) == "openai"


@pytest.mark.parametrize("model", ["model", "vendor/model"])
def test_unknown_provider_fails_even_with_a_slash_model(model):
    with pytest.raises(providers.BackendNotConfigured, match="ARGUS_PROVIDER") as caught:
        providers.resolve(model, {"ARGUS_PROVIDER": "typo"})
    for value in ("auto", *providers.ALL_PROVIDERS):
        assert value in str(caught.value)


def test_whitespace_credentials_are_absent():
    assert providers.auto_provider(dict.fromkeys(AUTO_KEYS, " \t")) is None
    with pytest.raises(providers.BackendNotConfigured):
        providers.resolve("model", dict.fromkeys(AUTO_KEYS, " \t"))


@pytest.mark.parametrize("provider, model", [
    ("gateway", "gpt-6.1-sol"), ("anthropic", "claude-sonnet-5-5"),
    ("openrouter", "openai/gpt-6.1-sol"), ("openai", "gpt-6.1-sol"),
    ("claude-cli", "claude-sonnet-5-5"), ("codex-cli", "gpt-6.1-sol"),
])
def test_explicit_default_model(provider, model):
    assert providers.default_model({"ARGUS_PROVIDER": provider}) == model


@pytest.mark.parametrize("env, model", [
    ({}, "gpt-6.1-sol"),
    ({"ANTHROPIC_BASE_URL": "url", "ANTHROPIC_AUTH_TOKEN": "key"}, "gpt-6.1-sol"),
    ({"ANTHROPIC_API_KEY": "key"}, "claude-sonnet-5-5"),
    ({"OPENROUTER_API_KEY": "key"}, "openai/gpt-6.1-sol"),
    ({"OPENAI_API_KEY": "key"}, "gpt-6.1-sol"),
    ({"OPENAI_BASE_URL": "url"}, "gpt-6.1-sol"),
    ({"ARGUS_PROVIDER": "typo", "ANTHROPIC_API_KEY": "key"}, "gpt-6.1-sol"),
])
def test_auto_and_unconfigured_default_model(env, model):
    assert providers.default_model(env) == model


def _assert_closed_objects(node):
    if isinstance(node, dict):
        if "properties" in node or node.get("type") == "object":
            assert node["required"] == list(node.get("properties", {}))
            assert node["additionalProperties"] is False
        for child in node.values():
            _assert_closed_objects(child)
    elif isinstance(node, list):
        for child in node:
            _assert_closed_objects(child)


def test_strict_review_schema_preserves_original_and_makes_rule_id_nullable():
    original = copy.deepcopy(REVIEW_TOOL.input_schema)
    strict = providers.strict_schema(REVIEW_TOOL.input_schema)
    _assert_closed_objects(strict)
    finding = strict["properties"]["findings"]["items"]
    assert finding["properties"]["rule_id"]["type"] == ["string", "null"]
    assert REVIEW_TOOL.input_schema == original
    assert "rule_id" not in original["properties"]["findings"]["items"]["required"]


@pytest.mark.parametrize("container", ["anyOf", "oneOf", "allOf", "$defs", "definitions", "items"])
def test_strict_schema_visits_nested_schema_forms(container):
    obj = {"type": "object", "properties": {"value": {"type": "string"}}}
    child = {"entry": obj} if container in ("$defs", "definitions") else [obj]
    strict = providers.strict_schema({container: child})
    _assert_closed_objects(strict)


@pytest.mark.parametrize("schema, nullable", [
    ({"type": "string"}, {"type": ["string", "null"]}),
    ({"type": ["string", "number"]}, {"type": ["string", "number", "null"]}),
    ({"anyOf": [{"type": "string"}]}, {"anyOf": [{"type": "string"}, {"type": "null"}]}),
    ({"$ref": "#/$defs/value"}, {"anyOf": [{"$ref": "#/$defs/value"}, {"type": "null"}]}),
    ({"type": "string", "enum": ["yes"]}, {"type": ["string", "null"], "enum": ["yes", None]}),
])
def test_optional_schema_forms_become_nullable(schema, nullable):
    result = providers.strict_schema({"type": "object", "properties": {"value": schema}})
    assert result["properties"]["value"] == nullable


def test_drop_nulls_removes_dictionary_keys_and_preserves_array_positions():
    value = {"absent": None, "nested": {"absent": None, "zero": 0},
             "items": [None, {"absent": None, "keep": False}, "last"]}
    original = copy.deepcopy(value)
    obj = {"type": "object", "properties": {"absent": {"type": "string"}}}
    schema = {"type": "object", "properties": {
        "absent": {"type": "string"}, "nested": obj, "items": {"type": "array", "items": obj},
    }}
    assert providers.drop_nulls(value, schema) == {
        "nested": {"zero": 0}, "items": [None, {"keep": False}, "last"],
    }
    assert value == original


@pytest.mark.parametrize("union", ["anyOf", "oneOf", "allOf"])
def test_drop_nulls_keeps_fields_required_by_any_defining_branch(union):
    schema = {union: [
        {"type": "object", "properties": {
            "shared": {"type": ["string", "null"]}, "optional": {"type": "string"},
            "one_branch": {"type": "string"},
        }},
        {"type": "object", "properties": {
            "shared": {"type": ["string", "null"]}, "optional": {"type": "string"},
        }, "required": ["shared"]},
        {"type": "null"},
    ]}
    value = {"shared": None, "optional": None, "one_branch": None, "unknown": None}
    assert providers.drop_nulls(value, schema) == {"shared": None, "unknown": None}


@pytest.mark.parametrize("container", ["$defs", "definitions"])
def test_drop_nulls_resolves_local_refs_and_preserves_required_nullable_fields(container):
    obj = {"type": "object", "properties": {
        "required": {"type": ["integer", "null"]}, "optional": {"type": "string"},
    }, "required": ["required"]}
    schema = {
        container: {"entry/name~": obj}, "type": "array",
        "items": {"$ref": f"#/{container}/entry~1name~0"},
    }
    original = copy.deepcopy(schema)
    assert providers.drop_nulls([{"required": None, "optional": None}], schema) == [
        {"required": None},
    ]
    assert schema == original


def test_drop_nulls_follows_tuple_items_and_nested_branch_properties():
    schema = {"type": "array", "items": [
        {"anyOf": [
            {"properties": {"nested": {"properties": {"keep": {"type": "null"}}},
                            "absent": {"type": "string"}}},
            {"properties": {"nested": {"properties": {"keep": {"type": "null"}},
                                       "required": ["keep"]}}},
        ]},
        {"properties": {"absent": {"type": "string"}}},
    ]}
    value = [{"nested": {"keep": None}, "absent": None}, {"absent": None}, {"unknown": None}]
    assert providers.drop_nulls(value, schema) == [{"nested": {"keep": None}}, {}, {"unknown": None}]


@pytest.mark.parametrize("schema", [
    {}, None, True, {"$ref": "#/$defs/missing"}, {"$ref": "https://example.test/schema"},
    {"$ref": "#/$defs/loop", "$defs": {"loop": {"$ref": "#/$defs/loop"}}},
])
def test_drop_nulls_leaves_unknown_or_unresolvable_schema_untouched(schema):
    value = {"unknown": None, "nested": [{"unknown": None}]}
    assert providers.drop_nulls(value, schema) == value


def test_drop_nulls_follows_recursive_refs_with_finite_values():
    schema = {"$defs": {"node": {"properties": {
        "keep": {"type": "null"}, "absent": {"type": "string"},
        "child": {"$ref": "#/$defs/node"},
    }, "required": ["keep"]}}, "$ref": "#/$defs/node"}
    value = {"keep": None, "absent": None, "child": {"keep": None, "absent": None}}
    assert providers.drop_nulls(value, schema) == {"keep": None, "child": {"keep": None}}
