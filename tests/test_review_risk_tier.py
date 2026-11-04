import pytest

from argus import client, review
from argus.diff import FileList, FilePatch
from argus.jarvis import live
from argus.schema import Review


def test_fast_review_labels_tripwire_without_any_model_client(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise AssertionError("model client unavailable")

    monkeypatch.setattr(review, "complete_tool", unavailable)
    monkeypatch.setattr(client, "complete_tool", unavailable)
    monkeypatch.setattr(client, "build_client", unavailable)
    monkeypatch.setattr(client, "_openai_request", unavailable)
    monkeypatch.setattr(client, "_openrouter_request", unavailable)
    monkeypatch.setattr("argus.jarvis.automark.judge_finding", unavailable)
    files = FileList([FilePatch("deploy/docker-compose.prod.yml", "modified", "@@ -1 +1 @@\n+x")])
    result = review.run_diff_review(files, fast=True)
    assert result.risk_tier["tier"] == "risky"
    assert result.risk_tier["changed_lines"] == 1
    assert result.risk_tier["reasons"] == [
        "tripwire path deploy/docker-compose.prod.yml (docker-compose*)"
    ]


@pytest.mark.parametrize("source", ["pr", "diff-fast", "diff-full"])
@pytest.mark.parametrize("fault", [False, True])
def test_every_review_return_labels_existing_files_and_isolates_classifier_faults(
    monkeypatch, caplog, source, fault,
):
    files = FileList([FilePatch("src/app.py", "removed", None, additions=0, deletions=301)])
    monkeypatch.setattr(review, "fetch_pr_files", lambda *_: files)
    monkeypatch.setattr(live, "_pr_meta", lambda *_: {"head_sha": "head"})
    monkeypatch.setattr(live, "_fetch_file_at", lambda *_: None)
    monkeypatch.setattr(review, "_deep_review", lambda *_: Review())
    if fault:
        def broken(_files):
            raise RuntimeError("classifier failed")

        monkeypatch.setattr(review.risk_tier, "classify_files", broken)
        monkeypatch.setattr(review.log, "handlers", [caplog.handler])
    if source == "pr":
        result = review.run_review("o/r", 1)
    else:
        result = review.run_diff_review(files, fast=source == "diff-fast")
    assert result.raw_count == 0
    if fault:
        assert result.risk_tier is None
        assert "classifier failed" in caplog.text
    else:
        assert result.risk_tier["tier"] == "risky"
        assert result.risk_tier["changed_lines"] == 301
        assert result.risk_tier["reasons"] == ["301 changed lines exceed the 300 line limit"]
