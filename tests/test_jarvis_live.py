"""Behaviour tests for wiring the probe lane into live review."""

import logging
from pathlib import Path

import pytest

from argus.diff import FilePatch
from argus.jarvis import live
from argus.schema import Finding, Review, Severity, Verdict, VerifiedFinding


@pytest.fixture(autouse=True)
def reset_known_fp_keys(monkeypatch):
    monkeypatch.setattr(live, "_KNOWN_FP_KEYS", None)
    monkeypatch.delenv("ARGUS_KNOWN_FPS_PATH", raising=False)


def _llm_finding() -> Finding:
    return Finding(file="a.py", line=1, severity=Severity.major, confidence=0.8,
                   title="llm bug", description="d", proposed_fix="f", rule_id=None)


def _scan(findings: list) -> live.ProbeScan:
    """A stubbed probe lane that ran the whole catalogue."""
    return live.ProbeScan(findings, live._probe_record(6))


def _probe_vf() -> VerifiedFinding:
    return VerifiedFinding(
        finding=Finding(file="app/api/checkout/x/route.ts", line=2, severity=Severity.critical,
                        confidence=1.0, title="probe: client_trusted_price", description="n",
                        proposed_fix="x", rule_id="client_trusted_price"),
        verdict=Verdict(is_real=True, confidence=1.0, reasoning="deterministic probe"))


@pytest.fixture
def stub_review(monkeypatch):
    from argus import review
    monkeypatch.setattr(review, "fetch_pr_files", lambda repo, pr: [
        FilePatch("a.py", "modified", "@@ -1 +1 @@\n+pass")])
    monkeypatch.setattr(live, "_pr_meta", lambda *args: {
        "title": "", "body": "", "commits": [], "head_sha": "sha"})
    monkeypatch.setattr(live, "_fetch_file_at", lambda *args: None)
    monkeypatch.setattr(review, "fetch_house_rules", lambda repo: None)
    monkeypatch.setattr(review, "_deep_review",
                        lambda diff, hr, model, intent_section=None, pr_section="": Review(findings=[_llm_finding()]))
    monkeypatch.setattr(review, "_verify",
                        lambda f, diff, model, **_: Verdict(is_real=True, confidence=0.9, reasoning="r"))
    return review


def test_run_review_appends_probe_findings(stub_review, monkeypatch):
    monkeypatch.setattr(live, "probe_scan", lambda repo, pr, *args: _scan([_probe_vf()]))
    result = stub_review.run_review("example/service", 1)
    titles = {vf.finding.title for vf in result.bugs}
    assert "llm bug" in titles
    assert "probe: client_trusted_price" in titles
    assert result.raw_count == 1  # LLM raw count untouched by the probe lane
    assert result.dropped == 0


def test_run_review_survives_probe_lane_failure(stub_review, monkeypatch):
    def _boom(repo, pr, *args):
        raise RuntimeError("probe lane exploded")

    monkeypatch.setattr(live, "probe_scan", _boom)
    result = stub_review.run_review("example/service", 1)
    assert {vf.finding.title for vf in result.bugs} == {"llm bug"}  # core review intact


def test_run_review_passes_intent_section_to_deep_review(stub_review, monkeypatch):
    captured = {}

    def _capture(diff, house_rules, model, intent_section=None, pr_section=""):
        captured["intent"] = intent_section
        return Review(findings=[])

    monkeypatch.setattr(stub_review, "_deep_review", _capture)
    monkeypatch.setattr(live, "intent_section",
                        lambda repo, pr, meta=None: "# PR intent (recovered from Linear TASK-1)\n...")
    monkeypatch.setattr(live, "probe_scan", lambda repo, pr, *args: _scan([]))
    stub_review.run_review("example/x", 1)
    assert captured["intent"] and "TASK-1" in captured["intent"]


def test_run_review_survives_intent_failure(stub_review, monkeypatch):
    def _boom(repo, pr, *args):
        raise RuntimeError("linear down")

    monkeypatch.setattr(live, "intent_section", _boom)
    monkeypatch.setattr(live, "probe_scan", lambda repo, pr, *args: _scan([]))
    result = stub_review.run_review("example/service", 1)
    assert {vf.finding.title for vf in result.bugs} == {"llm bug"}  # intent failure is non-fatal


def test_probe_findings_emits_deterministic_finding(monkeypatch):
    monkeypatch.setattr(live, "_changed_paths",
                        lambda repo, pr: ["app/api/checkout/place/route.ts", "README.md"])
    monkeypatch.setattr(live, "_pr_head_sha", lambda repo, pr: "headsha")
    contents = {"app/api/checkout/place/route.ts":
                "const s = z.object({ unitPriceToman: z.number().int() });\n"
                "export async function POST(req) { return createOrder(s); }\n"}
    monkeypatch.setattr(live, "_fetch_file_at", lambda repo, path, ref: contents.get(path))

    found = live.probe_findings("example/web", 1)

    assert len(found) == 1
    verified = found[0]
    assert verified.finding.rule_id == "client_trusted_price"
    assert verified.finding.severity.value == "critical"
    assert verified.finding.confidence == 1.0
    assert verified.verdict.is_real is True
    assert "client_trusted_price" in verified.verdict.reasoning


def test_probe_findings_short_circuits_with_no_candidate_files(monkeypatch):
    # Non-candidate paths must not trigger a head-sha or content fetch at all.
    monkeypatch.setattr(live, "_changed_paths", lambda repo, pr: ["README.md", "src/util.py"])

    def _boom(*args):
        raise AssertionError("must not fetch when there are no candidate files")

    monkeypatch.setattr(live, "_pr_head_sha", _boom)
    monkeypatch.setattr(live, "_fetch_file_at", _boom)

    assert live.probe_findings("example/service", 1) == []


def test_probe_findings_silent_on_a_fixed_file(monkeypatch):
    monkeypatch.setattr(live, "_changed_paths", lambda repo, pr: ["app/api/checkout/x/route.ts"])
    monkeypatch.setattr(live, "_pr_head_sha", lambda repo, pr: "sha")
    fixed = {"app/api/checkout/x/route.ts":
             "const s = z.object({ unitPriceToman: z.number() });\n"
             "const p = await getProductBySlug(slug); // re-priced server-side\n"}
    monkeypatch.setattr(live, "_fetch_file_at", lambda repo, path, ref: fixed.get(path))

    assert live.probe_findings("example/service", 2) == []


def test_intent_section_injects_recovered_issue(monkeypatch):
    monkeypatch.setenv("ARGUS_LINEAR_REPOS", "example/payments-api")
    monkeypatch.setenv("ARGUS_LINEAR_KEY_PREFIX", "TASK")
    monkeypatch.setattr(live, "_pr_meta",
                        lambda repo, pr: {"author_association": "OWNER", "title": "fix TASK-449 fan-in", "body": "", "commits": []})
    monkeypatch.setattr(live.linear, "fetch_issue", lambda ident, **k: {
        "identifier": "TASK-449", "title": "Fan-in", "description": "Problem: gaps. Acceptance: detect."})
    section = live.intent_section("example/payments-api", 71)
    assert section is not None
    assert "TASK-449" in section and "Fan-in" in section and "Acceptance: detect." in section


def test_intent_section_none_for_repo_without_an_issue(monkeypatch):
    # A repo outside the configured issue scope has nothing to inject.
    monkeypatch.setattr(live, "_pr_meta",
                        lambda repo, pr: {"title": "random change", "body": "", "commits": []})
    assert live.intent_section("example/app", 1) is None


def test_intent_section_referenced_but_unavailable_when_fetch_fails(monkeypatch):
    monkeypatch.setenv("ARGUS_LINEAR_REPOS", "example/argus")
    monkeypatch.setenv("ARGUS_LINEAR_KEY_PREFIX", "TASK")
    monkeypatch.setattr(live, "_pr_meta",
                        lambda repo, pr: {"author_association": "OWNER", "title": "TASK-9 wip", "body": "", "commits": []})
    monkeypatch.setattr(live.linear, "fetch_issue", lambda ident, **k: None)
    section = live.intent_section("example/argus", 1)
    assert section is not None
    assert "TASK-9" in section and "unavailable" in section.lower()


_FP_TITLE = "Concurrent requests can synthesize the same turn multiple times (race / quota)"


def test_suppress_known_fps_drops_matching_finding(monkeypatch):
    monkeypatch.setattr(live, "_known_fp_keys",
                        lambda: {("example/app", "app/main.py", _FP_TITLE.lower())})
    fp = Finding(file="app/main.py", line=35, severity=Severity.major, confidence=0.8,
                 title=_FP_TITLE, description="d", proposed_fix="f", rule_id=None)
    real = Finding(file="app/other.py", line=1, severity=Severity.major, confidence=0.8,
                   title="a real one", description="d", proposed_fix="f", rule_id=None)
    kept, suppressed = live.suppress_known_fps("example/app", [fp, real])
    assert [f.title for f in kept] == ["a real one"]
    assert [f.title for f in suppressed] == [_FP_TITLE]


def test_suppress_matches_case_and_whitespace_insensitively(monkeypatch):
    monkeypatch.setattr(live, "_known_fp_keys", lambda: {("example/service", "a.py", "some title")})
    finding = Finding(file="a.py", line=1, severity=Severity.minor, confidence=0.5,
                      title="  SOME   Title ", description="d", proposed_fix="f", rule_id=None)
    kept, suppressed = live.suppress_known_fps("example/service", [finding])
    assert kept == [] and len(suppressed) == 1


def test_known_fp_keys_loads_the_example_fixture(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "data/fixtures/known_fps.example.jsonl"
    monkeypatch.setenv("ARGUS_KNOWN_FPS_PATH", str(path))
    monkeypatch.setattr(live, "_KNOWN_FP_KEYS", None)
    keys = live._known_fp_keys()
    assert len(keys) == 3
    assert ("example/app", "app/main.py",
            "concurrent requests can run the same job twice (race)") in keys


@pytest.fixture
def argus_logs(monkeypatch):
    # The API lifespan sets propagate=False on the "argus" logger; restore it so
    # caplog sees records regardless of which tests ran before.
    monkeypatch.setattr(logging.getLogger("argus"), "propagate", True)


@pytest.mark.parametrize("value", [None, ""])
def test_known_fp_keys_empty_when_path_unset(monkeypatch, caplog, argus_logs, value):
    if value is None:
        monkeypatch.delenv("ARGUS_KNOWN_FPS_PATH", raising=False)
    else:
        monkeypatch.setenv("ARGUS_KNOWN_FPS_PATH", value)
    monkeypatch.setattr(live, "_KNOWN_FP_KEYS", None)
    with caplog.at_level("DEBUG", logger="argus.jarvis.live"):
        assert live._known_fp_keys() == set()
    assert "suppression from a file is off" in caplog.text


def test_known_fp_keys_empty_when_path_missing(tmp_path, monkeypatch, caplog, argus_logs):
    monkeypatch.setenv("ARGUS_KNOWN_FPS_PATH", str(tmp_path / "missing.jsonl"))
    monkeypatch.setattr(live, "_KNOWN_FP_KEYS", None)
    with caplog.at_level("DEBUG", logger="argus.jarvis.live"):
        assert live._known_fp_keys() == set()
        assert live._known_fp_keys() == set()
    assert "suppression from a file is off" in caplog.text
    assert len([record for record in caplog.records if record.levelname == "WARNING"]) == 1


def test_run_review_suppresses_a_known_fp(stub_review, monkeypatch):
    monkeypatch.setattr(live, "_known_fp_keys",
                        lambda: {("example/app", "app/main.py", _FP_TITLE.lower())})
    monkeypatch.setattr(stub_review, "_deep_review", lambda d, h, m, intent_section=None, pr_section="": Review(
        findings=[Finding(file="app/main.py", line=35, severity=Severity.major, confidence=0.8,
                          title=_FP_TITLE, description="d", proposed_fix="f", rule_id=None)]))
    monkeypatch.setattr(live, "probe_scan", lambda repo, pr, *args: _scan([]))
    monkeypatch.setattr(live, "intent_section", lambda repo, pr, meta=None: None)
    result = stub_review.run_review("example/app", 1)
    assert all(vf.finding.title != _FP_TITLE for vf in result.bugs + result.nits)


def test_candidate_paths_prefilters_by_basename():
    changed = ["app/api/checkout/place/route.ts", "pwa/sw.js", "deploy/nginx.conf",
               "src/util.py", "README.md", "components/Card.tsx"]
    candidates = live._candidate_paths(changed)
    assert "src/util.py" not in candidates and "README.md" not in candidates
    assert "pwa/sw.js" in candidates and "deploy/nginx.conf" in candidates
    assert "app/api/checkout/place/route.ts" in candidates
