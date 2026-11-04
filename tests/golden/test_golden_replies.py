"""Guarded thread replies and later-head finding checks."""

import base64

from argus import acceptance, replies
from argus.api import db

from .test_golden_acceptance import HEAD, NEXT_HEAD, REPO, counters, seed_published


def _enable(monkeypatch, path):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", REPO)
    with db.connect(path) as conn:
        conn.execute("INSERT INTO config(id,repo_allowlist,model,bug_cap,min_confidence) "
                     "VALUES (1,?,'model-a',10,0.5) ON CONFLICT(id) DO UPDATE SET model='model-a'",
                     ('["example/app"]',))


def _thread(path, stamp):
    finding = seed_published(path, stamp)[0]
    finding.update(gh_thread_id="thread-101", thread_resolved=False)
    with db.connect(path) as conn:
        conn.execute("UPDATE published_findings SET gh_thread_id=? WHERE id=?", ("thread-101", finding["id"]))
    return finding


def test_recheck_fixed_resolves(golden, transport, provider, db_path, frozen_now, monkeypatch):
    _enable(monkeypatch, db_path)
    stamp = frozen_now.isoformat()
    finding = _thread(db_path, stamp)
    transport.add("GET", f"/repos/{REPO}/pulls/comments", (200, [
        {"id": 901, "in_reply_to_id": 101, "body": "Re-check please after the latest update.",
         "user": {"login": "maintainer"}, "author_association": "MEMBER",
         "created_at": stamp, "updated_at": stamp},
        {"id": 902, "in_reply_to_id": 101, "body": "Fixed in the latest commit.",
         "user": {"login": "maintainer"}, "author_association": "OWNER",
         "created_at": stamp, "updated_at": stamp},
        {"id": 903, "in_reply_to_id": 101, "body": "Fixed", "user": {"login": "helper", "type": "Bot"},
         "created_at": stamp, "updated_at": stamp},
    ], {}))
    comparison = {"status": "ahead", "files": [{
        "filename": "app.py", "status": "modified", "additions": 1, "deletions": 1,
        "patch": "@@ -1,3 +1,3 @@\n def parse_input(value):\n-    return parse(value)\n+    return parse(value) if value else None\n ",
    }]}
    for head, content in ((NEXT_HEAD, "def parse_input(value):\n    return parse(value) if value else None\n\n"),
                          (HEAD, "def parse_input(value):\n    return parse(value)\n\n")):
        transport.add("GET", f"/repos/{REPO}/contents/app.py", (200, {
            "encoding": "base64", "content": base64.b64encode(content.encode()).decode(),
        }, {}), query={"ref": head})
    provider.register("recheck_finding", {
        "verdict": "fixed", "quote": "    return parse(value) if value else None",
    })
    transport.add("POST", f"/repos/{REPO}/pulls/3/comments/101/replies", (201, {"id": 910}, {}))
    transport.add("POST", "/graphql", (200, {"data": {"resolveReviewThread": {
        "thread": {"id": "thread-101", "isResolved": True},
    }}}, {}))
    run = counters()
    with db.connect(db_path) as conn:
        acceptance._replies(conn, REPO, [finding], "2026-01-01T00:00:00+00:00", stamp)
        returned = replies.run(conn, REPO, [finding], NEXT_HEAD,
                               {(REPO, HEAD, NEXT_HEAD): comparison}, stamp, run)
    transport.assert_all_used()
    golden.check("replies", "recheck-fixed-resolves", {"returned": returned, "counters": run})


def test_no_change_no_reply(golden, transport, db_path, frozen_now, monkeypatch):
    _enable(monkeypatch, db_path)
    stamp = frozen_now.isoformat()
    finding = _thread(db_path, stamp)
    transport.add("GET", f"/repos/{REPO}/pulls/comments", (200, [
        {"id": 903, "in_reply_to_id": 101, "body": "Fixed", "user": {"login": "helper", "type": "Bot"},
         "created_at": stamp, "updated_at": stamp},
    ], {}))
    with db.connect(db_path) as conn:
        acceptance._replies(conn, REPO, [finding], "2026-01-01T00:00:00+00:00", stamp)
        run = counters()
        returned = replies.run(conn, REPO, [finding], HEAD, {}, stamp, run)
    transport.assert_all_used()
    golden.check("replies", "no-change-no-reply", {"returned": returned, "counters": run})
