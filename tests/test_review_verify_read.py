"""Both review entry points hand the verifier the same cached reader they give the reviewer."""

from argus import review
from argus.diff import FilePatch
from argus.jarvis import live
from argus.schema import Finding, Review, Severity, Verdict

FILES = [FilePatch("src/app.py", "modified", "@@ -1,2 +1,3 @@\n import httpx\n+resp = httpx.get(url)\n print(resp)\n")]
FINDING = Finding(file="src/app.py", line=2, severity=Severity.major, confidence=0.7,
                  title="no timeout", description="d", proposed_fix="add timeout")
HEAD_TEXT = "import httpx\nresp = httpx.get(url)\nprint(resp)\n"


def _spy(monkeypatch):
    seen = []

    def verify(finding, diff, model, **kwargs):
        seen.append(kwargs)
        return Verdict(is_real=True, confidence=0.9, reasoning="r")

    monkeypatch.setattr(review, "_verify", verify)
    monkeypatch.setattr(review, "_deep_review", lambda *args, **kwargs: Review(findings=[FINDING]))
    return seen


def test_run_diff_review_passes_its_reader_to_the_verifier(monkeypatch):
    seen = _spy(monkeypatch)
    review.run_diff_review(FILES, repo=None, file_loader=lambda path: HEAD_TEXT, verify_variant="shipped")
    assert len(seen) == 1 and seen[0]["variant"] == "shipped"
    assert seen[0]["read"]("src/app.py") == HEAD_TEXT


def test_run_review_passes_its_reader_to_the_verifier(monkeypatch):
    seen = _spy(monkeypatch)
    monkeypatch.setattr(review, "fetch_pr_files", lambda repo, pr: FILES)
    monkeypatch.setattr(live, "_pr_meta", lambda repo, pr: {"head_sha": "abc"})
    monkeypatch.setattr(live, "_fetch_file_at", lambda repo, path, ref: HEAD_TEXT)
    monkeypatch.setattr(review, "fetch_house_rules", lambda repo: None)
    monkeypatch.setattr(review, "_intent_for_prompt", lambda *args: None)
    monkeypatch.setattr(review, "_suppress_known_fps", lambda repo, pr, findings: findings)
    no_lane = ([], [], {"lane": "probe", "status": "skipped"})
    monkeypatch.setattr(review, "_probe_lane", lambda *args: no_lane)
    monkeypatch.setattr(review, "_ruff_lane", lambda *args: ([], {"lane": "ruff", "status": "skipped"}))
    monkeypatch.setattr(review, "_config_lane", lambda *args: ([], {"lane": "config", "status": "skipped"}))
    monkeypatch.setattr(review, "_judge_lane", lambda *args: {"lane": "judge", "status": "skipped"})
    review.run_review("owner/repo", 1)
    assert len(seen) == 1
    assert seen[0]["read"]("src/app.py") == HEAD_TEXT
