"""Verifier variants behind ARGUS_VERIFY_VARIANT.

The shipped verifier runs on every live review, so the default must reach the
model byte for byte as it did before the variants existed. The golden fixture
was captured from origin/dev (186db67) before this change: every system prompt,
tool schema, user payload and parsed verdict the verifier produced there.
"""

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from argus import review
from argus.diff import FilePatch
from argus.evalkit.replay import serialize_finding
from argus.jarvis import live
from argus.schema import CounterEvidence, Finding, Review, VerifiedFinding

FIXTURES = Path(__file__).with_name("fixtures")
GOLDEN = json.loads(FIXTURES.joinpath("verify_shipped_golden.json").read_text(encoding="utf-8"))
REPO_ROOT = Path(__file__).resolve().parent.parent
VARIANT_ENV = "ARGUS_VERIFY_VARIANT"
VARIANTS = ("shipped", "shipped_diff_first", "default_keep", "reasoning_first", "evidence",
            "default_keep_checklists")
PROTECTED_CLASSES = ("error paths", "behavior changes", "concurrency", "boundaries",
                     "auth and middleware order", "silent fallbacks", "missing timeouts")

FINDING = Finding(
    file="src/app/handler.py", line=42, end_line=44, severity="major", confidence=0.8,
    title="Timeout missing", description="requests.get has no timeout.",
    proposed_fix="Pass timeout=10.", rule_id=None, category="resources",
    evidence="resp = requests.get(url)",
)
DIFF = "## File: src/app/handler.py (python)\n```diff\n@@ -40 +40 @@\n+resp = requests.get(url)\n```"
REAL = {"is_real": True, "confidence": 0.9, "reasoning": "r"}
QUOTE = {"path": "src/app/handler.py", "start_line": 40, "end_line": 40,
         "quote": "resp = requests.get(url, timeout=10)"}


def _record(args):
    model, system, user, tool = args
    return {"model": model, "system": system, "user": user, "tool_name": tool.name,
            "input_schema": tool.input_schema, "max_tokens": tool.max_tokens}


def _scripted(monkeypatch, replies):
    calls = []
    queue = list(replies)

    def fake(*args):
        calls.append(_record(args))
        return queue.pop(0)

    monkeypatch.setattr(review, "complete_tool", fake)
    return calls


def _ordered(value):
    """JSON text keeps key order, so equality on it is order-sensitive."""
    return json.dumps(value, ensure_ascii=False)


def _quote_at_head(path):
    """A reader whose file holds QUOTE at the line the quote cites."""
    return "\n" * (QUOTE["start_line"] - 1) + QUOTE["quote"] + "\n" if path == QUOTE["path"] else None


def _verify_with(monkeypatch, variant, reply=REAL, finding=FINDING, read=None):
    calls = _scripted(monkeypatch, [reply])
    verdict = review._verify(finding, DIFF, "model-x", variant=variant, read=read)
    assert len(calls) == 1
    return calls[0], verdict


def _payload(user):
    return json.loads(user.removeprefix("# Finding\n").split("\n\n# Diff under review\n")[0])


def _reload_with(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(VARIANT_ENV, raising=False)
    else:
        monkeypatch.setenv(VARIANT_ENV, value)
    importlib.reload(review)


@pytest.fixture
def restore_review_module(monkeypatch):
    """Put the environment back as the suite found it, then reload so the module
    setting matches it again."""
    yield
    monkeypatch.undo()
    importlib.reload(review)


@pytest.fixture
def unset_variant(monkeypatch, restore_review_module):
    """The module as it loads with the setting unset, whatever the ambient env holds."""
    _reload_with(monkeypatch, None)
    assert review.VERIFY_VARIANT == "shipped", "an unset setting must load as shipped"


def _stub_diff_lanes(monkeypatch):
    probe = {"lane": "probe", "status": "ran", "checks": 1}
    monkeypatch.setattr(live, "probe_scan_for_files", lambda *args: live.ProbeScan([], probe))
    monkeypatch.setattr(review, "REVIEW_PASSES", 1)


FILES = [FilePatch("src/app/handler.py", "modified", "@@ -40 +40 @@\n+resp = requests.get(url)\n")]


def _reviewer_then(verdict_reply):
    """A complete_tool stand-in: one reviewer finding, then the given verifier reply."""
    calls = []

    def fake(model, system, user, tool):
        if tool.name == "report_findings":
            return {"findings": [FINDING.model_dump()]}
        calls.append(_record((model, system, user, tool)))
        return dict(verdict_reply)

    return calls, fake


# --- R1: the default is byte-identical to dev, an unknown value fails at load ---

@pytest.mark.parametrize("case", GOLDEN["direct"], ids=[c["name"] for c in GOLDEN["direct"]])
@pytest.mark.parametrize("explicit", [False, True], ids=["default", "explicit-shipped"])
def test_shipped_verifier_matches_dev_byte_for_byte(monkeypatch, unset_variant, case, explicit):
    calls = _scripted(monkeypatch, case["replies"])
    finding = Finding(**case["finding_input"])
    if explicit:
        verdict = review._verify(finding, case["diff_text"], "model-x", variant="shipped")
    else:
        verdict = review._verify(finding, case["diff_text"], "model-x")
    assert _ordered(calls) == _ordered(case["calls"])
    assert type(verdict).__name__ == case["verdict_type"]
    assert verdict.model_dump_json() == case["verdict_json"]
    assert _ordered(verdict.model_dump(mode="json")) == _ordered(case["verdict"])


def test_shipped_review_path_matches_dev_byte_for_byte(monkeypatch, unset_variant):
    golden = GOLDEN["run_diff_review"]
    probe = {"lane": "probe", "status": "ran", "checks": 1}
    monkeypatch.setattr(live, "probe_scan_for_files", lambda *args: live.ProbeScan([], probe))
    monkeypatch.setattr(review, "REVIEW_PASSES", 1)
    calls = []
    verdicts = iter(golden["verdict_replies"])

    def fake(*args):
        record = _record(args)
        if record["tool_name"] == "report_findings":
            return golden["reviewer_reply"]
        calls.append(record)
        return next(verdicts)

    monkeypatch.setattr(review, "complete_tool", fake)
    files = [FilePatch(f["filename"], f["status"], f["patch"]) for f in golden["files"]]
    result = review.run_diff_review(files, model="model-x")
    assert _ordered(calls) == _ordered(golden["verifier_calls"])
    assert _ordered([i.verdict.model_dump(mode="json") for i in result.all_findings]) == \
        _ordered(golden["verdicts"])
    assert _ordered([serialize_finding(i) for i in result.all_findings]) == \
        _ordered(golden["serialized"])
    llm = dict(next(r for r in result.coverage if r["lane"] == "llm"))
    assert llm.pop("verify_variant") == "shipped"
    assert _ordered(llm) == _ordered(golden["llm_record"]), "the variant is the only added key"


def test_shipped_prompt_and_schema_are_the_dev_constants():
    shipped = review.VERIFIER_VARIANTS["shipped"]
    golden_call = GOLDEN["direct"][0]["calls"][0]
    assert shipped.system is review.VERIFY_SYSTEM
    assert shipped.tool is review.VERDICT_TOOL
    assert review.VERIFY_SYSTEM == golden_call["system"]
    assert _ordered(review.VERDICT_TOOL.input_schema) == _ordered(golden_call["input_schema"])


@pytest.mark.parametrize("value", ["bogus", "Shipped", "default-keep", "evidence,shipped"])
def test_unknown_variant_fails_when_review_loads(value):
    env = {**os.environ, VARIANT_ENV: value}
    done = subprocess.run([sys.executable, "-c", "import argus.review"], env=env,
                          capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode != 0
    assert "ValueError" in done.stderr
    assert VARIANT_ENV in done.stderr and repr(value) in done.stderr


@pytest.mark.parametrize("module", ["argus.api.main", "argus.api.watcher", "argus.api.db",
                                    "argus.cli", "argus.jarvis.cli", "argus.evalkit.cron",
                                    "argus.evalkit.replay", "argus.evalkit.weekly"])
def test_service_entry_points_fail_on_an_unknown_variant(module):
    env = {**os.environ, VARIANT_ENV: "bogus"}
    done = subprocess.run([sys.executable, "-c", f"import {module}"], env=env,
                          capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode != 0
    assert VARIANT_ENV in done.stderr


@pytest.mark.parametrize("value,want", [(None, "shipped"), ("", "shipped"), ("  ", "shipped"),
                                        *[(name, name) for name in VARIANTS]])
def test_setting_is_read_once_at_load(monkeypatch, restore_review_module, value, want):
    _reload_with(monkeypatch, value)
    assert review.VERIFY_VARIANT == want
    assert review._verify.__kwdefaults__ == {"variant": None, "read": None}
    calls = _scripted(monkeypatch, [{**REAL, "counter_evidence": None}])
    review._verify(FINDING, DIFF, "model-x")
    assert calls[0]["system"] == review.VERIFIER_VARIANTS[want].system
    assert _ordered(calls[0]["input_schema"]) == \
        _ordered(review.VERIFIER_VARIANTS[want].tool.input_schema)


def test_the_module_setting_is_resolved_when_verify_is_called(monkeypatch):
    calls = _scripted(monkeypatch, [{**REAL, "counter_evidence": None}])
    monkeypatch.setattr(review, "VERIFY_VARIANT", "default_keep")
    review._verify(FINDING, DIFF, "model-x")
    assert calls[0]["system"] == review.VERIFY_SYSTEM_DEFAULT_KEEP


def test_unknown_variant_name_passed_to_verify_raises(monkeypatch):
    calls = _scripted(monkeypatch, [REAL])
    with pytest.raises(ValueError, match="no_such_variant"):
        review._verify(FINDING, DIFF, "model-x", variant="no_such_variant")
    assert calls == []


@pytest.mark.parametrize("variant", VARIANTS)
def test_every_variant_tool_is_strict_compatible(variant):
    def closed(schema):
        if isinstance(schema, dict):
            kind = schema.get("type")
            if kind == "object" or (isinstance(kind, list) and "object" in kind):
                assert schema.get("additionalProperties") is False
                assert set(schema["required"]) == set(schema["properties"])
            for value in schema.values():
                closed(value)
        elif isinstance(schema, list):
            for value in schema:
                closed(value)

    tool = review.VERIFIER_VARIANTS[variant].tool
    closed(tool.input_schema)
    # The gateway's strict lane is unproven with anyOf, and a 400 there is not retried.
    assert "anyOf" not in json.dumps(tool.input_schema)
    assert tool.name == review.VERDICT_TOOL.name
    assert tool.max_tokens == review.VERDICT_TOOL.max_tokens


# --- R2: compose passes the setting -----------------------------------------

def test_compose_passes_the_variant_to_the_api():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    environment = compose["services"]["argus-api"]["environment"]
    assert environment[VARIANT_ENV] == "${ARGUS_VERIFY_VARIANT:-shipped}"


# --- R3: default_keep -------------------------------------------------------

def test_default_keep_prompt_and_counter_evidence_schema_reach_the_model(monkeypatch):
    call, _ = _verify_with(monkeypatch, "default_keep")
    shipped_call, _ = _verify_with(monkeypatch, "shipped")
    assert call["system"] == review.VERIFY_SYSTEM_DEFAULT_KEEP != review.VERIFY_SYSTEM
    schema = call["input_schema"]
    counter = schema["properties"]["counter_evidence"]
    assert counter["type"] == ["object", "null"]
    assert "anyOf" not in counter
    assert list(counter["properties"]) == ["path", "start_line", "end_line", "quote"]
    assert counter["required"] == ["path", "start_line", "end_line", "quote"]
    assert counter["additionalProperties"] is False
    assert "counter_evidence" in schema["required"]
    assert call["user"] == shipped_call["user"], "the payload is not this variant's change"


def test_default_keep_schema_adds_only_counter_evidence():
    shipped = review.VERDICT_TOOL.input_schema
    keep = review.VERIFIER_VARIANTS["default_keep"].tool.input_schema
    properties = dict(keep["properties"])
    properties.pop("counter_evidence")
    assert _ordered(properties) == _ordered(shipped["properties"])
    assert keep["required"] == [*shipped["required"], "counter_evidence"]
    assert {k: v for k, v in keep.items() if k not in ("properties", "required")} == \
        {k: v for k, v in shipped.items() if k not in ("properties", "required")}


def test_default_keep_prompt_states_both_grounds_and_every_protected_class():
    prompt = review.VERIFY_SYSTEM_DEFAULT_KEEP
    lowered = prompt.lower()
    assert "absent from the diff of the finding's file" in lowered
    assert "literally contradicts the finding's central claim" in lowered
    for protected in PROTECTED_CLASSES:
        assert protected in lowered, protected
    assert "counter_evidence" in prompt and "report_verdict" in prompt


def test_default_keep_refutation_keeps_its_counter_evidence(monkeypatch):
    reply = {"is_real": False, "confidence": 0.9, "reasoning": "the timeout is set",
             "counter_evidence": QUOTE}
    _, verdict = _verify_with(monkeypatch, "default_keep", reply, read=_quote_at_head)
    assert verdict.is_real is False
    assert verdict.counter_evidence == CounterEvidence(**QUOTE)


@pytest.mark.parametrize("counter_evidence", [
    "missing", None, "src/app/handler.py:40", [], {},
    {**QUOTE, "quote": ""}, {**QUOTE, "quote": "   "}, {**QUOTE, "path": ""},
    {**QUOTE, "start_line": 0}, {**QUOTE, "start_line": "forty"},
    {**QUOTE, "start_line": 41, "end_line": 40}, {k: v for k, v in QUOTE.items() if k != "quote"},
], ids=["absent", "null", "string", "list", "empty", "empty-quote", "blank-quote", "empty-path",
        "line-zero", "line-text", "reversed-lines", "no-quote"])
def test_default_keep_unusable_counter_evidence_parses_to_none(monkeypatch, counter_evidence):
    reply = {"is_real": False, "confidence": 0.9, "reasoning": "refuted"}
    if counter_evidence != "missing":
        reply["counter_evidence"] = counter_evidence
    _, verdict = _verify_with(monkeypatch, "default_keep", reply, read=_quote_at_head)
    # A refutation without a usable quote parses to none, and the quote check then voids it.
    assert verdict.counter_evidence is None and verdict.confidence == 0.9
    assert verdict.is_real is True and verdict.refutation_voided


def test_default_keep_still_retries_once_on_an_invalid_verdict(monkeypatch):
    calls = _scripted(monkeypatch, [{"confidence": 0.9},
                                    {"is_real": True, "confidence": 0.6, "counter_evidence": None}])
    verdict = review._verify(FINDING, DIFF, "model-x", variant="default_keep")
    assert len(calls) == 2
    assert verdict.is_real is True and verdict.counter_evidence is None


@pytest.mark.parametrize("variant", ["shipped", "shipped_diff_first", "reasoning_first", "evidence"])
def test_variants_without_the_field_ignore_a_stray_counter_evidence(monkeypatch, variant):
    reply = {"is_real": False, "confidence": 0.9, "reasoning": "r", "counter_evidence": QUOTE}
    _, verdict = _verify_with(monkeypatch, variant, reply)
    assert not hasattr(verdict, "counter_evidence")
    assert "counter_evidence" not in verdict.model_dump()


# --- R4: reasoning_first ----------------------------------------------------

def test_reasoning_first_schema_differs_only_in_field_order():
    shipped = review.VERDICT_TOOL.input_schema
    first = review.VERIFIER_VARIANTS["reasoning_first"].tool.input_schema
    def unordered(schema):
        return {**schema, "required": sorted(schema["required"])}

    assert unordered(first) == unordered(shipped), "same content when order is ignored"
    assert _ordered(first) != _ordered(shipped)
    assert list(first["properties"]) == ["reasoning", "is_real", "confidence"]
    assert first["required"] == ["reasoning", "is_real", "confidence"]


def test_reasoning_first_changes_nothing_else_in_the_call(monkeypatch):
    call, verdict = _verify_with(monkeypatch, "reasoning_first")
    shipped_call, shipped_verdict = _verify_with(monkeypatch, "shipped")
    assert call["system"] == shipped_call["system"]
    assert call["user"] == shipped_call["user"]
    assert verdict.model_dump_json() == shipped_verdict.model_dump_json()


# --- R5: evidence -----------------------------------------------------------

def test_evidence_payload_differs_only_by_the_evidence_field(monkeypatch):
    call, _ = _verify_with(monkeypatch, "evidence")
    shipped_call, _ = _verify_with(monkeypatch, "shipped")
    payload = _payload(call["user"])
    shipped_payload = _payload(shipped_call["user"])
    assert payload.pop("evidence") == FINDING.evidence
    assert _ordered(payload) == _ordered(shipped_payload)
    assert call["user"].split("\n\n# Diff under review\n")[1] == DIFF
    assert call["system"] == shipped_call["system"]
    assert _ordered(call["input_schema"]) == _ordered(shipped_call["input_schema"])


def test_evidence_payload_carries_an_empty_evidence_field(monkeypatch):
    bare = FINDING.model_copy(update={"evidence": ""})
    call, _ = _verify_with(monkeypatch, "evidence", finding=bare)
    assert _payload(call["user"])["evidence"] == ""


# --- R6: the llm lane record names the variant ------------------------------

@pytest.mark.parametrize("variant", VARIANTS)
def test_llm_lane_record_names_the_variant_the_verifier_used(monkeypatch, restore_review_module,
                                                              variant):
    _reload_with(monkeypatch, variant)
    probe = {"lane": "probe", "status": "ran", "checks": 1}
    monkeypatch.setattr(live, "probe_scan_for_files", lambda *args: live.ProbeScan([], probe))
    monkeypatch.setattr(review, "REVIEW_PASSES", 1)
    systems = []

    def fake(model, system, user, tool):
        if tool.name == "report_findings":
            return {"findings": [FINDING.model_dump()]}
        systems.append(system)
        return {**REAL, "counter_evidence": None}

    monkeypatch.setattr(review, "complete_tool", fake)
    files = [FilePatch("src/app/handler.py", "modified", "@@ -40 +40 @@\n+resp = requests.get(url)\n")]
    result = review.run_diff_review(files, model="model-x")
    llm = next(r for r in result.coverage if r["lane"] == "llm")
    assert llm["verify_variant"] == variant
    assert systems == [review.VERIFIER_VARIANTS[variant].system]


def test_default_keep_review_keeps_counter_evidence_on_the_verdict(monkeypatch,
                                                                    restore_review_module):
    _reload_with(monkeypatch, "default_keep")
    probe = {"lane": "probe", "status": "ran", "checks": 1}
    monkeypatch.setattr(live, "probe_scan_for_files", lambda *args: live.ProbeScan([], probe))
    monkeypatch.setattr(review, "REVIEW_PASSES", 1)

    def fake(model, system, user, tool):
        if tool.name == "report_findings":
            return {"findings": [FINDING.model_dump()]}
        return {"is_real": False, "confidence": 0.9, "reasoning": "r", "counter_evidence": QUOTE}

    monkeypatch.setattr(review, "complete_tool", fake)
    files = [FilePatch("src/app/handler.py", "modified", "@@ -40 +40 @@\n+resp = requests.get(url)\n")]
    result = review.run_diff_review(files, model="model-x")
    assert [i.verdict.counter_evidence for i in result.all_findings] == [CounterEvidence(**QUOTE)]


# --- The verdict keeps its counter-evidence through every serializer ----------

def test_default_keep_counter_evidence_survives_serialization(monkeypatch):
    reply = {"is_real": False, "confidence": 0.9, "reasoning": "the timeout is set",
             "counter_evidence": QUOTE}
    _, verdict = _verify_with(monkeypatch, "default_keep", reply, read=_quote_at_head)
    item = VerifiedFinding(finding=FINDING, verdict=verdict)
    assert item.model_dump(mode="json")["verdict"]["counter_evidence"] == QUOTE
    assert json.loads(item.model_dump_json())["verdict"]["counter_evidence"] == QUOTE
    row = serialize_finding(item)
    assert row["verdict"]["counter_evidence"] == QUOTE
    assert row["verdict_is_real"] is False


def test_a_shipped_verdict_serializes_without_a_counter_evidence_key(monkeypatch):
    _, verdict = _verify_with(monkeypatch, "shipped")
    item = VerifiedFinding(finding=FINDING, verdict=verdict)
    assert list(item.model_dump()["verdict"]) == ["is_real", "confidence", "reasoning"]


# --- verify_variant: the name a review passes is the one that runs and is recorded ---

def test_run_diff_review_runs_and_records_the_variant_it_is_given(monkeypatch, unset_variant):
    _stub_diff_lanes(monkeypatch)
    calls, fake = _reviewer_then(REAL)
    monkeypatch.setattr(review, "complete_tool", fake)
    result = review.run_diff_review(FILES, model="model-x", verify_variant="evidence")
    llm = next(r for r in result.coverage if r["lane"] == "llm")
    assert llm["verify_variant"] == "evidence"
    assert len(calls) == 1
    assert _payload(calls[0]["user"])["evidence"] == FINDING.evidence


def test_run_diff_review_default_records_the_module_setting(monkeypatch, unset_variant):
    _stub_diff_lanes(monkeypatch)
    calls, fake = _reviewer_then(REAL)
    monkeypatch.setattr(review, "complete_tool", fake)
    result = review.run_diff_review(FILES, model="model-x")
    assert next(r for r in result.coverage if r["lane"] == "llm")["verify_variant"] == "shipped"
    assert "evidence" not in _payload(calls[0]["user"])


@pytest.mark.parametrize("fast", [False, True], ids=["full", "fast"])
def test_run_diff_review_rejects_an_unknown_variant_before_any_work(monkeypatch, fast):
    def forbidden(*args, **kwargs):
        raise AssertionError("no lane may run for an unknown variant")

    monkeypatch.setattr(review, "complete_tool", forbidden)
    monkeypatch.setattr(live, "probe_scan_for_files", forbidden)
    with pytest.raises(ValueError, match="no_such_variant"):
        review.run_diff_review(FILES, model="model-x", fast=fast, verify_variant="no_such_variant")


def _stub_pr(monkeypatch):
    monkeypatch.setattr(review, "fetch_pr_files", lambda repo, pr: list(FILES))
    monkeypatch.setattr(live, "_pr_meta", lambda *args: {
        "title": "", "body": "", "commits": [], "head_sha": "sha"})
    monkeypatch.setattr(live, "_fetch_file_at", lambda *args: None)
    monkeypatch.setattr(live, "probe_scan",
                        lambda *args: live.ProbeScan([], live._probe_record(6)))
    monkeypatch.setattr(review, "fetch_house_rules", lambda repo: None)
    monkeypatch.setattr(review, "_intent_for_prompt", lambda *args: None)
    monkeypatch.setattr(review, "_deep_review",
                        lambda *args, **kwargs: Review(findings=[FINDING]))


def test_run_review_runs_and_records_the_variant_it_is_given(monkeypatch, unset_variant):
    _stub_pr(monkeypatch)
    calls, fake = _reviewer_then({**REAL, "counter_evidence": None})
    monkeypatch.setattr(review, "complete_tool", fake)
    result = review.run_review("example/x", 1, model="model-x", verify_variant="default_keep")
    llm = next(r for r in result.coverage if r["lane"] == "llm")
    assert llm["verify_variant"] == "default_keep"
    assert [c["system"] for c in calls] == [review.VERIFY_SYSTEM_DEFAULT_KEEP]


def test_run_review_rejects_an_unknown_variant_before_fetching_the_pr(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the PR must not be fetched for an unknown variant")

    monkeypatch.setattr(review, "fetch_pr_files", forbidden)
    monkeypatch.setattr(review, "complete_tool", forbidden)
    with pytest.raises(ValueError, match="no_such_variant"):
        review.run_review("example/x", 1, verify_variant="no_such_variant")


# --- ENG-1194: class-specific checklists ------------------------------------

CHECKLIST_CASES = {
    "resources": ("timeout", "request path"),
    "error_paths": ("fallback", "tell"),
    "input_handling": ("guard", "bypass"),
    "security": ("registered before", "route"),
}


def _in_category(category):
    return FINDING.model_copy(update={"category": category})


def test_there_is_one_checklist_for_each_of_the_four_categories():
    assert set(review.VERIFY_CHECKLISTS) == set(CHECKLIST_CASES)
    for category, words in CHECKLIST_CASES.items():
        text = review.VERIFY_CHECKLISTS[category].lower()
        for word in words:
            assert word in text, (category, word)


SCOPE_TOPICS = {"resources": "a missing timeout", "error_paths": "a silent fallback",
                "input_handling": "an input guard", "security": "auth or middleware order"}


@pytest.mark.parametrize("category", sorted(CHECKLIST_CASES))
def test_each_checklist_opens_with_a_scope_line_for_its_topic(category):
    first = review.VERIFY_CHECKLISTS[category].splitlines()[0]
    assert first == (f"Use this checklist only if the finding is about {SCOPE_TOPICS[category]}; "
                     "otherwise ignore it.")


@pytest.mark.parametrize("category", sorted(CHECKLIST_CASES))
def test_each_checklist_ends_by_denying_its_answers_as_a_refutation_ground(category):
    closing = ("These answers inform your decision but are not a ground to refute: "
               "refute only on ground A or ground B.")
    assert review.CHECKLIST_CLOSING == closing
    assert review.VERIFY_CHECKLISTS[category].endswith(f"\n{closing}")
    assert "ground A" in review.VERIFY_SYSTEM_DEFAULT_KEEP


def test_a_resources_finding_gets_the_timeout_checklist_under_default_keep_checklists(
        monkeypatch):
    call, _ = _verify_with(monkeypatch, "default_keep_checklists")
    assert review.VERIFY_CHECKLISTS["resources"] in call["user"]
    assert "timeout" in review.VERIFY_CHECKLISTS["resources"].lower()


@pytest.mark.parametrize("category", sorted(CHECKLIST_CASES))
def test_default_keep_checklists_differs_from_default_keep_only_by_the_checklist(
        monkeypatch, category):
    finding = _in_category(category)
    plain, plain_verdict = _verify_with(monkeypatch, "default_keep", finding=finding)
    listed, listed_verdict = _verify_with(monkeypatch, "default_keep_checklists", finding=finding)
    assert listed["system"] == plain["system"]
    assert _ordered(listed["input_schema"]) == _ordered(plain["input_schema"])
    assert listed["max_tokens"] == plain["max_tokens"] and listed["model"] == plain["model"]
    assert listed["user"].startswith(plain["user"])
    assert listed["user"] == \
        f"{plain['user']}\n\n{review.CHECKLIST_HEADING}\n{review.VERIFY_CHECKLISTS[category]}"
    for other, text in review.VERIFY_CHECKLISTS.items():
        assert (text in listed["user"]) == (other == category)
    assert type(listed_verdict) is type(plain_verdict)


@pytest.mark.parametrize("category", ["other", "contracts", "boundaries", "concurrency",
                                      "tests", "data_integrity", None])
def test_other_categories_get_the_default_keep_call_unchanged(monkeypatch, category):
    finding = _in_category(category)
    plain, _ = _verify_with(monkeypatch, "default_keep", finding=finding)
    listed, _ = _verify_with(monkeypatch, "default_keep_checklists", finding=finding)
    assert _ordered(listed) == _ordered(plain)


@pytest.mark.parametrize("variant", ["shipped", "shipped_diff_first", "default_keep",
                                     "reasoning_first", "evidence"])
@pytest.mark.parametrize("category", sorted(CHECKLIST_CASES))
def test_other_variants_never_carry_a_checklist(monkeypatch, variant, category):
    call, _ = _verify_with(monkeypatch, variant, finding=_in_category(category))
    for text in review.VERIFY_CHECKLISTS.values():
        assert text not in call["user"]
        assert text not in call["system"]


def test_default_keep_checklists_reuses_the_default_keep_prompt_schema_and_verdict_type():
    keep = review.VERIFIER_VARIANTS["default_keep"]
    listed = review.VERIFIER_VARIANTS["default_keep_checklists"]
    assert listed.system is keep.system
    assert listed.tool is keep.tool
    assert listed.verdict_type is keep.verdict_type


def test_default_keep_checklists_refutation_keeps_its_counter_evidence(monkeypatch):
    reply = {"is_real": False, "confidence": 0.9, "reasoning": "set", "counter_evidence": QUOTE}
    _, verdict = _verify_with(monkeypatch, "default_keep_checklists", reply)
    assert verdict.counter_evidence == CounterEvidence(**QUOTE)
