"""Refuted serious findings stay visible without changing the verdict."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from argus import acceptance, cli, diff, github, publish, verdict
from argus.api import db, main
from argus.review import ReviewResult
from argus.schema import Finding, Severity, Verdict, VerifiedFinding
from test_publish_markers import legacy_review

HEAD = "a" * 40
FILES = [diff.FilePatch("app.py", "modified", "@@ -10 +10 @@\n-old\n+new")]
HEADING = "### Unverified (the verifier disagreed)"
EXPLANATION = (
    "These findings were refuted by a second check. They are listed for a human "
    "decision and do not affect the verdict."
)


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "argus.db")
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    monkeypatch.setenv("ARGUS_PUBLISH", "dry-run")
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "owner/repo")
    monkeypatch.setenv("ARGUS_PUBLISH_TOKEN", "write-secret")
    monkeypatch.setenv("ARGUS_PUBLISH_BOT_LOGIN", "publisher")
    monkeypatch.setenv("ARGUS_PUBLISH_MAX_COMMENTS", "1")
    monkeypatch.setenv("ARGUS_PUBLISH_MIN_SEVERITY", "minor")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("Unexpected HTTP"))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("Unexpected HTTP"))
    monkeypatch.setattr(diff, "fetch_pr_files", lambda *a: FILES)
    monkeypatch.setattr(cli, "fetch_pr_files", lambda *a: FILES)
    db.init_db(path)
    return path


def finding(fp, *, severity="major", lane="llm", real=False, line=10):
    return VerifiedFinding(
        finding=Finding(file="app.py", line=line, end_line=line, severity=Severity(severity),
                        category="contracts", confidence=0.9, title=fp, description="Unchecked input",
                        evidence="accept(value)", proposed_fix="Validate input"),
        verdict=Verdict(is_real=real, confidence=0.9), lane=lane, fingerprint=fp,
    )


def store(path, raw, survivors=(), head=HEAD):
    rid = db.create_review(path, "owner/repo", 3, "test", head_sha=head)
    db.save_result(path, rid, ReviewResult(
        raw_count=len(raw), bugs=list(survivors), all_findings=list(raw),
        coverage=[{"lane": "llm", "status": "ran", "files_reviewed": 1}],
    ))
    return rid


def document(path, rid):
    return verdict.document(db.get_review(path, rid), db.get_findings(path, rid),
                            unverified=db.get_unverified_findings(path, rid))


def rows(path):
    with db.connect(path) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM published_findings ORDER BY id")]


@pytest.mark.parametrize("survives", [False, True])
def test_document_filters_raw_findings_without_changing_decision(database, survives):
    survivors = [finding("survivor", real=True)] if survives else []
    raw = [finding("major"), finding("critical", severity="critical"),
           finding("minor", severity="minor"), finding("nit", severity="nit"),
           finding("probe", lane="probe"), finding("confirmed", real=True)]
    if survives:
        raw.append(finding("survivor"))
    rid = store(database, raw, survivors)
    store(database, [finding("other-review")])
    original = verdict.document(db.get_review(database, rid), db.get_findings(database, rid))
    doc = document(database, rid)
    assert {f["fingerprint"] for f in doc["unverified"]} == {"major", "critical"}
    assert doc["findings"] == original["findings"]
    assert doc["counts"] == original["counts"]
    assert doc["verdict"] == original["verdict"] == ("changes_requested" if survives else "approve")
    assert doc["counts"]["confirmed"] == len(survivors)
    item = next(f for f in doc["unverified"] if f["fingerprint"] == "major")
    assert (item["file"], item["line"], item["end_line"], item["category"], item["evidence"],
            item["proposed_fix"], item["confidence"], item["evidence_tier"]) == (
                "app.py", 10, 10, "contracts", "accept(value)", "Validate input", 0.9, "single_critic")
    assert "unverified" not in original


def test_body_order_markers_and_inline_budget(database):
    survivors = [finding("inline", real=True), finding("overflow", real=True),
                 finding("outside", real=True, line=100)]
    rid = store(database, [finding("refuted", severity="critical")], survivors)
    doc = document(database, rid)
    payload = publish.render_review(HEAD, rid, doc, FILES, max_comments=1)
    body = payload["body"]
    assert body.index("### Outside the diff") < body.index("### Comment limit") < body.index(HEADING)
    assert f"{HEADING}\n\n{EXPLANATION}" in body
    assert "<!-- argus:ufp=refuted -->" in body
    assert len(payload["comments"]) == 1
    assert "<!-- argus:fp=inline -->" in payload["comments"][0]["body"]
    assert "refuted" not in payload["comments"][0]["body"]
    assert "Critical: 0" in body and "Major: 3" in body and "single_critic: 3" in body
    skipped = publish.render_review(HEAD, rid, doc, FILES, posted_fingerprints={"refuted"})
    assert HEADING not in skipped["body"]
    assert "Already posted: 1" in skipped["body"]


def test_critical_threshold_hides_unverified_major(database, monkeypatch):
    rid = store(database, [finding("refuted")])
    monkeypatch.setenv("ARGUS_PUBLISH_MIN_SEVERITY", "critical")
    outcome = publish.publish_review("owner/repo", 3, HEAD, rid, document(database, rid), FILES)
    assert HEADING not in outcome.payload["body"]
    assert not outcome.payload["comments"] and not outcome.items


def test_completed_review_dry_run_and_preview(database, capsys):
    rid = store(database, [finding("refuted")])
    main._publish_completed_review(database, rid, "owner/repo", 3)
    main._publish_completed_review(database, rid, "owner/repo", 3)
    assert [(r["placement"], r["unverified"], r["mode"]) for r in rows(database)] == [
        ("summary", 1, "dry_run")]
    cli._run_publish_preview(rid, database)
    assert HEADING in capsys.readouterr().out
    cli._run_publish_placement(rid, database)
    line = capsys.readouterr().out
    assert "summary" in line and "app.py:10" in line and "unverified" in line


@pytest.mark.parametrize("fallback", [False, True])
def test_only_new_unverified_posts_then_dedupes_next_head(database, monkeypatch, fallback):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    old = f"<!-- argus:review=99 head={'f' * 40} -->"
    remote = [{"id": 99, "user": {"login": "publisher"}, "body": old}]
    attempts = []
    head = HEAD

    def handler(request):
        if request.method == "POST":
            payload = json.loads(request.content)
            attempts.append(payload)
            if fallback and len(attempts) == 1:
                return httpx.Response(422, json={})
            posted = {"id": 70, "body": payload["body"], "user": {"login": "publisher"},
                      "submitted_at": db._now_iso()}
            remote.append(posted)
            return httpx.Response(200, json=posted)
        if request.url.path.endswith("/3"):
            return httpx.Response(200, json={"head": {"sha": head}})
        return httpx.Response(200, json=remote if request.url.path.endswith("/reviews") else [])

    rid = store(database, [finding("refuted")])
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        outcome = publish.publish_review("owner/repo", 3, head, rid, document(database, rid), FILES, http=http)
        assert outcome.status == "posted" and outcome.comments_posted == 0
        db.save_publish_outcome(database, rid, outcome)
        assert [(r["placement"], r["unverified"], r["mode"]) for r in rows(database)] == [
            ("summary", 1, "posted")]
        recovered = publish.publish_review("owner/repo", 3, head, rid, document(database, rid), FILES, http=http)
        assert recovered.status == "posted" and recovered.items[0]["unverified"]
        head = "b" * 40
        next_rid = store(database, [finding("refuted")], head=head)
        repeated = publish.publish_review("owner/repo", 3, head, next_rid,
                                          document(database, next_rid), FILES, http=http)
        assert repeated.status == "skipped" and repeated.reason == "nothing new"
        assert repeated.already_posted == 1
        empty = store(database, [], head=head)
        unchanged = publish.publish_review("owner/repo", 3, head, empty, document(database, empty), FILES, http=http)
        assert unchanged.status == "skipped" and unchanged.reason == "nothing new"
    assert len(attempts) == (2 if fallback else 1)
    assert all(HEADING in p["body"] and not p["comments"] for p in attempts)


def test_reconciliation_recovers_unverified(database, monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    rid = store(database, [finding("refuted")])
    now = datetime.now(timezone.utc)
    with db.connect(database) as conn:
        conn.execute("UPDATE reviews SET created_at=? WHERE id=?",
                     ((now - timedelta(minutes=5)).isoformat(), rid))
    remote = {"id": 70, "body": legacy_review(f"<!-- argus:review={rid} head={HEAD} -->", "refuted", "ufp"),
              "submitted_at": now.isoformat(), "user": {"login": "publisher"}}
    monkeypatch.setattr(github, "list_cached", lambda url, params: [remote] if url.endswith("reviews") else [])
    run = {"reconciled_found": 0, "reconciled_missing": 0}
    with db.connect(database) as conn:
        acceptance._reconcile(conn, database, now, run)
    assert run["reconciled_found"] == 1
    assert [(r["placement"], r["unverified"], r["gh_review_id"]) for r in rows(database)] == [
        ("summary", 1, 70)]


def test_acceptance_counts_unverified_within_summary(database):
    rid = store(database, [finding("refuted"), finding("unconfirmed")], [finding("inline", real=True)])
    doc = document(database, rid)
    payload = publish.render_review(HEAD, rid, doc, FILES)
    items = publish._placed_items(doc, payload)
    db.save_publish_outcome(database, rid, publish.PublishOutcome("posted", items=items))
    db.save_publish_outcome(database, rid, publish.PublishOutcome("dry_run", items=items))
    with db.connect(database) as conn:
        fid = conn.execute("SELECT id FROM published_findings WHERE fingerprint='refuted' AND mode='posted'").fetchone()[0]
        acceptance._record(conn, fid, "addressed", "true", "new-head", db._now_iso())
        conn.execute("INSERT INTO outcome_prs VALUES ('owner/repo',3,'closed',1,?,?,?)",
                     (db._now_iso(), HEAD, db._now_iso()))
        report = acceptance.report(conn)
    for group in (report, report["final"]):
        assert group["unverified"]["posted"] == 2
        assert group["unverified"]["loose_accepted"] == 1
        assert group["unverified"]["unconfirmed"] == 1
        assert group["by_placement"]["summary"] == group["unverified"]
        assert group["by_placement"]["inline"]["posted"] == 1
        assert group["totals"]["posted"] == 3
    assert "Unverified" in report["note"] and "inline comparison" in report["note"]


def test_existing_published_table_migrates_twice(database):
    rid = store(database, [], [finding("inline", real=True)])
    main._publish_completed_review(database, rid, "owner/repo", 3)
    with sqlite3.connect(database) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(published_findings)")}
        if "unverified" in columns:
            conn.execute("ALTER TABLE published_findings DROP COLUMN unverified")
    before = rows(database)
    db.init_db(database)
    db.init_db(database)
    after = rows(database)
    assert after == [dict(r, unverified=0) for r in before]
    with db.connect(database) as conn:
        column = next(r for r in conn.execute("PRAGMA table_info(published_findings)") if r["name"] == "unverified")
    assert (column["type"], column["notnull"], column["dflt_value"]) == ("INTEGER", 1, "0")


@pytest.mark.parametrize("first_real,second_real", [(False, True), (False, False), (True, False)])
@pytest.mark.parametrize("max_comments", [0, 1])
def test_fingerprint_state_across_heads(database, monkeypatch, first_real, second_real, max_comments):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    monkeypatch.setenv("ARGUS_PUBLISH_MAX_COMMENTS", str(max_comments))
    remote, comments, attempts = [], [], []
    head = HEAD

    def handler(request):
        if request.method == "POST":
            payload = json.loads(request.content)
            attempts.append(payload)
            posted = {"id": 70 + len(remote), "body": payload["body"],
                      "user": {"login": "publisher"}, "submitted_at": db._now_iso()}
            comments.extend({**c, "user": {"login": "publisher"}, "id": 100 + len(comments), "pull_request_review_id": posted["id"]}
                            for c in payload["comments"])
            remote.append(posted)
            return httpx.Response(200, json=posted)
        if request.url.path.endswith("/3"):
            return httpx.Response(200, json={"head": {"sha": head}})
        return httpx.Response(200, json=remote if request.url.path.endswith("/reviews") else comments)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        item = finding("X", real=first_real)
        rid = store(database, [item], [item] if first_real else [])
        first = publish.publish_review("owner/repo", 3, head, rid, document(database, rid), FILES, http=http)
        assert first.status == "posted"
        if not first_real:
            assert "<!-- argus:ufp=X -->" in first.payload["body"]
            assert "<!-- argus:fp=X -->" not in first.payload["body"]
        head = "b" * 40
        item = finding("X", real=second_real)
        rid = store(database, [item], [item] if second_real else [], head=head)
        second = publish.publish_review("owner/repo", 3, head, rid, document(database, rid), FILES, http=http)
    if second_real:
        assert second.status == "posted" and second.already_posted == 0
        assert len(attempts) == 2
        assert "Verdict: changes_requested" in second.payload["body"]
        assert second.comments_posted == max_comments
        rendered = second.payload["comments"][0]["body"] if max_comments else second.payload["body"]
        assert "<!-- argus:fp=X -->" in rendered
        assert second.items[0]["placement"] == ("inline" if max_comments else "summary")
        assert not second.items[0].get("unverified")
    else:
        assert second.status == "skipped" and second.reason == "nothing new"
        assert second.already_posted == 1 and len(attempts) == 1
        assert HEADING not in second.payload["body"]


@pytest.mark.parametrize("mode", ["dry_run", "posted"])
def test_latest_rendering_updates_placement_and_review(database, mode):
    for real in (True, False, True):
        item = finding("X", real=real)
        rid = store(database, [item], [item] if real else [])
        doc = document(database, rid)
        payload = publish.render_review(HEAD, rid, doc, FILES)
        outcome = publish.PublishOutcome(mode, items=publish._placed_items(doc, payload))
        db.save_publish_outcome(database, rid, outcome)
        saved = rows(database)
        assert len(saved) == 1
        assert (saved[0]["placement"], saved[0]["unverified"], saved[0]["review_id"]) == (
            "inline" if real else "summary", int(not real), rid)


def test_late_older_review_does_not_replace_newer_published_finding(database):
    old_item = finding("X")
    old_rid = store(database, [old_item])
    old_doc = document(database, old_rid)
    old_payload = publish.render_review(HEAD, old_rid, old_doc, FILES)
    old_outcome = publish.PublishOutcome(
        "posted", items=publish._placed_items(old_doc, old_payload))
    db.save_publish_outcome(database, old_rid, old_outcome)

    new_item = finding("X", severity="critical", real=True)
    new_rid = store(database, [new_item], [new_item])
    new_doc = document(database, new_rid)
    new_payload = publish.render_review(HEAD, new_rid, new_doc, FILES)
    db.save_publish_outcome(
        database, new_rid,
        publish.PublishOutcome("posted", items=publish._placed_items(new_doc, new_payload)))

    db.save_publish_outcome(database, old_rid, old_outcome)

    saved = rows(database)
    assert len(saved) == 1
    assert (saved[0]["placement"], saved[0]["unverified"], saved[0]["review_id"],
            saved[0]["severity"], saved[0]["line"]) == ("inline", 0, new_rid, "critical", 10)


def test_report_reads_copy_without_unverified_column(database, tmp_path):
    rid = store(database, [], [finding("inline", real=True)])
    doc = document(database, rid)
    payload = publish.render_review(HEAD, rid, doc, FILES)
    db.save_publish_outcome(database, rid, publish.PublishOutcome("posted", items=publish._placed_items(doc, payload)))
    copy = tmp_path / "old.db"
    with sqlite3.connect(database) as source, sqlite3.connect(copy) as target:
        source.backup(target)
        target.execute("ALTER TABLE published_findings DROP COLUMN unverified")
    with sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        report = acceptance.report(conn)
    stats = acceptance.stats(copy)
    for result in (report, stats):
        assert result["totals"]["posted"] == 1
        assert result["unverified"]["posted"] == 0
        assert result["by_placement"]["inline"]["posted"] == 1
    with sqlite3.connect(copy) as conn:
        assert "unverified" not in {r[1] for r in conn.execute("PRAGMA table_info(published_findings)")}


def test_publishing_document_includes_loaded_empty_list(database):
    rid = store(database, [])
    assert document(database, rid)["unverified"] == []


@pytest.mark.parametrize("outside_only", [False, True])
def test_unverified_limit_records_only_listed_items(database, outside_only):
    raw = [finding(f"item-{n}", severity="critical" if n < 2 else "major") for n in range(12)]
    rid = store(database, raw)
    doc = document(database, rid)
    for n, item in enumerate(doc["unverified"]):
        item["confidence"] = n / 12
    payload = publish.render_review(HEAD, rid, doc, FILES, outside_only=outside_only)
    body = payload["body"]
    assert body.count("<!-- argus:ufp=") == 10
    assert "2 more unverified findings are not listed." in body
    expected = ["item-1", "item-0", "item-11", "item-10", "item-9", "item-8", "item-7", "item-6", "item-5", "item-4"]
    positions = [body.index(f"<!-- argus:ufp={fp} -->") for fp in expected]
    assert positions == sorted(positions)
    assert "<!-- argus:ufp=item-2 -->" not in body and "<!-- argus:ufp=item-3 -->" not in body
    assert not payload["comments"]
    items = publish._placed_items(doc, payload)
    db.save_publish_outcome(database, rid, publish.PublishOutcome("posted", items=items))
    assert {r["fingerprint"] for r in rows(database)} == set(expected)
    assert all(r["unverified"] and r["placement"] == "summary" for r in rows(database))


def test_same_review_retry_recovers_capped_post_before_new_findings(database, monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    rid = store(database, [finding(f"item-{n}") for n in range(12)], [finding("confirmed", real=True)])
    doc = document(database, rid)
    remote, comments, posts = [], [], []

    def handler(request):
        if request.method == "POST":
            payload = json.loads(request.content)
            posts.append(payload)
            review = {"id": 70, "body": payload["body"], "user": {"login": "publisher"},
                      "submitted_at": "2026-10-16T10:00:00Z"}
            remote.append(review)
            comments.extend({**c, "user": {"login": "publisher"}, "id": 502, "pull_request_review_id": 70} for c in payload["comments"])
            return httpx.Response(200, json=review)
        if request.url.path.endswith("/3"):
            return httpx.Response(200, json={"head": {"sha": HEAD}})
        return httpx.Response(200, json=remote if request.url.path.endswith("/reviews") else comments)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        first = publish.publish_review("owner/repo", 3, HEAD, rid, doc, FILES, http=http)
        recovered = publish.publish_review("owner/repo", 3, HEAD, rid, doc, FILES, http=http)
    assert first.status == recovered.status == "posted"
    assert len(posts) == 1
    assert recovered.reason == "recovered an existing review"
    assert recovered.gh_review_id == 70 and recovered.bot_login == "publisher"
    assert recovered.posted_at == "2026-10-16T10:00:00Z"
    assert recovered.comments_posted == 1 and recovered.already_posted == 11
    assert recovered.items == first.items and len(recovered.items) == 11
    assert recovered.payload["body"] == posts[0]["body"]
    db.save_publish_outcome(database, rid, recovered)
    assert len(rows(database)) == 11


@pytest.mark.parametrize("mode", ["dry_run", "posted"])
def test_rerender_replaces_all_stored_fields_and_clears_old_ids(database, mode):
    rid = store(database, [finding("X")])
    doc = document(database, rid)
    payload = publish.render_review(HEAD, rid, doc, FILES)
    db.save_publish_outcome(database, rid, publish.PublishOutcome(
        mode, items=publish._placed_items(doc, payload), gh_review_id=70, posted_at="2026-10-14T10:00:00Z"))

    item = finding("X", real=True, severity="critical", line=11)
    rid = store(database, [item], [item])
    doc = document(database, rid)
    doc["findings"][0].update(file="renamed.py", end_line=12, category="security", title="Changed check")
    files = [diff.FilePatch("renamed.py", "modified", "@@ -11,2 +11,2 @@\n+new\n+tail")]
    payload = publish.render_review(HEAD, rid, doc, files)
    items = publish._placed_items(doc, payload)
    items[0]["gh_comment_id"] = 502
    db.save_publish_outcome(database, rid, publish.PublishOutcome(
        mode, items=items, posted_at="2026-10-15T10:00:00Z"))
    saved = rows(database)
    assert len(saved) == 1
    assert {key: saved[0][key] for key in (
        "review_id", "placement", "unverified", "severity", "category", "path", "line", "end_line",
        "title", "posted_at", "gh_review_id", "gh_comment_id",
    )} == {
        "review_id": rid, "placement": "inline", "unverified": 0, "severity": "critical",
        "category": "security", "path": "renamed.py", "line": 11, "end_line": 12,
        "title": "Changed check", "posted_at": "2026-10-15T10:00:00Z",
        "gh_review_id": None, "gh_comment_id": 502,
    }

    db.save_publish_outcome(database, rid, publish.PublishOutcome(
        mode, items=items, gh_review_id=71, posted_at="2026-10-15T11:00:00Z"))
    payload = publish.render_review(HEAD, rid, doc, files, outside_only=True)
    db.save_publish_outcome(database, rid, publish.PublishOutcome(
        mode, items=publish._placed_items(doc, payload), posted_at="2026-10-16T10:00:00Z"))
    saved = rows(database)
    assert len(saved) == 1
    assert (saved[0]["placement"], saved[0]["gh_review_id"], saved[0]["gh_comment_id"], saved[0]["posted_at"]) == (
        "summary", None, None, "2026-10-16T10:00:00Z")


@pytest.mark.parametrize("placement", ["inline", "summary"])
def test_rerender_same_placement_keeps_missing_remote_ids(database, placement):
    item = finding("X", real=True)
    rid = store(database, [item], [item])
    doc = document(database, rid)
    payload = publish.render_review(HEAD, rid, doc, FILES, outside_only=placement == "summary")
    items = publish._placed_items(doc, payload)
    if placement == "inline":
        items[0]["gh_comment_id"] = 502
    db.save_publish_outcome(database, rid, publish.PublishOutcome("posted", items=items, gh_review_id=70))
    db.save_publish_outcome(database, rid, publish.PublishOutcome("posted", items=publish._placed_items(doc, payload)))
    saved = rows(database)
    assert len(saved) == 1
    assert (saved[0]["gh_review_id"], saved[0]["gh_comment_id"]) == (70, 502 if placement == "inline" else None)


@pytest.mark.parametrize("total", [10, 12])
@pytest.mark.parametrize("fallback", [False, True])
def test_body_budget_omits_long_unverified_items_and_still_posts_confirmed(database, monkeypatch, total, fallback):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    rid = store(database, [finding(f"item-{n}") for n in range(total)], [finding("confirmed", real=True)])
    doc = document(database, rid)
    for item in doc["unverified"]:
        item["evidence"] = "x" * 24_000
    posts = []

    def handler(request):
        if request.method == "POST":
            payload = json.loads(request.content)
            posts.append(payload)
            if fallback and len(posts) == 1:
                return httpx.Response(422, json={})
            return httpx.Response(200, json={"id": 70, "body": payload["body"]})
        body = {"head": {"sha": HEAD}} if request.url.path.endswith("/3") else []
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        outcome = publish.publish_review("owner/repo", 3, HEAD, rid, doc, FILES, http=http)
    assert outcome.status == "posted"
    assert len(posts) == (2 if fallback else 1)
    for payload in posts:
        body = payload["body"]
        assert len(body) <= 60_000
        assert body.count("<!-- argus:ufp=") == 2
        assert "<!-- argus:ufp=item-0 -->" in body and "<!-- argus:ufp=item-1 -->" in body
        assert f"{total - 2} more unverified findings are not listed." in body
    assert {item["fingerprint"] for item in outcome.items} == {"confirmed", "item-0", "item-1"}
    confirmed = next(item for item in outcome.items if item["fingerprint"] == "confirmed")
    assert confirmed["placement"] == ("summary" if fallback else "inline")
    db.save_publish_outcome(database, rid, outcome)
    assert {r["fingerprint"] for r in rows(database)} == {"confirmed", "item-0", "item-1"}


@pytest.mark.parametrize("earlier_review,confirmed_line,expected_status", [
    (True, None, "skipped"),
    (True, 10, "posted"),
    (True, 100, "posted"),
    (False, None, "posted"),
])
def test_publish_uses_rendered_payload_when_unverified_item_exceeds_budget(
    database, monkeypatch, earlier_review, confirmed_line, expected_status,
):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    head = "b" * 40
    survivors = [finding("confirmed", real=True, line=confirmed_line)] if confirmed_line else []
    rid = store(database, [finding("omitted")], survivors, head=head)
    doc = document(database, rid)
    doc["unverified"][0]["evidence"] = "x" * 60_000
    remote = [{"id": 70, "user": {"login": "publisher"}, "body": f"<!-- argus:review=999 head={HEAD} -->"}] if earlier_review else []
    comments, posts = [], []

    def handler(request):
        if request.method == "POST":
            payload = json.loads(request.content)
            posts.append(payload)
            review = {"id": 71, "user": {"login": "publisher"}, "body": payload["body"]}
            remote.append(review)
            comments.extend({**c, "user": {"login": "publisher"}, "id": 502, "pull_request_review_id": 71} for c in payload["comments"])
            return httpx.Response(200, json=review)
        if request.url.path.endswith("/3"):
            return httpx.Response(200, json={"head": {"sha": head}})
        return httpx.Response(200, json=remote if request.url.path.endswith("/reviews") else comments)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        outcome = publish.publish_review("owner/repo", 3, head, rid, doc, FILES, http=http)
        assert outcome.status == expected_status
        assert "1 more unverified findings are not listed." in outcome.payload["body"]
        assert "<!-- argus:ufp=" not in outcome.payload["body"]
        assert len(posts) == (1 if expected_status == "posted" else 0)
        if expected_status == "skipped":
            assert outcome.reason == "nothing new" and not outcome.items
        else:
            assert [item["fingerprint"] for item in outcome.items] == (["confirmed"] if survivors else [])
            if survivors:
                assert outcome.items[0]["placement"] == ("inline" if confirmed_line == 10 else "summary")
            recovered = publish.publish_review("owner/repo", 3, head, rid, doc, FILES, http=http)
            assert recovered.status == "posted" and recovered.reason == "recovered an existing review"

        head = "c" * 40
        next_rid = store(database, [finding("omitted")], survivors, head=head)
        repeated = publish.publish_review("owner/repo", 3, head, next_rid, doc, FILES, http=http)
        assert repeated.status == "skipped" and repeated.reason == "nothing new"
        assert len(posts) == (1 if expected_status == "posted" else 0)


def test_422_fallback_drops_unverified_section_when_confirmed_body_needs_space(database, monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    rid = store(database, [finding(f"item-{n}") for n in range(12)], [finding("confirmed", real=True)])
    doc = document(database, rid)
    doc["findings"][0]["evidence"] = "c" * 59_000
    for item in doc["unverified"]:
        item["evidence"] = "u" * 24_000
    posts = []

    def handler(request):
        if request.method == "POST":
            payload = json.loads(request.content)
            posts.append(payload)
            return httpx.Response(422 if len(posts) == 1 else 200, json={})
        body = {"head": {"sha": HEAD}} if request.url.path.endswith("/3") else []
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        outcome = publish.publish_review("owner/repo", 3, HEAD, rid, doc, FILES, http=http)
    assert outcome.status == "posted" and len(posts) == 2
    assert posts[0]["body"].count("<!-- argus:ufp=") == 2
    body = posts[1]["body"]
    assert len(body) <= 60_000
    assert HEADING not in body and "<!-- argus:ufp=" not in body
    assert "12 more unverified findings are not listed." in body
    assert "<!-- argus:fp=confirmed -->" in body
    assert [item["fingerprint"] for item in outcome.items] == ["confirmed"]
