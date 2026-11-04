"""Stored review placement checks and publishing previews."""

import sys

from argus import cli, publish
from argus.api import db
from argus.review import ReviewResult
from argus.schema import Finding, Severity, Verdict, VerifiedFinding

HEAD = "a" * 40
PATCH = "@@ -9,3 +9,4 @@\n context\n-old\n+new\n+extra\n tail"


def stored_review(path):
    review_id = db.create_review(path, "example/app", 42, "model-a", head_sha=HEAD)
    items = [VerifiedFinding(
        finding=Finding(file="src/app.py", line=line, end_line=end, severity=Severity.major,
                        confidence=0.9, category="contracts", title=title,
                        description="An empty input reaches storage.",
                        evidence="save(value)", proposed_fix="Reject empty input before saving."),
        verdict=Verdict(is_real=True, confidence=0.9), fingerprint=fp, lane="llm",
    ) for fp, line, end, title in [
        ("fp-inline", 10, 11, "Validate input before saving"),
        ("fp-stale", 100, None, "Check the changed location"),
        ("fp-summary", 200, None, "Check the fallback value"),
    ]]
    db.save_result(path, review_id, ReviewResult(
        raw_count=3, bugs=items, nits=[], head_sha=HEAD,
        coverage=[{"lane": "llm", "status": "ran", "files_reviewed": 1, "files_omitted": 0}],
    ))
    return review_id


def register_files(transport):
    transport.add("GET", "/repos/example/app/pulls/42/files", (200, [
        {"filename": "src/app.py", "status": "modified", "patch": PATCH,
         "additions": 2, "deletions": 1},
        {"filename": "src/removed.py", "status": "removed", "patch": "@@ -1 +0,0 @@\n-old"},
    ], {}), query={"per_page": "100", "page": "1"})


def test_publish_placement(golden, transport, db_path, monkeypatch, capsys):
    review_id = stored_review(db_path)
    findings = db.get_findings(db_path, review_id)
    items = [{**item, "placement": placement, "gh_comment_id": comment_id,
              "unverified": unverified}
             for item, placement, comment_id, unverified in zip(
                 findings, ("inline", "inline", "summary"), (801, 802, None), (False, False, True),
                 strict=True,
             )]
    db.save_publish_outcome(db_path, review_id, publish.PublishOutcome(
        "posted", comments_posted=2, items=items, gh_review_id=301,
        bot_login="example-bot", posted_at="2026-01-15T00:00:00Z",
    ))
    register_files(transport)
    monkeypatch.setattr(sys, "argv", ["argus", "publish-placement", str(review_id), "--db", db_path])

    cli.main()
    output = capsys.readouterr()

    assert "commentable=yes" in output.out
    assert "commentable=no" in output.out
    assert "unverified" in output.out
    transport.assert_all_used()
    golden.check("cli-publish-placement", "placement", {
        "stdout": output.out.splitlines(), "stderr": output.err.splitlines(),
    })


def test_publish_preview(golden, transport, db_path, monkeypatch, capsys):
    review_id = stored_review(db_path)
    register_files(transport)
    monkeypatch.setattr(sys, "argv", ["argus", "publish-preview", str(review_id), "--db", db_path])

    cli.main()
    output = capsys.readouterr()

    assert "**Argus review**" in output.out
    assert "src/app.py:10-11 (RIGHT)" in output.out
    assert db.get_review(db_path, review_id)["publish_status"] is None
    transport.assert_all_used()
    golden.check("cli-publish-placement", "preview", {
        "stdout": output.out.splitlines(), "stderr": output.err.splitlines(),
    })
