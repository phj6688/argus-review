import asyncio
import copy
import hashlib
import json
import logging
import os
import subprocess
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from argus import cli_providers, client, review
from argus.evalkit import reverify
from argus.schema import Finding


DIFF = "@@ -1 +1 @@\n-old\n+value = 'café'\n"
KEY = "12345678-1234-5678-9012-123456789012"
MODELS = ("gpt-6.1-sol", "claude-sonnet-5-5")
REAL = {"is_real": True, "confidence": 0.9, "reasoning": "The diff confirms it."}
FINDINGS = [
    Finding(file=f"{name}.py", line=1, severity="major", confidence=0.8,
            title=f"Finding {name}", description=f"Description {name}", proposed_fix="Fix it.")
    for name in ("a", "b")
]


def _response(data=REAL, **kwargs):
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name="report_verdict", input=data)],
        stop_reason="tool_use", **kwargs,
    )


@pytest.fixture
def gateway(monkeypatch):
    def install(*replies):
        calls = []
        queue = list(replies)

        def create(**kwargs):
            calls.append(copy.deepcopy(kwargs))
            reply = queue.pop(0) if queue else _response()
            if isinstance(reply, Exception):
                raise reply
            return reply

        monkeypatch.setattr(client.providers, "resolve", lambda model: client.providers.GATEWAY)
        monkeypatch.setattr(client, "build_client", lambda provider: SimpleNamespace(
            messages=SimpleNamespace(create=create)))
        return calls

    return install


def _text(request):
    content = request["messages"][0]["content"]
    return content if isinstance(content, str) else "".join(block["text"] for block in content)


def _bytes(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize("model", MODELS)
def test_sibling_findings_share_the_diff_and_request_prefix(gateway, model):
    calls = gateway()
    variant = review.VERIFIER_VARIANTS["shipped_diff_first"]
    assert variant.system is review.VERIFY_SYSTEM
    assert variant.tool is review.VERDICT_TOOL
    assert variant.verdict_type is review.VERIFIER_VARIANTS["shipped"].verdict_type
    for finding in FINDINGS:
        assert review._verify(finding, DIFF, model, variant="shipped_diff_first").is_real

    prefix = f"# Diff under review\n{DIFF}\n\n"
    assert review._verify_prefix(DIFF) == prefix
    texts = [_text(call) for call in calls]
    assert os.path.commonprefix(texts).startswith(prefix + "# Finding\n")
    assert texts[0][len(prefix):] != texts[1][len(prefix):]
    hashes = []
    for call, finding in zip(calls, FINDINGS):
        user = variant.user_prompt(finding, DIFF)
        assert user == prefix + "# Finding\n" + finding.model_dump_json(
            indent=2, include={"file", "line", "severity", "confidence", "title",
                              "description", "proposed_fix", "rule_id"})
        assert _text(call).encode("utf-8") == client._ask(user, variant.tool).encode("utf-8")
        shared = copy.deepcopy(call)
        content = shared["messages"][0]["content"]
        if isinstance(content, str):
            shared["messages"][0]["content"] = content.split("# Finding\n", 1)[0]
        else:
            content[1]["text"] = content[1]["text"].split("# Finding\n", 1)[0]
        hashes.append(hashlib.sha256(_bytes(shared)).hexdigest())
    assert hashes[0] == hashes[1]


def test_gateway_scope_adds_both_cache_keys(gateway):
    calls = gateway()
    with client.prompt_cache_scope(KEY):
        assert client.complete_tool(MODELS[0], "system", "user", review.VERDICT_TOOL) == REAL
    assert calls[0]["extra_headers"] == {"X-Claude-Code-Session-Id": KEY}
    assert calls[0]["extra_body"] == {"prompt_cache_key": KEY}
    assert isinstance(calls[0]["messages"][0]["content"], str)


def test_claude_scope_marks_only_the_shared_prefix(gateway):
    calls = gateway()
    prefix = f"# Diff under review\n{DIFF}\n\n"
    user = prefix + "# Finding\nA finding"
    with client.prompt_cache_scope(KEY, prefix):
        client.complete_tool(MODELS[1], "system", user, review.VERDICT_TOOL)
    sent = calls[0]
    assert "extra_headers" not in sent and "extra_body" not in sent
    assert "prompt_cache_key" not in sent
    assert sent["messages"][0]["content"] == [
        {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": client._ask(user, review.VERDICT_TOOL)[len(prefix):]},
    ]
    assert _text(sent).encode("utf-8") == client._ask(user, review.VERDICT_TOOL).encode("utf-8")


@pytest.mark.parametrize("prefix,user", [("", "user"), ("diff", "finding then diff"),
                                         ("\n\nAnswer", "")])
def test_claude_without_a_matching_user_prefix_keeps_plain_text(gateway, prefix, user):
    calls = gateway()
    with client.prompt_cache_scope(KEY, prefix):
        client.complete_tool(MODELS[1], "system", user, review.VERDICT_TOOL)
    assert calls[0]["messages"][0]["content"] == client._ask(user, review.VERDICT_TOOL)
    assert "extra_headers" not in calls[0] and "extra_body" not in calls[0]


@pytest.mark.parametrize("model", MODELS)
def test_without_scope_the_entire_request_is_unchanged(gateway, model):
    calls = gateway()
    tool = review.VERDICT_TOOL
    client.complete_tool(model, "system", "user", tool)
    assert calls == [{
        "model": model, "max_tokens": tool.max_tokens, "system": "system",
        "messages": [{"role": "user", "content": client._ask("user", tool)}],
        "tools": [{"name": tool.name, "description": f"Report the structured result for {tool.name}.",
                   "input_schema": tool.input_schema, "strict": True}],
        "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
    }]


@pytest.mark.parametrize("model", MODELS)
def test_tool_mismatch_retry_resends_identical_bytes(gateway, caplog, model):
    calls = gateway(SimpleNamespace(content=None, stop_reason="end_turn"), _response())
    with caplog.at_level(logging.INFO, logger="argus.client"), client.prompt_cache_scope(KEY, "prefix"):
        assert client.complete_tool(model, "system", "prefix finding", review.VERDICT_TOOL) == REAL
    assert len(calls) == 2
    assert _bytes(calls[0]) == _bytes(calls[1])
    usage = [r.getMessage() for r in caplog.records if r.getMessage().startswith("llm usage ")]
    assert usage == [
        f"llm usage model={model} input=0 cache_read=0 cache_write=0 output=0 cache_key={KEY[:8]}"
    ] * 2


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("variant", ["shipped", "shipped_diff_first"])
def test_validation_retry_resends_identical_bytes_and_keeps_scope(gateway, model, variant):
    calls = gateway(_response({}), _response())
    assert review._verify(FINDINGS[0], DIFF, model, variant=variant).is_real
    assert len(calls) == 2
    assert _bytes(calls[0]) == _bytes(calls[1])
    if model.startswith("claude"):
        assert isinstance(calls[0]["messages"][0]["content"], list if variant == "shipped_diff_first" else str)
    else:
        assert calls[0]["extra_body"] == {"prompt_cache_key": review._cache_key(DIFF, model, variant)}
    client.complete_tool(model, "system", "user", review.VERDICT_TOOL)
    assert "extra_headers" not in calls[2] and "extra_body" not in calls[2]
    assert isinstance(calls[2]["messages"][0]["content"], str)


@pytest.mark.parametrize("model", MODELS)
def test_strict_retry_changes_only_strict(gateway, model):
    error = client.BadRequestError("strict unsupported", response=httpx.Response(
        400, request=httpx.Request("POST", "https://example.invalid/messages")), body=None)
    calls = gateway(error, _response())
    with client.prompt_cache_scope(KEY, "prefix"):
        assert client.complete_tool(model, "system", "prefix finding", review.VERDICT_TOOL) == REAL
    assert len(calls) == 2
    first = copy.deepcopy(calls[0])
    assert first["tools"][0].pop("strict") is True
    assert _bytes(first) == _bytes(calls[1])


def test_scope_restores_outer_state_even_after_an_exception(gateway):
    calls = gateway()

    def call():
        client.complete_tool(MODELS[0], "system", "user", review.VERDICT_TOOL)

    with client.prompt_cache_scope("outer"):
        call()
        with pytest.raises(RuntimeError, match="stop"):
            with client.prompt_cache_scope("inner"):
                call()
                raise RuntimeError("stop")
        call()
    call()
    assert [c.get("extra_body", {}).get("prompt_cache_key") for c in calls] == [
        "outer", "inner", "outer", None,
    ]


def test_concurrent_scopes_do_not_mix_keys(gateway):
    calls = gateway()

    async def verify(key):
        with client.prompt_cache_scope(key):
            await asyncio.sleep(0)
            client.complete_tool(MODELS[0], "system", key, review.VERDICT_TOOL)

    async def run():
        await asyncio.gather(verify("first"), verify("second"))

    asyncio.run(run())
    assert len(calls) == 2
    for call in calls:
        key = call["extra_body"]["prompt_cache_key"]
        assert call["messages"][0]["content"] == client._ask(key, review.VERDICT_TOOL)


@pytest.mark.parametrize("provider", [client.providers.OPENROUTER, client.providers.OPENAI])
def test_chat_payloads_are_unchanged_by_scope(monkeypatch, provider):
    calls = []

    def request(payload):
        calls.append(copy.deepcopy(payload))
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "report_verdict", "arguments": json.dumps(REAL),
        }}]}}]}

    monkeypatch.setattr(client.providers, "resolve", lambda model: provider)
    monkeypatch.setattr(client, f"_{provider}_request", request)
    for scope in (nullcontext(), client.prompt_cache_scope(KEY, "user")):
        with scope:
            assert client.complete_tool("model", "system", "user", review.VERDICT_TOOL) == REAL
    assert len(calls) == 2
    assert _bytes(calls[0]) == _bytes(calls[1])


@pytest.mark.parametrize("provider", [client.providers.CLAUDE_CLI, client.providers.CODEX_CLI])
def test_cli_payloads_are_unchanged_by_scope(monkeypatch, tmp_path, provider):
    calls = []
    work, files = tmp_path / "work", tmp_path / "files"
    work.mkdir()
    files.mkdir()

    @contextmanager
    def workspace():
        yield work, files

    def run(name, argv, cwd, prompt, timeout):
        payload = {"name": name, "argv": argv, "cwd": str(cwd), "prompt": prompt, "timeout": timeout}
        if provider == client.providers.CLAUDE_CLI:
            payload["system"] = (files / "system.txt").read_text(encoding="utf-8")
            stdout = json.dumps({"structured_output": REAL})
        else:
            payload["schema"] = (files / "schema.json").read_text(encoding="utf-8")
            Path(argv[argv.index("-o") + 1]).write_text(json.dumps(REAL), encoding="utf-8")
            stdout = ""
        calls.append(copy.deepcopy(payload))
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(client.providers, "resolve", lambda model: provider)
    monkeypatch.setattr(cli_providers, "_workspace", workspace)
    monkeypatch.setattr(cli_providers, "_binary", lambda name: "executable")
    monkeypatch.setattr(cli_providers, "_run", run)
    for scope in (nullcontext(), client.prompt_cache_scope(KEY, "user")):
        with scope:
            assert client.complete_tool("model", "system", "user", review.VERDICT_TOOL) == REAL
    assert len(calls) == 2
    assert _bytes(calls[0]) == _bytes(calls[1])


def test_cache_keys_are_stable_and_separate_diffs_models_and_variants(gateway):
    calls = gateway()
    inputs = [(DIFF, MODELS[0], "shipped_diff_first")] * 2 + [
        (DIFF + "\n", MODELS[0], "shipped_diff_first"),
        (DIFF, "gpt-other", "shipped_diff_first"),
        (DIFF, MODELS[0], "shipped"),
    ]
    for index, (diff, model, variant) in enumerate(inputs):
        review._verify(FINDINGS[index % 2], diff, model, variant=variant)
    keys = [call["extra_body"]["prompt_cache_key"] for call in calls]
    assert keys[0] == "c69291c0-3590-5f4b-b11d-270cbea862a4"
    assert keys[0] == keys[1]
    assert len(set(keys)) == 4
    assert keys == [review._cache_key(*args) for args in inputs]


def test_diff_first_variant_has_a_distinct_reverify_hash():
    assert reverify._variant_sha256("shipped_diff_first") != reverify._variant_sha256("shipped")


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("usage,expected", [
    ({}, (0, 0, 0, 0)),
    ({"usage": None}, (0, 0, 0, 0)),
    ({"usage": SimpleNamespace(input_tokens=12, output_tokens=3)}, (12, 0, 0, 3)),
    ({"usage": SimpleNamespace(input_tokens=None, cache_read_input_tokens=None,
                               cache_creation_input_tokens=None, output_tokens=None)}, (0, 0, 0, 0)),
    ({"usage": SimpleNamespace(input_tokens=12, cache_read_input_tokens=8,
                               cache_creation_input_tokens=4, output_tokens=3)}, (12, 8, 4, 3)),
])
def test_usage_is_logged_with_missing_values_as_zero(gateway, caplog, scoped, usage, expected):
    gateway(_response(**usage))
    with caplog.at_level(logging.INFO, logger="argus.client"):
        with client.prompt_cache_scope(KEY) if scoped else nullcontext():
            client.complete_tool(MODELS[0], "system", "user", review.VERDICT_TOOL)
    messages = [r.getMessage() for r in caplog.records if r.name == "argus.client"]
    input_tokens, cache_read, cache_write, output_tokens = expected
    assert messages == [
        f"llm usage model={MODELS[0]} input={input_tokens} cache_read={cache_read} "
        f"cache_write={cache_write} output={output_tokens} cache_key={KEY[:8] if scoped else '-'}"
    ]
