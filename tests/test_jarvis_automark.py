"""Behaviour tests for the Jarvis auto-marker.

The judge, GitHub fetch, and db writer are injected, so nothing here makes a live
call. Covers the tier ladder (probe/locate/judge), threshold gating, the
never-overwrite guarantee, dry-run vs commit, fail-safe degradation, the
non-Anthropic guard, and the human-only precision/corpus filters.
"""

import pytest

from argus.api import db
from argus.jarvis import automark, corpus

# A checkout route with a client-supplied price and no server re-pricing: fires
# the client_trusted_price incident probe at line 1 (mirrors the live probe test).
_PROBE_FILE = "app/api/checkout/place/route.ts"
_PROBE_CONTENT = (
    "const s = z.object({ unitPriceToman: z.number().int() });\n"
    "export async function POST(req) { return createOrder(s); }\n"
)


def _finding(**over) -> automark.Finding:
    base = dict(finding_id=1, repo="example/x", pr=1, file="src/app.py", line=3,
                severity="major", title="t", description="d", has_mark=False)
    base.update(over)
    return automark.Finding(**base)


def _judge(verdict: str, confidence: float, evidence: str = "e"):
    return lambda payload: {"verdict": verdict, "confidence": confidence, "evidence": evidence}


# ------------------------------------------------------------------ tier 1: probe

def test_probe_hit_marks_real(monkeypatch):
    def _boom(_payload):
        raise AssertionError("judge must not run when a probe fires")

    finding = _finding(file=_PROBE_FILE, line=1)
    decision = automark.classify(finding, _PROBE_CONTENT, "headsha0000", judge=_boom)
    assert decision.mark == "real"
    assert decision.tier == "probe"
    assert decision.marker == automark.MARKER_PROBE
    assert decision.confidence == 1.0
    assert decision.writable is True
    assert "client_trusted_price" in decision.evidence


def test_probe_ignored_when_hit_is_far_from_finding_line(monkeypatch):
    # The probe fires at line 1; a finding 500 lines away is a different issue,
    # so it falls through to the judge rather than being mislabelled real.
    finding = _finding(file=_PROBE_FILE, line=500)
    padded = _PROBE_CONTENT + ("// filler\n" * 600)
    decision = automark.classify(finding, padded, "head", judge=_judge("fp", 0.9))
    assert decision.tier == "judge"
    assert decision.mark == "fp"


# ------------------------------------------------------------------ tier 2: locate

def test_locate_missing_file_marks_cant_locate():
    decision = automark.classify(_finding(), None, "deadbeef1234", judge=_judge("real", 1.0))
    assert decision.mark == "cant_locate"
    assert decision.tier == "locate"
    assert decision.marker == automark.MARKER_LOCATE
    assert decision.writable is True


def test_locate_line_out_of_range_marks_stale():
    decision = automark.classify(_finding(line=99), "one\ntwo\n", "headsha", judge=_judge("real", 1.0))
    assert decision.mark == "stale"
    assert decision.tier == "locate"
    assert "out of range" in decision.evidence


# ------------------------------------------------------------------ tier 3: judge

def test_judge_real_above_threshold_is_writable():
    decision = automark.classify(_finding(), "a\nb\nc\nd\n", "h", judge=_judge("real", 0.9))
    assert decision.mark == "real" and decision.tier == "judge"
    assert decision.marker == automark.MARKER_JUDGE
    assert decision.writable is True


def test_judge_fp_above_threshold_is_writable():
    decision = automark.classify(_finding(), "a\nb\nc\nd\n", "h", judge=_judge("fp", 0.85))
    assert decision.mark == "fp"
    assert decision.writable is True


def test_judge_unsure_is_not_writable():
    decision = automark.classify(_finding(), "a\nb\nc\nd\n", "h", judge=_judge("unsure", 0.99))
    assert decision.mark == "unsure"
    assert decision.writable is False


def test_judge_real_below_threshold_is_gated_out():
    decision = automark.classify(_finding(), "a\nb\nc\nd\n", "h",
                                 judge=_judge("real", 0.5), threshold=0.75)
    assert decision.mark == "real"
    assert decision.writable is False  # confidence under threshold -> needs human


def test_unparseable_verdict_degrades_to_unsure():
    def _bad(_payload):
        return {"verdict": "yes-definitely", "confidence": "high"}

    decision = automark.classify(_finding(), "a\nb\nc\nd\n", "h", judge=_bad)
    assert decision.mark == "unsure"
    assert decision.writable is False


def test_judge_exception_degrades_to_unsure_without_crashing():
    def _boom(_payload):
        raise RuntimeError("transport blew up")

    decision = automark.classify(_finding(), "a\nb\nc\nd\n", "h", judge=_boom)
    assert decision.mark == "unsure"
    assert decision.provenance == "auto:error"
    assert decision.writable is False


# ------------------------------------------------------------- non-Anthropic guard

def test_anthropic_judge_is_refused():
    for bad in ("claude-opus-4-8", "anthropic", "claude-3", "opus", "sonnet"):
        with pytest.raises(ValueError):
            automark._assert_non_anthropic("http://host/v1/chat/completions", bad)
    with pytest.raises(ValueError):
        automark._assert_non_anthropic("https://api.anthropic.com/v1/x", "gemma")


def test_non_anthropic_judge_is_allowed():
    automark._assert_non_anthropic("http://127.0.0.1:8002/v1/chat/completions", "gemma-4")
    automark._assert_non_anthropic("http://192.0.2.10:8000/v1/chat/completions", "medgemma-4b")


def test_parse_verdict_unwraps_code_fenced_json():
    raw = '```json\n{"verdict":"real","confidence":0.9,"evidence":"off-by-one"}\n```'
    parsed = automark._parse_verdict(raw)
    assert parsed["verdict"] == "real" and parsed["confidence"] == 0.9


def test_parse_verdict_salvages_truncated_reply():
    # max_tokens cut the reply mid-evidence: no closing brace, but verdict and
    # confidence lead the object and must still be recovered.
    raw = '```json\n{"verdict":"fp", "confidence":0.9, "evidence":"the snippet only defines'
    parsed = automark._parse_verdict(raw)
    assert parsed["verdict"] == "fp" and parsed["confidence"] == 0.9


def test_parse_verdict_empty_content_raises_cleanly():
    # A model can return null/empty content; default_judge coerces None -> "", and the
    # parser must raise a clean ValueError (-> degraded to unsure) not a TypeError.
    with pytest.raises(ValueError):
        automark._parse_verdict("")


@pytest.mark.parametrize("url, model", [
    (None, None), ("", ""), (" \t", " \t"),
    ("https://judge.test/v1/chat/completions", None), (None, "gemma"),
])
def test_unconfigured_automark_is_disabled(monkeypatch, url, model):
    for name, value in ((automark.ENV_JUDGE_URL, url), (automark.ENV_JUDGE_MODEL, model)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    def unexpected_request(*args, **kwargs):
        raise AssertionError("disabled judge must not make a request")

    monkeypatch.setattr(automark.httpx, "post", unexpected_request)
    monkeypatch.setattr(automark.httpx, "get", unexpected_request)
    with pytest.raises(RuntimeError, match="automark.*disabled") as caught:
        automark.default_judge({})
    for name in ("ARGUS_AUTOMARK_JUDGE_URL", "ARGUS_AUTOMARK_JUDGE_MODEL"):
        assert name in str(caught.value)
    assert automark.judge_reachable() == (False, str(caught.value))


# ------------------------------------------------------------------------- run()

def _run_with(findings, judge, *, commit, **kw):
    writes = []

    def _write(finding_id, mark, *, marker, note):
        writes.append({"finding_id": finding_id, "mark": mark, "marker": marker, "note": note})
        return len(writes)

    report = automark.run(
        list_findings=lambda **_: findings,
        fetch_file=kw.get("fetch_file", lambda repo, path, ref: "a\nb\nc\nd\n"),
        head_sha_for=kw.get("head_sha_for", lambda repo, pr: "headsha0000"),
        judge=judge, write_mark=_write, commit=commit,
        judge_ok=kw.get("judge_ok", True), threshold=kw.get("threshold", 0.75))
    return report, writes


def test_dry_run_does_not_write():
    report, writes = _run_with([_finding()], _judge("real", 0.9), commit=False)
    assert writes == []                       # nothing persisted in dry-run
    assert len(report.would_mark) == 1
    assert report.would_mark[0].mark == "real"
    assert report.marked == []


def test_commit_writes_the_mark_with_tier_marker():
    report, writes = _run_with([_finding(finding_id=42)], _judge("real", 0.9), commit=True)
    assert len(writes) == 1
    assert writes[0]["finding_id"] == 42
    assert writes[0]["marker"] == automark.MARKER_JUDGE
    assert writes[0]["mark"] == "real"
    assert "auto:judge" in writes[0]["note"] and "conf=0.90" in writes[0]["note"]
    assert len(report.marked) == 1


def test_never_overwrites_an_existing_mark():
    def _boom(_payload):
        raise AssertionError("a marked finding must not reach the judge")

    report, writes = _run_with([_finding(has_mark=True)], _boom, commit=True)
    assert writes == []
    assert len(report.skipped) == 1
    assert report.skipped[0][1] == "already marked"


def test_below_threshold_goes_to_needs_human_not_written():
    report, writes = _run_with([_finding()], _judge("real", 0.4), commit=True)
    assert writes == []
    assert len(report.needs_human) == 1
    assert report.needs_human[0].mark == "real"


def test_degraded_judge_routes_judge_tier_to_human_but_keeps_probe():
    findings = [_finding(finding_id=1, file=_PROBE_FILE, line=1),  # probe, no judge needed
                _finding(finding_id=2)]                            # would need the judge
    contents = {_PROBE_FILE: _PROBE_CONTENT, "src/app.py": "a\nb\nc\nd\n"}
    report, writes = _run_with(
        findings, _judge("real", 0.9), commit=True, judge_ok=False,
        fetch_file=lambda repo, path, ref: contents.get(path))
    marked_ids = {d.finding_id for d in report.marked}
    human_ids = {d.finding_id for d in report.needs_human}
    assert marked_ids == {1}          # probe tier still resolves
    assert human_ids == {2}           # judge tier degraded to needs-human
    assert [w["finding_id"] for w in writes] == [1]


def test_fetch_failure_skips_finding_without_crashing():
    def _raise(repo, path, ref):
        raise RuntimeError("github down")

    report, writes = _run_with([_finding()], _judge("real", 0.9), commit=True, fetch_file=_raise)
    assert writes == []
    assert len(report.skipped) == 1
    assert "file fetch failed" in report.skipped[0][1]


# ----------------------------------------------- human-only precision + corpus

@pytest.fixture
def argus_path(tmp_path, monkeypatch) -> str:
    path = tmp_path / "argus.db"
    monkeypatch.setenv("ARGUS_DB_PATH", str(path))
    db.init_db(str(path))
    return str(path)


def _seed_finding(argus_path: str, file: str = "a.py", line: int = 10) -> int:
    review_id = db.create_review(argus_path, "example/echo", 1, "m")
    conn = db.connect(argus_path)
    try:
        cursor = conn.execute(
            "INSERT INTO findings (review_id, file, line, severity, confidence, "
            "title, description, proposed_fix, rule_id, is_bug, verdict_is_real, "
            "verdict_confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (review_id, file, line, "major", 0.8, "bug", "d", "f", None, 1, 1, 0.9),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def test_mark_precision_excludes_auto_marks_by_default(argus_path):
    from argus.api import metrics

    human = _seed_finding(argus_path, "a.py")
    auto = _seed_finding(argus_path, "b.py")
    db.add_mark(argus_path, human, "real", marker="alice")
    db.add_mark(argus_path, auto, "fp", marker=automark.MARKER_JUDGE)

    human_only = metrics.mark_precision(argus_path)
    assert human_only["located"] == 1 and human_only["real"] == 1 and human_only["fp"] == 0

    combined = metrics.mark_precision(argus_path, include_auto=True)
    assert combined["located"] == 2 and combined["fp"] == 1


def test_corpus_load_marks_ignores_auto_marks(argus_path, tmp_path):
    jarvis_path = str(tmp_path / "jarvis.db")
    corpus.init_corpus_db(jarvis_path)
    human = _seed_finding(argus_path, "a.py")
    auto = _seed_finding(argus_path, "b.py")
    db.add_mark(argus_path, human, "real", marker="alice")
    db.add_mark(argus_path, auto, "real", marker=automark.MARKER_PROBE)

    inserted = corpus.CorpusLoader(jarvis_path, argus_path).load_marks()

    assert inserted == 1  # only the human gold mark enters the corpus
