"""run_diff_review: fast/full modes, fingerprint stamping, and the judge lane."""

from argus import review as review_mod
from argus.diff import FilePatch
from argus.jarvis import live
from argus.review import run_diff_review
from argus.schema import Finding, Review, Severity, Verdict

COMPOSE_PATCH = """@@ -1,2 +1,3 @@
 services:
+  web: {ports: ["0.0.0.0:8080:80"]}
 volumes:
"""

PY_PATCH = """@@ -1,2 +1,3 @@
 import httpx
+resp = httpx.get(url)
 print(resp)
"""


def _files():
    return [
        FilePatch(filename="deploy/docker-compose.yml", status="modified", patch=COMPOSE_PATCH),
        FilePatch(filename="src/app.py", status="modified", patch=PY_PATCH),
    ]


def test_fast_mode_runs_probes_only_and_never_calls_the_model(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("fast mode must not call the LLM")

    monkeypatch.setattr(review_mod, "complete_tool", boom)
    result = run_diff_review(_files(), fast=True)
    assert result.raw_count == 0
    hits = result.bugs + result.nits
    assert [i.finding.rule_id for i in hits] == ["app_bound_to_all_interfaces"]
    assert hits[0].lane == "probe"
    assert hits[0].fingerprint.startswith("argusfp:")


def test_patch_mode_skips_file_probes(monkeypatch):
    # A cypher.py patch would trip unscoped_cypher_endpoint IF FileProbes ran on
    # partial content (the absent-marker may live outside the hunk); assert the
    # patch-only path leaves them out.
    cypher = FilePatch(filename="src/api/graph/cypher.py", status="modified",
                       patch="@@ -1,1 +1,2 @@\n import x\n+session.run(q)\n")
    hits = live.probe_findings_for_files(None, None, [cypher])
    assert hits == []


def test_full_mode_reviews_verifies_and_stamps_fingerprints(monkeypatch):
    finding = Finding(file="src/app.py", line=2, severity=Severity.major,
                      confidence=0.7, title="no timeout on httpx.get",
                      description="d", proposed_fix="add timeout")
    monkeypatch.setattr(review_mod, "_deep_review",
                        lambda diff, rules, model, intent, pr_section="": Review(findings=[finding]))
    monkeypatch.setattr(review_mod, "_verify",
                        lambda f, diff, model, **_: Verdict(is_real=True, confidence=0.9, reasoning="r"))
    result = run_diff_review(_files(), repo=None)
    llm_bugs = [i for i in result.bugs if i.lane == "llm"]
    assert len(llm_bugs) == 1
    stamped = llm_bugs[0]
    assert stamped.fingerprint.startswith("argusfp:")
    assert result.raw_count == 1
    # The probe hit rides along with its own lane.
    assert any(i.lane == "probe" for i in result.bugs + result.nits)


def test_judge_lane_off_by_default(monkeypatch):
    monkeypatch.delenv(review_mod.JUDGE_LIVE_ENV, raising=False)

    def boom(*args, **kwargs):
        raise AssertionError("judge must not run when the lane is off")

    monkeypatch.setattr("argus.jarvis.automark.judge_finding", boom)
    finding = Finding(file="src/app.py", line=2, severity=Severity.major,
                      confidence=0.7, title="t", description="d", proposed_fix="p")
    monkeypatch.setattr(review_mod, "_deep_review",
                        lambda *a: Review(findings=[finding]))
    monkeypatch.setattr(review_mod, "_verify",
                        lambda *a, **_: Verdict(is_real=True, confidence=0.9, reasoning="r"))
    result = run_diff_review(_files())
    assert all(i.judge is None for i in result.bugs)


def _configure_judge_pair(monkeypatch):
    """Both judges available, so selection has a real choice to make. The default
    reviewer is Claude, so the lane must pick the GPT judge."""
    from argus.jarvis import judges
    monkeypatch.setenv(judges.ENV_BASE_URL, "http://gateway.test/v1/chat/completions")
    monkeypatch.setenv(judges.ENV_ANTHROPIC_MODEL, "claude-opus-5-5")
    monkeypatch.setenv(judges.ENV_ANTHROPIC_KEY, "claude-key")
    monkeypatch.setenv(judges.ENV_OPENAI_MODEL, "gpt-6.1-sol")
    monkeypatch.setenv(judges.ENV_OPENAI_KEY, "gpt-key")


def test_judge_lane_agreement_disagreement_and_failure(monkeypatch):
    monkeypatch.setenv(review_mod.JUDGE_LIVE_ENV, "1")
    _configure_judge_pair(monkeypatch)
    answers = iter([
        {"verdict": "real", "confidence": 0.92},
        {"verdict": "fp", "confidence": 0.8},
        RuntimeError("judge down"),
    ])

    def fake_judge(finding, snippet, reviewer_model=None):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr("argus.jarvis.automark.judge_finding", fake_judge)
    findings = [
        Finding(file="src/app.py", line=2, severity=Severity.major, confidence=0.9,
                title=f"bug {n}", description="d", proposed_fix="p")
        for n in range(3)
    ]
    monkeypatch.setattr(review_mod, "_deep_review",
                        lambda *a: Review(findings=findings))
    monkeypatch.setattr(review_mod, "_verify",
                        lambda *a, **_: Verdict(is_real=True, confidence=0.9, reasoning="r"))
    result = run_diff_review(_files())
    llm_bugs = [i for i in result.bugs if i.lane == "llm"]
    verdicts = [i.judge.verdict if i.judge else None for i in llm_bugs]
    assert verdicts == ["real", "fp", None]


def test_judge_lane_sanitizes_unknown_verdicts(monkeypatch):
    monkeypatch.setenv(review_mod.JUDGE_LIVE_ENV, "1")
    _configure_judge_pair(monkeypatch)
    monkeypatch.setattr(
        "argus.jarvis.automark.judge_finding",
        lambda finding, snippet, reviewer_model=None: {"verdict": "MAYBE?", "confidence": 7})
    finding = Finding(file="src/app.py", line=2, severity=Severity.critical,
                      confidence=0.9, title="t", description="d", proposed_fix="p")
    monkeypatch.setattr(review_mod, "_deep_review", lambda *a: Review(findings=[finding]))
    monkeypatch.setattr(review_mod, "_verify",
                        lambda *a, **_: Verdict(is_real=True, confidence=0.9, reasoning="r"))
    result = run_diff_review(_files())
    judged = [i for i in result.bugs if i.lane == "llm"][0]
    assert judged.judge.verdict == "unsure"
    assert judged.judge.confidence == 1.0


def test_post_image_reconstruction():
    assert live._post_image(PY_PATCH).splitlines() == [
        "import httpx", "resp = httpx.get(url)", "print(resp)"]
