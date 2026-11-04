"""Published finding collection, recovery, and acceptance reports."""

from datetime import timedelta

from argus import acceptance, publish, replies
from argus.api import db
from argus.review import ReviewResult
from argus.schema import Finding, Verdict, VerifiedFinding


HEAD = "a" * 40
NEXT_HEAD = "b" * 40
REPO = "example/app"


def seed_review(path, entries=None, *, repo=REPO, pr=3):
    entries = entries or [{}]
    items = []
    for index, entry in enumerate(entries, 1):
        finding = Finding.model_validate({
            "file": "app.py", "line": 2, "end_line": 2,
            "severity": "major", "confidence": 0.9,
            "title": "Missing input check", "description": "Empty input reaches the parser.",
            "proposed_fix": "Reject empty input before parsing.",
            "category": "input_handling", "evidence": "    return parse(value)",
            **entry,
        })
        items.append(VerifiedFinding(
            finding=finding, verdict=Verdict(is_real=True, confidence=0.95),
            fingerprint=f"fp-{index}",
        ))
    review_id = db.create_review(path, repo, pr, "model-a", head_sha=HEAD)
    db.save_result(path, review_id, ReviewResult(
        raw_count=len(items), bugs=items, all_findings=items, head_sha=HEAD,
        coverage=[{"lane": "llm", "status": "ran", "files": 1, "model": "model-a"}],
    ))
    return review_id, items


def seed_published(path, stamp, entries=None, *, repo=REPO, pr=3):
    review_id, findings = seed_review(path, entries, repo=repo, pr=pr)
    items = [{
        **item.finding.model_dump(mode="json"), "fingerprint": item.fingerprint,
        "placement": "inline", "gh_comment_id": 100 + index,
    } for index, item in enumerate(findings, 1)]
    db.save_publish_outcome(path, review_id, publish.PublishOutcome(
        "posted", comments_posted=len(items), items=items, gh_review_id=20,
        posted_at=stamp, bot_login="publisher",
    ))
    with db.connect(path) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM published_findings WHERE review_id=? ORDER BY id", (review_id,),
        )]


def counters():
    return {"bot": replies.counts(), "rechecks_attempted": 0, "writes_attempted": 0,
            "failures": 0, "reconciled_found": 0, "reconciled_missing": 0}


def test_collect(golden, transport, db_path, frozen_now, monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "0")
    stamp = frozen_now.isoformat()
    seed_published(db_path, stamp, [{}, {"file": "worker.py", "title": "Lost error result"}])
    transport.add("GET", f"/repos/{REPO}/pulls/comments", (200, [
        {"id": 901, "in_reply_to_id": 101, "body": "Fixed by validating input first.",
         "user": {"login": "maintainer"}, "author_association": "MEMBER",
         "created_at": stamp, "updated_at": stamp},
        {"id": 902, "in_reply_to_id": 102, "body": "Rejected because the caller already handles this case.",
         "user": {"login": "maintainer"}, "author_association": "OWNER",
         "created_at": stamp, "updated_at": stamp},
        {"id": 903, "in_reply_to_id": 102, "body": "Fixed", "user": {"login": "helper", "type": "Bot"},
         "created_at": stamp, "updated_at": stamp},
    ], {}))
    transport.add("GET", f"/repos/{REPO}/pulls/3", (200, {
        "state": "open", "head": {"sha": NEXT_HEAD}, "user": {"login": "maintainer"},
    }, {}))
    transport.add("POST", "/graphql", (200, {"data": {"repository": {"pullRequest": {
        "reviewThreads": {"nodes": [
            {"id": "thread-101", "isResolved": True, "comments": {"nodes": [{"databaseId": 101}]}},
            {"id": "thread-102", "isResolved": False, "comments": {"nodes": [{"databaseId": 102}]}},
        ]},
    }}}}, {}))
    transport.add("GET", f"/repos/{REPO}/compare/{HEAD}...{NEXT_HEAD}", (200, {
        "status": "ahead", "files": [{"filename": "app.py", "patch": "@@ -2 +2 @@\n-old\n+new"}],
    }, {}))
    result = acceptance.collect(db_path)
    transport.assert_all_used()
    golden.check("acceptance", "collect", {"returned": result, "stats": acceptance.stats(db_path)})


def test_reconcile(golden, transport, db_path, frozen_now, monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", REPO)
    monkeypatch.setenv("ARGUS_PUBLISH_BOT_LOGIN", "publisher")
    review_id, _ = seed_review(db_path)
    with db.connect(db_path) as conn:
        conn.execute("UPDATE reviews SET created_at=? WHERE id=?",
                     ((frozen_now - timedelta(hours=1)).isoformat(), review_id))
    transport.add("GET", f"/repos/{REPO}/pulls/3/reviews", (200, [{
        "id": 20, "user": {"login": "publisher"}, "submitted_at": frozen_now.isoformat(),
        "body": f"Review completed.\n<!-- argus:review={review_id} head={HEAD} -->",
    }], {}))
    transport.add("GET", f"/repos/{REPO}/pulls/3/comments", (200, [{
        "id": 101, "pull_request_review_id": 20, "user": {"login": "publisher"},
        "body": "Missing input check\n<!-- argus:fp=fp-1 -->", "path": "app.py", "line": 2,
    }], {}))
    run = counters()
    with db.connect(db_path) as conn:
        returned = acceptance._reconcile(conn, db_path, frozen_now, run)
    transport.assert_all_used()
    golden.check("acceptance", "reconcile", {"returned": returned, "counters": run})


def test_report(golden, db_path, frozen_now):
    stamp = frozen_now.isoformat()
    findings = seed_published(db_path, stamp, [
        {}, {"title": "Lost error result"}, {"title": "Unbounded wait"},
        {"title": "Missing summary check"}, {"title": "Untrusted acceptance"},
    ])
    with db.connect(db_path) as conn:
        for item, value in zip(findings[:3], ("fixed", "rejected", "deferred")):
            acceptance._record(conn, item["id"], "reply", value, 900 + item["id"], stamp, stamp,
                               body=value, author_association="MEMBER", author_login="maintainer")
        acceptance._record(conn, findings[3]["id"], "addressed", "true", NEXT_HEAD, stamp)
        conn.execute("UPDATE published_findings SET placement='summary',unverified=1 WHERE id=?",
                     (findings[3]["id"],))
        acceptance._record(conn, findings[4]["id"], "reply", "fixed", 905, stamp, stamp,
                           body="Fixed", author_association="CONTRIBUTOR", author_login="visitor")
        conn.execute("INSERT INTO outcome_prs VALUES (?,?, 'closed',1,?,?,?)",
                     (REPO, 3, stamp, NEXT_HEAD, stamp))
        report = acceptance.report(conn, since="2026-01-01T00:00:00+00:00")
    golden.check("acceptance", "report", report)
