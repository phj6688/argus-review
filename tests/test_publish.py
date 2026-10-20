"""Publishing contracts use real rendering and mocked HTTP only."""

import json
from unittest.mock import Mock

import httpx
import pytest

from argus import github, publish
from argus.diff import FilePatch

HEAD = "a" * 40
FILES = [FilePatch("src/app.py", "modified", "@@ -9,3 +9,4 @@\n context\n-old\n+new\n+extra\n tail")]


def finding(**updates):
    item = {
        "file": "src/app.py", "line": 10, "end_line": None,
        "category": "contracts", "severity": "major", "evidence_tier": "consensus",
        "title": "Missing check", "description": "The input is unchecked.",
        "evidence": "accept(value)", "proposed_fix": "Validate the input.",
        "fingerprint": "fp-one",
    }
    return item | updates


def document(*items):
    return {
        "review_id": 7, "source": {"head_sha": HEAD}, "verdict": "changes_requested",
        "coverage": {"level": "partial", "lanes": [
            {"lane": "llm", "status": "ran", "files_reviewed": 2, "files_omitted": 1}]},
        "findings": list(items),
    }


@pytest.fixture(autouse=True)
def publish_env(monkeypatch):
    for key in ("ARGUS_PUBLISH", "ARGUS_PUBLISH_REPOS", "ARGUS_PUBLISH_TOKEN",
                "ARGUS_PUBLISH_MIN_SEVERITY", "ARGUS_PUBLISH_MAX_COMMENTS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "owner/repo")
    monkeypatch.setenv("ARGUS_PUBLISH_TOKEN", "write-secret")
    monkeypatch.setenv("ARGUS_PUBLISH_BOT_LOGIN", "publisher")
    # Any accidentally uninjected HTTP call fails locally.
    monkeypatch.setattr(httpx, "get", Mock(side_effect=AssertionError("unexpected HTTP")))
    monkeypatch.setattr(httpx, "post", Mock(side_effect=AssertionError("unexpected HTTP")))


def test_inline_body_shape_and_summary():
    payload = publish.render_review(HEAD, 7, document(finding()), FILES)
    assert payload["commit_id"] == HEAD
    assert payload["event"] == "COMMENT"
    comment = payload["comments"][0]
    assert comment["path"] == "src/app.py"
    assert comment["line"] == 10
    assert comment["side"] == "RIGHT"
    assert comment["body"] == (
        "_Contracts_ | _Major_ | _consensus_\n\n**Missing check**\n\n"
        "The input is unchecked\\.\n\n```python\naccept(value)\n```\n\n"
        "**Suggested fix:** Validate the input\\.\n\n<details>\n"
        "<summary>Prompt for agents</summary>\n\n"
        "In `src/app.py` around line 10: Missing check. Validate the input\\. "
        "Verify the issue against the current code before changing anything.\n"
        "</details>\n\n<!-- argus:fp=fp-one -->"
    )
    summary = payload["body"]
    for text in ("**Argus review** for aaaaaaa", "changes_requested", "partial",
                 "Files reviewed: 2; omitted: 1", "Major: 1", "consensus: 1",
                 "Below posting threshold: 0", "Already posted: 0"):
        assert text in summary
    assert summary.endswith(f"<!-- argus:review=7 head={HEAD} -->\n<!-- argus:manifest fp= ufp= -->")


@pytest.mark.parametrize("end_line,expected_end", [(None, 10), (12, 12)])
def test_placement_uses_finding_lines_when_comment_has_no_lines(end_line, expected_end):
    doc = document(finding(end_line=end_line))
    payload = publish.render_review(HEAD, 7, doc, FILES)
    payload["comments"][0].update(path="renamed.py", line=None, start_line=None,
                                  original_line=None, original_start_line=None)
    item = publish._placed_items(doc, payload)[0]
    assert (item["line"], item["end_line"]) == (10, expected_end)
    assert item["file"] == "renamed.py"


def test_placement_preserves_rendered_multiline_span():
    doc = document(finding(end_line=12))
    payload = publish.render_review(HEAD, 7, doc, FILES)
    item = publish._placed_items(doc, payload)[0]
    assert (item["line"], item["end_line"]) == (10, 12)


@pytest.mark.parametrize("category,label", [
    ("contracts", "Contracts"), ("error_paths", "Error paths"),
    ("input_handling", "Input handling"), ("boundaries", "Boundaries"),
    ("resources", "Resources"), ("concurrency", "Concurrency"),
    ("security", "Security"), ("tests", "Tests"), ("data_integrity", "Data integrity"),
    ("other", "Correctness"), (None, "Correctness"), ("unknown", "Correctness"),
])
def test_category_labels(category, label):
    assert publish.render_comment(finding(category=category)).startswith(f"_{label}_ | _Major_")


@pytest.mark.parametrize("severity,label", [
    ("critical", "Critical"), ("major", "Major"), ("minor", "Minor"), ("nit", "Nit"),
])
def test_severity_labels(severity, label):
    assert f"| _{label}_ |" in publish.render_comment(finding(severity=severity))


def test_empty_evidence_omitted_and_unknown_language_empty():
    assert "```" not in publish.render_comment(finding(evidence=""))
    assert "```\naccept(value)\n```" in publish.render_comment(finding(file="binary"))


def test_commentable_lines_include_context_and_added_multi_hunk():
    patch = ("--- a/app.py\n+++ b/app.py\n@@ -4,3 +4,3 @@\n context\n-old\n+new\n tail\n"
             "@@ -20,1 +21,2 @@\n+added\n last\n\\ No newline at end of file")
    assert publish.commentable_lines(patch) == {4, 5, 6, 21, 22}
    assert publish.commentable_lines(None) == set()
    assert publish.commentable_lines("@@ -1,1 +0,0 @@\n-deleted") == set()


def test_multiline_span_uses_end_line_and_start_line():
    payload = publish.render_review(HEAD, 7, document(finding(line=9, end_line=11)), FILES)
    comment = payload["comments"][0]
    assert (comment["line"], comment["start_line"], comment["start_side"]) == (11, 9, "RIGHT")
    assert "around line 9-11" in comment["body"]


@pytest.mark.parametrize("updates", [
    {"line": 8}, {"end_line": 15}, {"file": "unseen.py"}, {"end_line": 9},
])
def test_uncommentable_findings_go_to_summary(updates):
    payload = publish.render_review(HEAD, 7, document(finding(**updates)), FILES)
    assert payload["comments"] == []
    assert "Outside the diff" in payload["body"]
    assert f"{updates.get('file', 'src/app.py')}:{updates.get('line', 10)}" in payload["body"]
    assert "Missing check" in payload["body"]
    assert "Validate the input\\." in payload["body"]


def test_span_crossing_hunk_gap_is_outside():
    files = [FilePatch("src/app.py", "modified", "@@ -1 +1 @@\n+a\n@@ -3 +3 @@\n+b")]
    payload = publish.render_review(HEAD, 7, document(finding(line=1, end_line=3)), files)
    assert payload["comments"] == []
    assert "Outside the diff" in payload["body"]


def test_removed_file_not_commentable():
    files = [FilePatch("src/app.py", "removed", FILES[0].patch)]
    assert publish.render_review(HEAD, 7, document(finding()), files)["comments"] == []


def test_nits_never_inline_even_with_nit_threshold():
    payload = publish.render_review(HEAD, 7, document(finding(severity="nit")), FILES,
                                    min_severity="nit")
    assert payload["comments"] == []
    assert "Nit: 1" in payload["body"]
    assert "Below posting threshold: 1" in payload["body"]
    assert "Missing check" not in payload["body"]


def test_cap_prioritizes_severity_and_lists_overflow():
    items = [finding(severity=s, title=s, fingerprint=s) for s in ("minor", "major", "critical")]
    payload = publish.render_review(HEAD, 7, document(*items), FILES, max_comments=1)
    assert len(payload["comments"]) == 1
    assert "**critical**" in payload["comments"][0]["body"]
    assert "Comment limit" in payload["body"]
    assert "**major**" in payload["body"]
    assert "**minor**" in payload["body"]


def test_min_severity_filters_minor():
    payload = publish.render_review(HEAD, 7, document(finding(severity="minor")), FILES,
                                    min_severity="major")
    assert payload["comments"] == []
    assert "Below posting threshold: 1" in payload["body"]


@pytest.mark.parametrize("unscored", [{"confidence": None}, {}], ids=["null", "missing"])
def test_inline_order_uses_severity_then_confidence_with_stable_ties(unscored):
    items = [
        finding(title="Unscored", **unscored),
        finding(title="Minor", severity="minor", confidence=1.0),
        finding(title="Lower confidence", confidence=0.5),
        finding(title="First tied", confidence=0.9),
        finding(title="Critical", severity="critical", confidence=0.1),
        finding(title="Second tied", confidence=0.9),
        finding(title="Zero", confidence=0.0),
    ]
    payload = publish.render_review(HEAD, 7, document(*items), FILES, max_comments=7)
    titles = [comment["body"].split("**")[1] for comment in payload["comments"]]
    assert titles == ["Critical", "First tied", "Second tied", "Lower confidence",
                      "Unscored", "Zero", "Minor"]


def test_render_review_defaults_to_four_inline_comments():
    items = [finding(title=f"Issue {i}", fingerprint=f"fp-{i}") for i in range(6)]
    payload = publish.render_review(HEAD, 7, document(*items), FILES)
    assert len(payload["comments"]) == 4
    assert "### Comment limit" in payload["body"]
    assert payload["body"].count("<!-- argus:fp=") == 2
    assert "**Issue 4**" in payload["body"]
    assert "**Issue 5**" in payload["body"]


def call_publish(http=None, doc=None):
    return publish.publish_review("owner/repo", 42, HEAD, 7, doc or document(finding()), FILES, http=http)


@pytest.mark.parametrize("mode", [None, "0", "invalid"])
def test_disabled_never_calls_http(monkeypatch, mode):
    if mode is not None:
        monkeypatch.setenv("ARGUS_PUBLISH", mode)
    http = Mock()
    assert call_publish(http).status == "skipped"
    assert http.mock_calls == []


@pytest.mark.parametrize("mode", ["1", "dry-run"])
@pytest.mark.parametrize("repos", ["", "other/repo"])
def test_allowlist_required(monkeypatch, mode, repos):
    monkeypatch.setenv("ARGUS_PUBLISH", mode)
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", repos)
    http = Mock()
    assert call_publish(http).status == "skipped"
    assert http.mock_calls == []


def test_dry_run_contains_payload_without_any_http(monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "dry-run")
    monkeypatch.delenv("ARGUS_PUBLISH_TOKEN")
    monkeypatch.setenv("ARGUS_PUBLISH_MAX_COMMENTS", "0")
    http = Mock()
    outcome = call_publish(http)
    assert outcome.status == "dry_run"
    assert outcome.comments_posted == 0
    assert outcome.payload["comments"] == []
    assert "Comment limit" in outcome.payload["body"]
    assert http.mock_calls == []


def test_missing_write_token_never_falls_back(monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    monkeypatch.delenv("ARGUS_PUBLISH_TOKEN")
    monkeypatch.setenv("GITHUB_TOKEN", "read-secret")
    http = Mock()
    outcome = call_publish(http)
    assert outcome.status == "error"
    assert "ARGUS_PUBLISH_TOKEN" in outcome.reason
    assert outcome.payload is not None
    assert http.mock_calls == []


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_post_dedupes_and_paginates(monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer write-secret"
        assert request.extensions["timeout"]["read"] == 15
        if request.method == "GET" and request.url.path.endswith("/42"):
            return httpx.Response(200, json={"head": {"sha": HEAD}})
        if request.method == "GET":
            if request.url.path.endswith("/reviews"):
                return httpx.Response(200, json=[])
            assert request.url.path.endswith("/comments")
            assert request.url.params["per_page"] == "100"
            if request.url.params["page"] == "1":
                return httpx.Response(200, json=[{"body": "unrelated"}] * 100)
            return httpx.Response(200, json=[{"user": {"login": "publisher"}, "body": "Finding\n<!-- argus:fp=fp-one -->"}])
        payload = json.loads(request.content)
        assert payload["event"] == "COMMENT"
        assert payload["commit_id"] == HEAD
        assert len(payload["comments"]) == 1
        assert "fp-two" in payload["comments"][0]["body"]
        assert "Already posted: 1" in payload["body"]
        return httpx.Response(200, json={"id": 8})

    with client(handler) as http:
        outcome = call_publish(http, document(finding(), finding(fingerprint="fp-two")))
    assert (outcome.status, outcome.comments_posted, outcome.already_posted) == ("posted", 1, 1)
    assert len(requests) == 7


def test_same_head_summary_and_nothing_new_skips_post(monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/42"):
            return httpx.Response(200, json={"head": {"sha": HEAD}})
        assert request.method == "GET"
        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json=[{"user": {"login": "publisher"}, "body": "Finding\n<!-- argus:fp=fp-one -->"}])
        if request.url.params["page"] == "1":
            return httpx.Response(200, json=[{"body": "other review"}] * 100)
        return httpx.Response(200, json=[{"user": {"login": "publisher"}, "body": f"<!-- argus:review=2 head={HEAD} -->"}])

    with client(handler) as http:
        outcome = call_publish(http)
    assert outcome.status == "skipped"
    assert outcome.reason == "nothing new"
    assert outcome.already_posted == 1
    assert len(requests) == 4


def test_previous_head_summary_suppresses_repeat_summary(monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")

    posts = []

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/42"):
            return httpx.Response(200, json={"head": {"sha": HEAD}})
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(200, json={})
        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json=[{"user": {"login": "publisher"}, "body": "Finding\n<!-- argus:fp=fp-one -->"}])
        return httpx.Response(200, json=[{"user": {"login": "publisher"}, "body": f"<!-- argus:review=2 head={'b' * 40} -->"}])

    with client(handler) as http:
        outcome = call_publish(http)
    assert outcome.status == "skipped"
    assert outcome.reason == "nothing new"
    assert posts == []
    assert outcome.comments_posted == 0


def test_422_retries_once_moving_comments_to_outside_diff(monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    posts = []

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/42"):
            return httpx.Response(200, json={"head": {"sha": HEAD}})
        if request.method == "GET":
            return httpx.Response(200, json=[])
        payload = json.loads(request.content)
        posts.append(payload)
        return httpx.Response(422 if len(posts) == 1 else 200, json={})

    with client(handler) as http:
        outcome = call_publish(http)
    assert outcome.status == "posted"
    assert outcome.comments_posted == 0
    assert "422" in outcome.reason
    assert len(posts) == 2
    assert len(posts[0]["comments"]) == 1
    assert posts[1]["comments"] == []
    assert "Outside the diff" in posts[1]["body"]
    assert "src/app.py:10" in posts[1]["body"]
    assert "Validate the input\\." in posts[1]["body"]
    assert outcome.payload == posts[1]
    assert posts[1]["body"].endswith(f"<!-- argus:review=7 head={HEAD} -->\n<!-- argus:manifest fp=fp-one ufp= -->")


@pytest.mark.parametrize("failure", ["transport", "get", "post", "retry"])
def test_errors_are_outcomes_and_never_leak_token(monkeypatch, caplog, failure):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    posts = []

    def handler(request):
        if failure == "transport":
            raise RuntimeError("failure write-secret")
        if request.method == "GET" and request.url.path.endswith("/42"):
            return httpx.Response(500 if failure == "get" else 200, json={"head": {"sha": HEAD}})
        if request.method == "GET":
            return httpx.Response(500 if failure == "get" else 200, json=[])
        posts.append(request)
        return httpx.Response(422 if failure == "retry" else 500, json={})

    with client(handler) as http:
        outcome = call_publish(http)
    assert outcome.status == "error"
    assert outcome.comments_posted == 0
    assert outcome.payload is not None
    assert "write-secret" not in outcome.reason + caplog.text
    assert len(posts) <= 2


@pytest.mark.parametrize("key,value", [("ARGUS_PUBLISH_MIN_SEVERITY", "bogus"),
                                       ("ARGUS_PUBLISH_MAX_COMMENTS", "-1"),
                                       ("ARGUS_PUBLISH_MAX_COMMENTS", "abc")])
def test_bad_configuration_returns_error_without_http(monkeypatch, key, value):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    monkeypatch.setenv(key, value)
    http = Mock()
    assert call_publish(http).status == "error"
    assert http.mock_calls == []


def test_default_cap_is_four_and_dry_run_records_summary_placements(monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "dry-run")
    items = [finding(title=f"Issue {i}", fingerprint=f"fp-{i}") for i in range(6)]
    outcome = call_publish(doc=document(*items))
    assert outcome.status == "dry_run"
    assert len(outcome.payload["comments"]) == 4
    assert "### Comment limit" in outcome.payload["body"]
    assert outcome.payload["body"].count("<!-- argus:fp=") == 2
    assert "**Issue 4**" in outcome.payload["body"]
    assert "**Issue 5**" in outcome.payload["body"]
    assert [(item["fingerprint"], item["placement"]) for item in outcome.items] == [
        ("fp-0", "inline"), ("fp-1", "inline"), ("fp-2", "inline"), ("fp-3", "inline"),
        ("fp-4", "summary"), ("fp-5", "summary"),
    ]
    monkeypatch.setenv("ARGUS_PUBLISH_MIN_SEVERITY", "critical")
    outcome = call_publish(doc=document(*items))
    assert outcome.payload["comments"] == []
    assert "Below posting threshold: 6" in outcome.payload["body"]


def test_environment_overrides_default_cap_at_call_time(monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "dry-run")
    items = [finding(title=f"Issue {i}", fingerprint=f"fp-{i}") for i in range(6)]
    for limit in (2, 5):
        monkeypatch.setenv("ARGUS_PUBLISH_MAX_COMMENTS", str(limit))
        outcome = call_publish(doc=document(*items))
        assert outcome.status == "dry_run"
        assert len(outcome.payload["comments"]) == limit
        assert sum(item["placement"] == "summary" for item in outcome.items) == 6 - limit


def test_allowlist_whitespace_multiple_repos_and_case(monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "dry-run")
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", " other/project, OWNER/REPO, ")
    assert call_publish().status == "dry_run"


def test_single_line_end_does_not_emit_start_line():
    payload = publish.render_review(HEAD, 7, document(finding(end_line=10)), FILES)
    comment = payload["comments"][0]
    assert "start_line" not in comment
    assert "start_side" not in comment
    assert "around line 10:" in comment["body"]


def test_summary_counts_all_severities_and_tiers_including_filtered():
    items = [finding(severity="critical", evidence_tier="deterministic"),
             finding(severity="major", evidence_tier="consensus"),
             finding(severity="minor", evidence_tier="single_critic"),
             finding(severity="nit", evidence_tier="disputed")]
    payload = publish.render_review(HEAD, 7, document(*items), FILES)
    assert "Critical: 1; Major: 1; Minor: 1; Nit: 1" in payload["body"]
    assert "deterministic: 1; consensus: 1; single_critic: 1; disputed: 1" in payload["body"]
    assert len(payload["comments"]) == 3


def test_evidence_with_fence_stays_inside_code_block():
    body = publish.render_comment(finding(evidence="```example```"))
    assert "````python\n```example```\n````" in body


def test_clean_same_head_summary_skips_post(monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/42"):
            return httpx.Response(200, json={"head": {"sha": HEAD}})
        assert request.method == "GET"
        data = [] if request.url.path.endswith("/comments") else [{"user": {"login": "publisher"}, "body": f"<!-- argus:review=1 head={HEAD} -->"}]
        return httpx.Response(200, json=data)

    with client(handler) as http:
        outcome = call_publish(http, document())
    assert outcome.status == "skipped"
    assert outcome.reason == "nothing new"


def test_new_outside_finding_still_posts_summary(monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/42"):
            return httpx.Response(200, json={"head": {"sha": HEAD}})
        if request.method == "GET":
            assert request.url.path.endswith(("/comments", "/reviews"))
            return httpx.Response(200, json=[])
        payload = json.loads(request.content)
        assert payload["comments"] == []
        assert "Outside the diff" in payload["body"]
        return httpx.Response(200, json={})

    with client(handler) as http:
        outcome = call_publish(http, document(finding(line=100)))
    assert outcome.status == "posted"


def test_retry_failure_records_original_422_fallback(monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    posts = []

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/42"):
            return httpx.Response(200, json={"head": {"sha": HEAD}})
        if request.method == "GET":
            return httpx.Response(200, json=[])
        posts.append(json.loads(request.content))
        return httpx.Response(422 if len(posts) == 1 else 500, json={})

    with client(handler) as http:
        outcome = call_publish(http)
    assert outcome.status == "error"
    assert "422" in outcome.reason
    assert "Outside the diff" in outcome.reason
    assert outcome.payload == posts[1]
    assert len(posts) == 2


@pytest.mark.parametrize("separator", ["\f", "\u2028", "\r"])
def test_commentable_lines_uses_git_numbering(separator):
    assert publish.commentable_lines(
        f"@@ -1,2 +1,3 @@\n first{separator}continued\n+second\n third") == {1, 2, 3}


@pytest.mark.parametrize("outside,limit,fallback", [(True, 15, False), (False, 0, False), (False, 15, True)])
def test_review_body_fingerprints_are_already_posted(monkeypatch, outside, limit, fallback):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    old = publish.render_review("b" * 40, 1, document(finding(line=99 if outside else 10)),
                                FILES, max_comments=limit, outside_only=fallback)
    assert "argus:fp=fp-one" in old["body"]
    requests = []
    def handler(request):
        requests.append((request.method, request.url.path))
        assert request.method == "GET"
        if request.url.path.endswith("/42"):
            return httpx.Response(200, json={"head": {"sha": HEAD}})
        return httpx.Response(200, json=[{"user": {"login": "publisher"}, "body": old["body"]}] if request.url.path.endswith("/reviews") else [])
    with client(handler) as http:
        outcome = call_publish(http)
    assert outcome.status == "skipped"
    assert outcome.reason == "nothing new"
    assert all(method == "GET" for method, _ in requests)
    assert outcome.already_posted == 1
    assert sum(method == "GET" and path.endswith("/reviews") for method, path in requests) == 1


def test_moved_head_skips_without_post(monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    requests = []
    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path.endswith("/42")
        return httpx.Response(200, json={"head": {"sha": "b" * 40}})
    with client(handler) as http:
        outcome = call_publish(http)
    assert outcome.status == "skipped"
    assert outcome.reason == "PR head moved since the review (aaaaaaa -> bbbbbbb)"
    assert outcome.payload["commit_id"] == HEAD
    assert len(requests) == 1


@pytest.mark.parametrize("failure_at", [None, 1, 2, 3, 4, 5])
def test_publish_responses_preserve_read_quota(monkeypatch, failure_at):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    monkeypatch.setattr(github, "_quota", {"core": {"remaining": 4500, "reset": 9999999999}})
    monkeypatch.setattr(github, "_pause_until", 123.0)
    before = github.quota_snapshot(), github.pause_until()
    requests = []

    def handler(request):
        requests.append(request)
        headers = {"X-RateLimit-Remaining": "10", "X-RateLimit-Reset": "9999999999"}
        if len(requests) == failure_at:
            return httpx.Response(429, headers=headers | {"Retry-After": "120"}, json={})
        if request.method == "POST":
            return httpx.Response(422 if len(requests) == 4 else 200, headers=headers, json={})
        body = {"head": {"sha": HEAD}} if request.url.path.endswith("/42") else []
        return httpx.Response(200, headers=headers, json=body)

    with client(handler) as http:
        outcome = call_publish(http)
    assert (github.quota_snapshot(), github.pause_until()) == before
    if failure_at:
        assert outcome.status == "error"
        assert "GitHubRateLimited" in outcome.reason
        assert len(requests) == failure_at
    else:
        assert outcome.status == "posted"
        assert len(requests) == 5


@pytest.mark.parametrize("clean", [True, False])
def test_first_review_posts_even_without_new_findings(monkeypatch, clean):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    posts = []
    def handler(request):
        if request.method == "POST":
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={})
        if request.url.path.endswith("/42"):
            return httpx.Response(200, json={"head": {"sha": HEAD}})
        body = [{"user": {"login": "publisher"}, "body": "Finding\n<!-- argus:fp=fp-one -->"}] if request.url.path.endswith("/comments") else []
        return httpx.Response(200, json=body)
    with client(handler) as http:
        outcome = call_publish(http, document() if clean else document(finding()))
    assert outcome.status == "posted"
    assert len(posts) == 1 and posts[0]["comments"] == []


def test_same_id_recovery_uses_existing_comments(monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    requests = []
    stamp = "2026-10-14T10:00:00Z"
    def handler(request):
        requests.append(request.url.path)
        assert request.method == "GET"
        if request.url.path.endswith("/reviews/70/comments"):
            raise RuntimeError("listing unavailable")
        if request.url.path.endswith("/42"):
            body = {"head": {"sha": HEAD}}
        elif request.url.path.endswith("/comments"):
            body = [{"id": 502, "user": {"login": "publisher"}, "pull_request_review_id": 70, "path": "src/app.py", "line": 10,
                     "body": "Finding\n<!-- argus:fp=fp-one -->"},
                    {"id": 503, "pull_request_review_id": 71, "body": "other review"}]
        else:
            body = [{"id": 70, "submitted_at": stamp, "user": {"login": "publisher"},
                     "body": f"<!-- argus:review=7 head={HEAD} -->"}]
        return httpx.Response(200, json=body)
    with client(handler) as http:
        outcome = call_publish(http)
    assert outcome.status == "posted" and outcome.posted_at == stamp
    assert outcome.comments_posted == 1 and outcome.reason == "recovered an existing review"
    assert outcome.items[0]["gh_comment_id"] == 502
    assert outcome.gh_review_id == 70 and outcome.bot_login == "publisher"
    assert len(requests) == 3


@pytest.mark.parametrize("status", [None, 500, 502, 503, 504])
def test_publish_transient_failure_never_retries_post(monkeypatch, status):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request)
            if status is None:
                raise httpx.ReadTimeout("connection lost", request=request)
            return httpx.Response(status, json={})
        body = {"head": {"sha": HEAD}} if request.url.path.endswith("/42") else []
        return httpx.Response(200, json=body)

    with client(handler) as http:
        outcome = call_publish(http)
    assert outcome.status == "error"
    assert len(posts) == 1
