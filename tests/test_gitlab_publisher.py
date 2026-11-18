"""Exercise publishing and recovery against an in-memory GitLab server."""

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from types import SimpleNamespace

import httpx
import pytest

from argus import github, publish
from argus.api import db, main
from argus.diff import FileList, FilePatch
from argus.gitlab import publisher
from argus.gitlab.client import GitLabClient
from argus.gitlab.body import assert_body_safe, render_gitlab_comment
from argus.gitlab.pacing import NotePacer
from argus.gitlab.reader import GitLabForge
from test_gitlab_client import SENTINEL, configured as configured, make_client as make_client
from test_gitlab_pacing import FakeClock


KEY = "gitlab:gitlab.example.test#42"
HEAD = "a" * 40
FILES = FileList([FilePatch("app.py", "modified", "@@ -1,2 +1,6 @@\n same\n-old\n+new\n+two\n+three\n+four\n+five\n")])


def finding(number=1, **updates):
    return {"fingerprint": f"finding-{number}", "severity": "major", "category": "contracts",
            "file": "app.py", "line": number, "end_line": None, "title": "Check input",
            "description": "This input is unchecked.", "proposed_fix": "Validate input.",
            "evidence": "unchecked(value)", "evidence_tier": "consensus", **updates}


def document(count=1, **updates):
    return {"findings": [finding(n) for n in range(1, count + 1)], "verdict": "fail", **updates}


class Server:
    def __init__(self):
        self.head = HEAD
        self.user = {"id": 7, "username": "review-bot"}
        self.member = {"id": 7, "access_level": 20}
        self.source = 42
        self.paths = {42: "allowed/app", 43: "allowed/fork"}
        self.discussions = []
        self.discussion_head = None
        self.failures = {}
        self.summary_status = 201
        self.summary_leftover = False
        self.diff_status = 200
        self.missing_version = False
        self.paginate = False
        self.before_write = lambda request: None

    def note(self, body, position=None, author=7, note_type="DiffNote"):
        number = 101 + len(self.discussions)
        note = {"id": number, "author": {"id": author}, "body": body,
                "type": note_type if position else "Note", "position": position}
        discussion = {"id": f"{number:040x}", "individual_note": position is None, "notes": [note]}
        self.discussions.append(discussion)
        return discussion

    def handle(self, request):
        path = request.url.path
        if request.method == "POST":
            self.before_write(request)
            payload = json.loads(request.content)
            assert set(payload) <= {"body", "position"}
            assert_body_safe(payload["body"])
            if path.endswith("/notes"):
                if self.summary_status == 201 or self.summary_leftover:
                    note = self.note(payload["body"])["notes"][0]
                    return httpx.Response(self.summary_status, json=note)
                return httpx.Response(self.summary_status)
            assert path.endswith("/discussions")
            if self.discussion_head is not None:
                payload["position"]["head_sha"] = self.discussion_head
            line = payload["position"]["new_line"]
            failure = self.failures.pop(line, None)
            if failure == "leftover":
                self.note(**payload)
                return httpx.Response(500)
            if failure == "unknown":
                return httpx.Response(201, json={})
            if failure == 429:
                return httpx.Response(429, headers={"Retry-After": "60"})
            if isinstance(failure, int):
                return httpx.Response(failure)
            result = self.note(**payload, note_type="Note" if failure == "downgraded" else "DiffNote")
            if failure == "wrong_position":
                result["notes"][0]["position"]["new_line"] = 900
            return httpx.Response(201, json=result)
        if path.endswith("/user"):
            return httpx.Response(200, json=self.user)
        if "/members/all/" in path:
            return httpx.Response(404 if self.member is None else 200, json=self.member)
        if path.endswith("/diffs"):
            return httpx.Response(self.diff_status, json=[{
                "new_path": "app.py", "old_path": "app.py", "diff": FILES[0].patch,
            }])
        if path.endswith("/versions"):
            versions = [] if self.missing_version else [
                {"id": 8, "base_commit_sha": "b" * 40, "start_commit_sha": "c" * 40,
                 "head_commit_sha": self.head},
                {"id": 7, "base_commit_sha": "b" * 40, "start_commit_sha": "c" * 40,
                 "head_commit_sha": "e" * 40},
            ]
            return httpx.Response(200, json=versions)
        if path.endswith("/discussions"):
            assert request.url.params["per_page"] == "100"
            if self.paginate and request.url.params.get("page") != "2":
                return httpx.Response(200, json=[], headers={"X-Next-Page": "2"})
            return httpx.Response(200, json=self.discussions)
        if "/merge_requests/" in path:
            return httpx.Response(200, json={"iid": 3, "sha": self.head, "state": "opened",
                                            "source_project_id": self.source, "target_project_id": 42})
        project_id = int(path.rsplit("/", 1)[1])
        return httpx.Response(200, json={"id": project_id, "path_with_namespace": self.paths[project_id],
                                        "archived": False, "default_branch": "main", "visibility": "private"})


@pytest.fixture
def setup(configured, make_client, monkeypatch, tmp_path):
    path = str(tmp_path / "publish.db")
    db.init_db(path)
    for key, value in {"ARGUS_DB_PATH": path, "ARGUS_PUBLISH": "1", "ARGUS_GITLAB_PUBLISH": "1",
                       "ARGUS_GITLAB_PUBLISH_PROJECTS": KEY, "ARGUS_GITLAB_ALLOW": KEY,
                       "ARGUS_DENY_OWNERS": "", "ARGUS_PUBLISH_MIN_SEVERITY": "minor",
                       "ARGUS_PUBLISH_MAX_COMMENTS": "10"}.items():
        monkeypatch.setenv(key, value)
    publisher._identities.clear()
    server = Server()
    client, requests = make_client(server.handle)
    clock = FakeClock()
    monkeypatch.setattr("time.time", lambda: 1_800_000_000.0 + clock.now)
    client.pacing = NotePacer(30, clock=clock, sleep=clock.sleep)
    review_id = db.create_review(path, KEY, 3, "test", HEAD)
    return SimpleNamespace(server=server, client=client, requests=requests, clock=clock,
                           path=path, review_id=review_id, forge=GitLabForge(client))


def run(setup, doc=None, **kwargs):
    return setup.forge.publish(KEY, 3, HEAD, setup.review_id, doc or document(), kwargs.pop("files", FILES), **kwargs)


def posts(setup):
    return [r for r in setup.requests if r.method == "POST"]


def save_rows(setup, outcome):
    db.save_publish_outcome(setup.path, setup.review_id, outcome)
    with closing(db.connect(setup.path)) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM published_findings ORDER BY fingerprint")]


def test_all_201_diffnotes_save_inline_ids(setup):
    outcome = run(setup, document(3))
    assert (outcome.status, outcome.comments_posted, outcome.bot_user_id, outcome.bot_login) == (
        "posted", 3, 7, "review-bot")
    assert outcome.gh_review_id == 104
    rows = save_rows(setup, outcome)
    assert [(r["placement"], r["gh_comment_id"], r["gh_thread_id"], r["anchor_status"]) for r in rows] == [
        ("inline", n, f"{n:040x}", "anchored") for n in range(101, 104)]
    assert posts(setup)[-1].url.path.endswith("/notes")
    assert publish.review_marker(outcome.payload["body"]) == (setup.review_id, HEAD)


@pytest.mark.parametrize("case", ["posted", "recovered", "resumed", "partial_post", "dry_run"])
def test_outcomes_keep_recording_ids_and_identity(setup, monkeypatch, case):
    doc = document(findings=[finding(), finding(2, line=900), finding(3)])
    if case == "recovered":
        assert run(setup, doc).status == "posted"
    elif case == "resumed":
        setup.server.summary_status = 500
        assert run(setup, doc).reason == "partial_post"
        setup.server.summary_status = 201
    elif case == "partial_post":
        setup.server.failures[3] = 422
    elif case == "dry_run":
        monkeypatch.setenv("ARGUS_GITLAB_PUBLISH", "dry-run")
    outcome = run(setup, doc)
    assert outcome.status == ("error" if case == "partial_post" else "dry_run" if case == "dry_run" else "posted")
    if case == "recovered":
        assert outcome.reason == "recovered an existing review"
    elif case == "resumed":
        assert (outcome.comments_posted, outcome.already_posted) == (0, 2)
    elif case == "partial_post":
        assert outcome.reason == "partial_post"
    assert (outcome.bot_user_id, outcome.bot_login) == (7, "review-bot")
    assert len(outcome.items) == (1 if case == "partial_post" else 3)
    for item in outcome.items:
        assert {"gh_comment_id", "gh_thread_id", "anchor_status"} <= item.keys()
        if item["placement"] == "inline" and case != "dry_run":
            assert item["gh_thread_id"]
            assert type(item["gh_comment_id"]) is int
            assert item["anchor_status"] == "anchored"
        else:
            assert item["gh_thread_id"] is None and item["gh_comment_id"] is None
            assert item["anchor_status"] is None
    assert len(save_rows(setup, outcome)) == len(outcome.items)
    with closing(db.connect(setup.path)) as conn:
        identity = conn.execute("SELECT repo, bot_login, bot_user_id FROM publishing_identities").fetchone()
        assert tuple(identity) == (KEY, "review-bot", 7)


def test_one_400_demotes_and_saves_exact_rows(setup):
    setup.server.failures[3] = 400
    outcome = run(setup, document(5))
    assert (outcome.status, outcome.comments_posted) == ("posted", 4)
    rows = save_rows(setup, outcome)
    fields = ("fingerprint", "placement", "gh_comment_id", "gh_thread_id", "anchor_status", "gh_review_id")
    assert [tuple(r[k] for k in fields) for r in rows] == [
        ("finding-1", "inline", 101, f"{101:040x}", "anchored", 105),
        ("finding-2", "inline", 102, f"{102:040x}", "anchored", 105),
        ("finding-3", "summary", None, None, "rejected_400", 105),
        ("finding-4", "inline", 103, f"{103:040x}", "anchored", 105),
        ("finding-5", "inline", 104, f"{104:040x}", "anchored", 105),
    ]


@pytest.mark.parametrize("returned_head", [HEAD, "d" * 40])
def test_500_leftover_reconciles_without_duplicate_on_rerun(setup, returned_head):
    setup.server.failures[1] = "leftover"
    setup.server.discussion_head = returned_head
    first = run(setup)
    assert (first.status, first.items[0]["placement"], first.items[0]["gh_comment_id"]) == ("posted", "inline", 101)
    assert first.items[0]["anchor_status"] == "anchored"
    assert first.comments_posted == 1
    assert len(posts(setup)) == 2
    setup.server.paginate = True
    second = run(setup)
    assert (second.status, second.reason, second.already_posted) == ("posted", "recovered an existing review", 1)
    assert second.items == first.items
    assert second.gh_review_id == first.gh_review_id
    assert second.comments_posted == 0
    assert len(posts(setup)) == 2


@pytest.mark.parametrize("returned_head, placement, anchor_status, comments_posted", [
    (HEAD, "inline", "anchored", 1),
    ("d" * 40, "summary", "downgraded", 0),
])
def test_201_diffnote_requires_matching_head(setup, returned_head, placement, anchor_status, comments_posted):
    setup.server.discussion_head = returned_head
    outcome = run(setup)
    assert (outcome.status, outcome.comments_posted) == ("posted", comments_posted)
    assert json.loads(posts(setup)[0].content)["position"]["head_sha"] == HEAD
    row = save_rows(setup, outcome)[0]
    assert (row["placement"], row["anchor_status"]) == (placement, anchor_status)
    if placement == "summary":
        assert row["gh_comment_id"] is None and row["gh_thread_id"] is None
        assert "finding-1" in publish.summary_fingerprints(outcome.payload["body"])[0]
    else:
        assert (row["gh_comment_id"], row["gh_thread_id"]) == (101, f"{101:040x}")


@pytest.mark.parametrize("failure", ["downgraded", "wrong_position"])
def test_201_plain_note_is_downgraded(setup, failure):
    setup.server.failures[1] = failure
    outcome = run(setup)
    assert (outcome.status, outcome.comments_posted) == ("posted", 0)
    row = save_rows(setup, outcome)[0]
    assert (row["placement"], row["gh_comment_id"], row["gh_thread_id"], row["anchor_status"]) == (
        "summary", None, None, "downgraded")
    assert "finding-1" in publish.summary_fingerprints(outcome.payload["body"])[0]


@pytest.mark.parametrize("save_first", [True, False])
@pytest.mark.parametrize("new_review", [False, True])
def test_summary_failure_preserves_three_inline_items_and_rerun_dedups(setup, save_first, new_review):
    setup.server.summary_status = 500
    first = run(setup, document(3))
    assert (first.status, first.reason, first.comments_posted) == ("error", "partial_post", 3)
    if save_first:
        assert len(save_rows(setup, first)) == 3
    assert first.gh_review_id is None
    if new_review:
        setup.review_id = db.create_review(setup.path, KEY, 3, "test", HEAD)
    setup.requests.clear()
    setup.server.summary_status = 201
    second = run(setup, document(3))
    assert (second.status, second.comments_posted, second.already_posted) == ("posted", 0, 3)
    assert len(posts(setup)) == 1 and posts(setup)[0].url.path.endswith("/notes")
    assert second.gh_review_id == 104
    fields = ("fingerprint", "placement", "gh_comment_id", "gh_thread_id", "anchor_status")
    expected = [
        ("finding-1", "inline", 101, f"{101:040x}", "anchored"),
        ("finding-2", "inline", 102, f"{102:040x}", "anchored"),
        ("finding-3", "inline", 103, f"{103:040x}", "anchored"),
    ]
    assert [tuple(item[k] for k in fields) for item in second.items] == expected
    rows = save_rows(setup, second)
    assert [tuple(row[k] for k in fields) for row in rows] == expected
    assert all(row["gh_review_id"] == 104 and row["review_id"] == setup.review_id for row in rows)


def test_rerun_records_existing_inline_finding_once(setup):
    setup.server.summary_status = 500
    assert run(setup).reason == "partial_post"
    setup.server.summary_status = 201
    outcome = run(setup, document(findings=[finding(), finding()]))
    assert (outcome.status, outcome.comments_posted) == ("posted", 0)
    assert len(outcome.items) == 1
    assert (outcome.items[0]["placement"], outcome.items[0]["gh_comment_id"]) == ("inline", 101)


@pytest.mark.parametrize("unknown_inline", [False, True])
def test_partial_retry_posts_own_summary_after_another_review(setup, unknown_inline):
    first = run(setup)
    assert first.status == "posted"
    save_rows(setup, first)
    setup.review_id = db.create_review(setup.path, KEY, 3, "test", HEAD)
    doc = document(2)
    setup.server.summary_status = 500
    if unknown_inline:
        setup.server.failures[2] = "unknown"
    setup.requests.clear()
    partial = run(setup, doc)
    assert (partial.status, partial.reason, partial.gh_review_id) == ("error", "partial_post", None)
    assert partial.comments_posted == (0 if unknown_inline else 1)
    attempts = posts(setup)
    assert len(attempts) == (1 if unknown_inline else 2)
    assert attempts[0].url.path.endswith("/discussions")
    assert json.loads(attempts[0].content)["position"]["new_line"] == 2
    rows = save_rows(setup, partial)
    assert [row["fingerprint"] for row in rows] == (
        ["finding-1"] if unknown_inline else ["finding-1", "finding-2"])
    if unknown_inline:
        # The marker can become visible after an unconfirmed creation attempt.
        setup.server.note(**json.loads(attempts[0].content))
    stored = db.get_review(setup.path, setup.review_id)
    prior = {"status": stored["publish_status"], "reason": stored["publish_error"]}
    assert prior == {"status": "error", "reason": "partial_post"}
    setup.server.summary_status = 201
    setup.requests.clear()

    retried = run(setup, doc, prior=prior)

    assert (retried.status, retried.comments_posted, retried.already_posted) == ("posted", 0, 2)
    assert retried.gh_review_id == 104
    assert len(posts(setup)) == 1 and posts(setup)[0].url.path.endswith("/notes")
    assert f"argus:review={setup.review_id} head={HEAD}" in json.loads(posts(setup)[0].content)["body"]
    fields = ("fingerprint", "placement", "gh_comment_id", "gh_thread_id", "anchor_status")
    expected = [("finding-1", "inline", 101, f"{101:040x}", "anchored"),
                ("finding-2", "inline", 103, f"{103:040x}", "anchored")]
    assert [tuple(item[key] for key in fields) for item in retried.items] == expected
    rows = save_rows(setup, retried)
    assert [tuple(row[key] for key in fields) for row in rows] == expected
    assert all(row["review_id"] == setup.review_id and row["gh_review_id"] == 104
               and row["mode"] == "posted" for row in rows)

    stored = db.get_review(setup.path, setup.review_id)
    prior = {"status": stored["publish_status"], "reason": stored["publish_error"]}
    assert prior == {"status": "posted", "reason": None}
    setup.requests.clear()
    recovered = run(setup, doc, prior=prior)
    assert (recovered.status, recovered.reason, recovered.gh_review_id) == (
        "posted", "recovered an existing review", 104)
    assert posts(setup) == []


@pytest.mark.parametrize("interruption", ["access", "head", "fetch", "dry_run", "config"])
def test_completed_partial_review_survives_attempt_without_writes(setup, monkeypatch, interruption):
    first = run(setup)
    assert first.status == "posted"
    save_rows(setup, first)
    setup.review_id = db.create_review(setup.path, KEY, 3, "test", HEAD)
    doc = document(2)
    setup.server.summary_status = 500
    setup.requests.clear()
    partial = run(setup, doc)
    assert (partial.status, partial.reason, partial.comments_posted, partial.gh_review_id) == (
        "error", "partial_post", 1, None)
    assert [request.url.path.rsplit("/", 1)[1] for request in posts(setup)] == ["discussions", "notes"]
    save_rows(setup, partial)
    stored = db.get_review(setup.path, setup.review_id)
    monkeypatch.setattr(main, "forge_for", lambda repo: setup.forge)
    monkeypatch.setattr(main.verdict, "document", lambda *a, **kw: doc)
    setup.server.summary_status = 201
    setup.requests.clear()

    with monkeypatch.context() as interrupted:
        if interruption == "access":
            interrupted.setattr(setup.server, "member", {"id": 7, "access_level": 10})
        elif interruption == "head":
            interrupted.setattr(setup.server, "head", "d" * 40)
        elif interruption == "fetch":
            interrupted.setattr(setup.server, "diff_status", 500)
        elif interruption == "dry_run":
            interrupted.setenv("ARGUS_GITLAB_PUBLISH", "dry-run")
        elif interruption == "config":
            interrupted.setenv("ARGUS_GITLAB_PUBLISH_PROJECTS", "")
        main._publish_completed_review(setup.path, setup.review_id, KEY, 3)

    assert posts(setup) == []
    kept = db.get_review(setup.path, setup.review_id)
    assert (kept["publish_status"], kept["publish_error"]) == ("error", "partial_post")
    assert kept == stored

    setup.requests.clear()
    main._publish_completed_review(setup.path, setup.review_id, KEY, 3)

    assert len(posts(setup)) == 1 and posts(setup)[0].url.path.endswith("/notes")
    assert f"argus:review={setup.review_id} head={HEAD}" in json.loads(posts(setup)[0].content)["body"]
    completed = db.get_review(setup.path, setup.review_id)
    assert (completed["publish_status"], completed["publish_error"]) == ("posted", None)


@pytest.mark.parametrize("status, reason", [("posted", None), ("error", "fetch failed")])
def test_completed_review_without_partial_prior_saves_skipped_outcome(setup, monkeypatch, status, reason):
    db.save_publish_outcome(setup.path, setup.review_id, publish.PublishOutcome(status, reason=reason))
    setup.server.member = {"id": 7, "access_level": 10}
    monkeypatch.setattr(main, "forge_for", lambda repo: setup.forge)
    monkeypatch.setattr(main.verdict, "document", lambda *a, **kw: document())

    main._publish_completed_review(setup.path, setup.review_id, KEY, 3)

    stored = db.get_review(setup.path, setup.review_id)
    assert (stored["publish_status"], stored["publish_error"]) == (
        "skipped", "GitLab publishing access unavailable")
    assert posts(setup) == []


def test_concurrent_publishes_serialize_entire_mr_flow(setup, monkeypatch):
    entered, attempted, release = (threading.Event() for _ in range(3))
    lock = setup.client.pacing.write_lock
    order = []

    @contextmanager
    def observed_lock(*args):
        if threading.current_thread().name.endswith("_1"):
            attempted.set()
        with lock(*args):
            yield

    def before_write(request):
        order.append(threading.current_thread().name)
        if len(order) == 1:
            entered.set()
            assert release.wait(5)

    monkeypatch.setattr(setup.client.pacing, "write_lock", observed_lock)
    setup.server.before_write = before_write
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, setup, document(3))
        assert entered.wait(5)
        second = pool.submit(run, setup, document(3))
        try:
            assert attempted.wait(5)
            assert len(order) == 1
        finally:
            release.set()
        results = [first.result(timeout=5), second.result(timeout=5)]
    assert [r.status for r in results] == ["posted", "posted"]
    assert results[1].reason == "recovered an existing review"
    assert len(order) == 4 and len(set(order)) == 1


@pytest.mark.parametrize("denial", ["target", "changed_source"])
def test_policy_denial_while_waiting_for_mr_lock_sends_no_writes(setup, monkeypatch, denial):
    attempted = threading.Event()
    lock = setup.client.pacing.write_lock

    @contextmanager
    def observed_lock(*args):
        attempted.set()
        with lock(*args):
            yield

    monkeypatch.setattr(setup.client.pacing, "write_lock", observed_lock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with lock(7, 42, 3):
            pending = pool.submit(run, setup)
            assert attempted.wait(5)
            assert not pending.done()
            deny_key = KEY
            if denial == "changed_source":
                setup.server.source = 43
                deny_key = "gitlab:gitlab.example.test#43"
            db.save_config(setup.path, repos=[], model="test", bug_cap=10,
                           min_confidence=0.5, deny=[deny_key])
        outcome = pending.result(timeout=5)
    assert (outcome.status, outcome.reason) == ("skipped", "repository denied")
    assert (outcome.bot_user_id, outcome.bot_login) == (7, "review-bot")
    assert outcome.comments_posted == 0 and outcome.items == []
    assert posts(setup) == []


def test_429_respects_retry_after_without_changing_github(setup):
    setup.server.failures[1] = 429
    before = dict(vars(github))
    quota, cache = copy.deepcopy(github._quota), copy.deepcopy(github._cache)
    outcome = run(setup)
    assert outcome.status == "posted"
    assert setup.clock.waits[0] == 60
    assert len(posts(setup)) == 3
    assert vars(github) == before and github._quota == quota and github._cache == cache


def test_dry_run_sends_no_write_and_records_rows(setup, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_PUBLISH", "dry-run")
    doc = document(findings=[finding(), finding(2, line=900)])
    outcome = run(setup, doc)
    assert outcome.status == "dry_run" and posts(setup) == []
    rows = save_rows(setup, outcome)
    assert [r["placement"] for r in rows] == ["inline", "summary"]
    assert all((r["mode"], r["gh_comment_id"], r["gh_thread_id"], r["gh_review_id"], r["anchor_status"])
               == ("dry_run", None, None, None, None) for r in rows)
    assert outcome.payload["comments"][0]["position"]["head_sha"] == HEAD


@pytest.mark.parametrize("field", ["description", "evidence", "proposed_fix"])
@pytest.mark.parametrize("text", ["/merge\n/rebase", "   /merge\n   /rebase",
                                  "```\n/merge\n/rebase\n```", "\u200b/merge\n\u200b/rebase"])
def test_commands_neutralized_in_inline_and_summary(setup, field, text):
    doc = document(findings=[finding(**{field: text}), finding(2, line=900, **{field: text})])
    assert run(setup, doc).status == "posted"
    bodies = [json.loads(r.content)["body"] for r in posts(setup)]
    assert len(bodies) == 2
    for body in bodies:
        assert_body_safe(body)
        assert "\\/merge" in body and "\\/rebase" in body
        assert not any(line.lstrip(" \t\u200b").startswith("/") for line in body.splitlines())


@pytest.mark.parametrize("unsafe", ["/merge", "/rebase", "@someone"])
def test_forced_unsafe_body_refused_before_write(setup, monkeypatch, unsafe):
    monkeypatch.setattr(publisher, "render_gitlab_comment", lambda finding: unsafe)
    outcome = run(setup)
    assert outcome.status == "error" and posts(setup) == []
    assert (outcome.bot_user_id, outcome.bot_login) == (7, "review-bot")
    assert save_rows(setup, outcome) == []


def test_other_authors_markers_do_not_dedup(setup):
    setup.server.note(render_gitlab_comment(finding()), {"new_path": "app.py", "new_line": 1}, author=8)
    summary = publish.render_review(HEAD, setup.review_id, document(), [])
    setup.server.note(summary["body"], author=8)
    outcome = run(setup)
    assert (outcome.status, outcome.comments_posted, outcome.already_posted) == ("posted", 1, 0)
    assert len(posts(setup)) == 2


@pytest.mark.parametrize("denial", ["not_allowlisted", "target", "source"])
def test_policy_gates_send_no_writes(setup, monkeypatch, denial):
    if denial == "not_allowlisted":
        monkeypatch.setenv("ARGUS_GITLAB_PUBLISH_PROJECTS", KEY + "0")
    else:
        monkeypatch.setenv("ARGUS_DENY_OWNERS", "blocked")
        setup.server.paths[42 if denial == "target" else 43] = "blocked/app"
        setup.server.source = 43 if denial == "source" else 42
    outcome = run(setup)
    assert outcome.status == "skipped"
    assert outcome.reason == ("repo not allowlisted" if denial == "not_allowlisted" else "repository denied")
    assert posts(setup) == []
    if denial == "not_allowlisted":
        assert setup.requests == []


@pytest.mark.parametrize("general", ["0", "dry-run", "1", "invalid"])
@pytest.mark.parametrize("gitlab", ["0", "dry-run", "1"])
def test_lower_publish_mode_wins(setup, monkeypatch, general, gitlab):
    monkeypatch.setenv("ARGUS_PUBLISH", general)
    monkeypatch.setenv("ARGUS_GITLAB_PUBLISH", gitlab)
    expected = "skipped" if general in {"0", "invalid"} or gitlab == "0" else (
        "dry_run" if "dry-run" in {general, gitlab} else "posted")
    assert run(setup).status == expected
    if expected != "posted":
        assert posts(setup) == []
    if expected == "skipped":
        assert setup.requests == []


@pytest.mark.parametrize("missing_version", [False, True])
def test_head_moved_skips_without_writes(setup, missing_version):
    setup.server.missing_version = missing_version
    if not missing_version:
        setup.server.head = "d" * 40
    outcome = run(setup)
    assert outcome.status == "skipped" and outcome.reason.startswith("MR head moved since the review")
    assert (outcome.bot_user_id, outcome.bot_login) == (7, "review-bot")
    assert posts(setup) == []


def test_outside_diff_finding_goes_to_summary(setup):
    outcome = run(setup, document(findings=[finding(line=900)]))
    assert (outcome.status, outcome.comments_posted) == ("posted", 0)
    assert "### Outside the diff" in outcome.payload["body"]
    assert save_rows(setup, outcome)[0]["placement"] == "summary"
    assert len(posts(setup)) == 1


def test_identity_cache_is_keyed_by_url_and_token(setup, make_client, monkeypatch):
    assert setup.forge.bot_identity() == (7, "review-bot")
    assert setup.forge.bot_identity() == (7, "review-bot")
    assert len(setup.requests) == 1
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", "different-test-credential")
    client, requests = make_client(setup.server.handle)
    assert publisher.bot_identity(client) == (7, "review-bot") and len(requests) == 1
    monkeypatch.setenv("ARGUS_GITLAB_URL", "https://gitlab.example.test/other")
    client, requests = make_client(setup.server.handle)
    assert publisher.bot_identity(client) == (7, "review-bot") and len(requests) == 1


@pytest.mark.parametrize("bad", [{}, [], {"id": True, "username": "bot"}, {"id": 0, "username": "bot"},
                                  {"id": 7, "username": ""}, {"id": 7, "username": "bad name"},
                                  {"id": 7, "username": "x" * 256}, {"id": 7, "username": "@bot"}])
def test_identity_rejects_malformed_payload_and_does_not_cache_failure(setup, bad):
    setup.server.user = bad
    with pytest.raises(ValueError):
        setup.forge.bot_identity()
    outcome = run(setup)
    assert outcome.status == "skipped" and posts(setup) == []
    setup.server.user = {"id": 7, "username": "review-bot"}
    assert setup.forge.bot_identity() == (7, "review-bot")


@pytest.mark.parametrize("level,state", [(10, "no_access"), (20, "ok"), (30, "ok"), (None, "no_access")])
def test_access_requires_reporter(setup, level, state):
    setup.server.member = {"id": 7, "access_level": level} if level else None
    assert setup.forge.check_access(KEY)["state"] == state
    assert posts(setup) == []
    if state != "ok":
        outcome = run(setup)
        assert outcome.status == "skipped" and posts(setup) == []
        assert (outcome.bot_user_id, outcome.bot_login) == (7, "review-bot")


@pytest.mark.parametrize("mode,called", [("0", False), ("1", True), ("dry-run", True), ("invalid", False)])
def test_completed_review_reaches_gitlab_publisher_when_enabled(setup, monkeypatch, mode, called):
    monkeypatch.setenv("ARGUS_GITLAB_PUBLISH", mode)
    calls = []
    forge = SimpleNamespace(change_files=lambda *a: FILES,
                            publish=lambda *a, **kw: calls.append(a) or publish.PublishOutcome("skipped"))
    monkeypatch.setattr(main, "forge_for", lambda repo: forge)
    monkeypatch.setattr(main.publish, "enabled_for", lambda repo: pytest.fail("GitHub gate used for GitLab"))
    monkeypatch.setattr(main.verdict, "document", lambda *a, **kw: document())
    main._publish_completed_review(setup.path, setup.review_id, KEY, 3)
    assert bool(calls) == called
    assert db.get_review(setup.path, setup.review_id)["publish_status"] == ("skipped" if called else None)


@pytest.mark.parametrize("repo", ["owner/repo", KEY])
def test_completed_review_passes_prior_only_for_gitlab(setup, monkeypatch, repo):
    review_id = db.create_review(setup.path, repo, 3, "test", HEAD)
    db.save_publish_outcome(setup.path, review_id, publish.PublishOutcome("error", reason="partial_post"))
    calls = []

    def record_publish(*args, **kwargs):
        calls.append((args, kwargs))
        return publish.PublishOutcome("posted")

    forge = SimpleNamespace(change_files=lambda *a: FILES, publish=record_publish)
    doc = document()
    monkeypatch.setattr(main, "forge_for", lambda repo: forge)
    monkeypatch.setattr(main.publish, "enabled_for", lambda repo: True)
    monkeypatch.setattr(main.verdict, "document", lambda *a, **kw: doc)

    main._publish_completed_review(setup.path, review_id, repo, 3)

    expected_kwargs = {"prior": {"status": "error", "reason": "partial_post"}} if repo == KEY else {}
    assert calls == [((repo, 3, HEAD, review_id, doc, FILES), expected_kwargs)]
    assert db.get_review(setup.path, review_id)["publish_status"] == "posted"


def test_empty_500_reconciliation_demotes_to_summary(setup):
    setup.server.failures[1] = 500
    outcome = run(setup)
    assert outcome.status == "posted" and outcome.comments_posted == 0
    row = save_rows(setup, outcome)[0]
    assert (row["placement"], row["anchor_status"]) == ("summary", "rejected_500")
    assert len(posts(setup)) == 2


@pytest.mark.parametrize("status", [502, 503, 504])
@pytest.mark.parametrize("late_note", [False, True])
def test_gateway_failure_saves_partial_items_and_rerun_checks_for_late_note(setup, status, late_note):
    setup.server.failures[3] = status
    first = run(setup, document(5))
    assert (first.status, first.reason, first.comments_posted) == ("error", "partial_post", 2)
    assert first.gh_review_id is None
    assert first.payload["counts"] == {"attempted": 3, "confirmed_inline": 2, "unresolved": 1}
    assert len(posts(setup)) == 3
    assert all(r.url.path.endswith("/discussions") for r in posts(setup))
    rows = save_rows(setup, first)
    assert [(r["fingerprint"], r["placement"], r["anchor_status"]) for r in rows] == [
        ("finding-1", "inline", "anchored"), ("finding-2", "inline", "anchored")]
    if late_note:
        setup.server.note(**json.loads(posts(setup)[2].content))
    setup.requests.clear()
    second = run(setup, document(5))
    assert (second.status, second.comments_posted, second.already_posted) == (
        "posted", 2 if late_note else 3, 3 if late_note else 2)
    discussion_posts = [r for r in posts(setup) if r.url.path.endswith("/discussions")]
    assert [json.loads(r.content)["position"]["new_line"] for r in discussion_posts] == (
        [4, 5] if late_note else [3, 4, 5])
    assert sum(r.url.path.endswith("/notes") for r in posts(setup)) == 1
    assert second.gh_review_id == 106
    rows = save_rows(setup, second)
    assert [(r["fingerprint"], r["gh_comment_id"], r["gh_thread_id"], r["anchor_status"]) for r in rows] == [
        (f"finding-{n}", 100 + n, f"{100 + n:040x}", "anchored") for n in range(1, 6)]


@pytest.mark.parametrize("failure", [403, 422, 599, "unknown"])
def test_unresolved_inline_cannot_leave_a_completion_marker(setup, failure):
    setup.server.failures[2] = failure
    outcome = run(setup, document(3))
    assert (outcome.status, outcome.reason, outcome.comments_posted) == ("error", "partial_post", 1)
    assert [r["fingerprint"] for r in save_rows(setup, outcome)] == ["finding-1"]
    assert outcome.payload["counts"]["unresolved"] == 1
    assert all(not r.url.path.endswith("/notes") for r in posts(setup))


def test_400_is_not_recorded_when_summary_fails(setup):
    setup.server.failures[2] = 400
    setup.server.summary_status = 403
    outcome = run(setup, document(3))
    assert (outcome.status, outcome.reason) == ("error", "partial_post")
    assert [r["fingerprint"] for r in save_rows(setup, outcome)] == ["finding-1", "finding-3"]


def test_downgraded_note_is_summarized_after_failed_summary(setup):
    setup.server.failures[1] = "downgraded"
    setup.server.summary_status = 500
    first = run(setup)
    assert (first.status, first.reason) == ("error", "partial_post")
    assert save_rows(setup, first) == []
    setup.server.summary_status = 201
    second = run(setup)
    assert (second.status, second.comments_posted, second.already_posted) == ("posted", 0, 1)
    row = save_rows(setup, second)[0]
    assert (row["placement"], row["anchor_status"]) == ("summary", "downgraded")
    assert len(posts(setup)) == 3


def test_dry_run_with_existing_review_has_no_confirmed_ids(setup, monkeypatch):
    assert run(setup).status == "posted"
    setup.requests.clear()
    monkeypatch.setenv("ARGUS_GITLAB_PUBLISH", "dry-run")
    outcome = run(setup)
    assert outcome.status == "dry_run" and outcome.gh_review_id is None
    assert posts(setup) == []
    assert all(i["gh_comment_id"] is None and i["gh_thread_id"] is None for i in outcome.items)


def test_dry_run_after_downgrade_has_no_anchor_status(setup, monkeypatch):
    setup.server.failures[1] = "downgraded"
    setup.server.summary_status = 500
    assert run(setup).reason == "partial_post"
    setup.requests.clear()
    monkeypatch.setenv("ARGUS_GITLAB_PUBLISH", "dry-run")
    outcome = run(setup)
    assert outcome.status == "dry_run" and posts(setup) == []
    assert outcome.items[0]["placement"] == "summary"
    assert outcome.items[0]["anchor_status"] is None


def test_summary_leftover_is_recovered_on_rerun(setup):
    setup.server.summary_status = 500
    setup.server.summary_leftover = True
    first = run(setup, document(findings=[finding(line=900)]))
    assert first.reason == "partial_post"
    second = run(setup, document(findings=[finding(line=900)]))
    assert (second.status, second.reason) == ("posted", "recovered an existing review")
    assert len(posts(setup)) == 1
    assert save_rows(setup, second)[0]["placement"] == "summary"


def test_comment_limit_unverified_and_threshold_match_shared_options(setup, monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH_MAX_COMMENTS", "1")
    doc = document(3, unverified=[finding(4), finding(5, severity="nit")])
    doc["findings"].append(finding(6, severity="nit"))
    outcome = run(setup, doc)
    assert outcome.status == "posted" and outcome.comments_posted == 1
    assert "### Comment limit" in outcome.payload["body"]
    assert "### Unverified" in outcome.payload["body"]
    assert "Below posting threshold: 2" in outcome.payload["body"]
    assert publish.summary_fingerprints(outcome.payload["body"]) == ({"finding-2", "finding-3"}, {"finding-4"})
    assert len(save_rows(setup, outcome)) == 4


def test_unverified_body_limit_records_only_listed_items(setup, monkeypatch):
    monkeypatch.setattr(publish, "_MAX_BODY", 1900)
    outcome = run(setup, document(0, unverified=[finding(n) for n in range(1, 15)]))
    assert outcome.status == "posted" and len(outcome.payload["body"]) <= 1900
    listed = publish.summary_fingerprints(outcome.payload["body"])[1]
    assert 0 < len(listed) < 10
    assert {r["fingerprint"] for r in save_rows(setup, outcome)} == listed


def test_oversized_summary_refused_before_any_write(setup, monkeypatch):
    monkeypatch.setattr(publish, "_MAX_BODY", 600)
    outcome = run(setup, document(findings=[finding(), finding(2, line=900)]))
    assert outcome.status == "error" and posts(setup) == []
    assert save_rows(setup, outcome) == []


def test_unseen_coverage_line_is_gitlab_only(setup):
    complete = publish.render_review(HEAD, 1, document(), FILES)
    unseen = FileList(FILES, unseen=True)
    assert publish.render_review(HEAD, 1, document(), unseen) == complete
    outcome = run(setup, files=unseen)
    assert "Some changed files were not visible to this review." in outcome.payload["body"]


@pytest.mark.parametrize("kwargs", [{}, {"prior": {"status": "posted", "reason": None}}])
def test_nothing_new_skips_a_different_review(setup, kwargs):
    assert run(setup).status == "posted"
    setup.review_id = db.create_review(setup.path, KEY, 3, "test", HEAD)
    assert run(setup, document(2)).status == "posted"
    setup.review_id = db.create_review(setup.path, KEY, 3, "test", HEAD)
    setup.requests.clear()
    outcome = run(setup, document(2), **kwargs)
    assert (outcome.status, outcome.reason, outcome.already_posted) == ("skipped", "nothing new", 2)
    assert (outcome.bot_user_id, outcome.bot_login) == (7, "review-bot")
    assert posts(setup) == []


@pytest.mark.parametrize("setting", ["ARGUS_GITLAB_PUBLISH", "ARGUS_GITLAB_PUBLISH_PROJECTS", "ARGUS_GITLAB_URL"])
def test_invalid_configuration_has_fixed_reason_and_no_writes(setup, monkeypatch, setting):
    monkeypatch.setenv(setting, "bad * setting")
    outcome = run(setup)
    assert outcome.status == "skipped" and "bad * setting" not in outcome.reason
    assert posts(setup) == []


@pytest.mark.parametrize("pr,head", [(0, HEAD), (-1, HEAD), (True, HEAD), (3, "bad"), (3, None)])
def test_invalid_review_identity_is_refused_without_reads(setup, pr, head):
    outcome = setup.forge.publish(KEY, pr, head, 1, document(), FILES)
    assert outcome.status == "skipped" and setup.requests == []


def test_exception_after_write_retains_items_and_redacts_error(setup, monkeypatch, caplog):
    original = GitLabClient.request

    def request(client, method, route, **kwargs):
        if method == "POST" and route == "mr_notes":
            raise RuntimeError("Rejected " + SENTINEL + " https://name:password@gitlab.example.test/?token=private")
        return original(client, method, route, **kwargs)

    monkeypatch.setattr(GitLabClient, "request", request)
    outcome = run(setup)
    assert (outcome.status, outcome.reason) == ("error", "partial_post")
    assert (outcome.bot_user_id, outcome.bot_login) == (7, "review-bot")
    assert len(save_rows(setup, outcome)) == 1
    for text in [caplog.text, json.dumps(outcome.payload), db.get_review(setup.path, setup.review_id)["publish_payload"]]:
        assert SENTINEL not in text and "name:password" not in text and "token=private" not in text
        assert "[redacted]" in text


@pytest.mark.parametrize("unsafe", ["/merge", "@someone"])
def test_forced_unsafe_summary_refused_before_write(setup, monkeypatch, unsafe):
    monkeypatch.setattr(publisher, "_summary", lambda *a: (unsafe, []))
    outcome = run(setup, document(0))
    assert outcome.status == "error" and posts(setup) == []
    assert save_rows(setup, outcome) == []
