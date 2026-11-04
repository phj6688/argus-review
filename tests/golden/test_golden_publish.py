"""Publishing payloads, duplicate detection, recovery and retries."""

from argus import publish
from argus.diff import FilePatch

HEAD = "a" * 40
BASE = "/repos/example/app/pulls/42"
BOT = "example-bot"
FILES = [FilePatch("src/app.py", "modified", "@@ -9,3 +9,4 @@\n context\n-old\n+new\n+extra\n tail")]
PAGE = {"per_page": "100", "page": "1"}


def finding(**updates):
    return {
        "file": "src/app.py", "line": 10, "end_line": 11,
        "category": "contracts", "severity": "major", "evidence_tier": "consensus",
        "title": "Validate input before saving", "description": "An empty input reaches storage.",
        "evidence": "save(value)", "proposed_fix": "Reject empty input before saving.",
        "confidence": 0.9, "fingerprint": "fp-inline",
    } | updates


def document(*items, unverified=()):
    return {
        "review_id": 7, "source": {"head_sha": HEAD}, "verdict": "changes_requested",
        "coverage": {"level": "partial", "lanes": [
            {"lane": "llm", "status": "ran", "files_reviewed": 2, "files_omitted": 1},
        ]},
        "findings": list(items), "unverified": list(unverified),
    }


def rich_document():
    return document(
        finding(),
        finding(file="src/config.py", line=30, end_line=None, severity="minor",
                evidence_tier="deterministic", title="Set a request timeout",
                fingerprint="fp-outside"),
        finding(severity="nit", title="Use a clearer variable name", fingerprint="fp-nit"),
        unverified=[finding(evidence_tier="disputed", title="Check the fallback value",
                            fingerprint="fp-disputed")],
    )


def enable(monkeypatch, mode="1"):
    monkeypatch.setenv("ARGUS_PUBLISH", mode)
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "example/app")


def register_reads(transport, *, comments=(), reviews=()):
    transport.add("GET", BASE, (200, {"head": {"sha": HEAD}}, {}))
    transport.add("GET", "/user", (200, {"login": BOT}, {}))
    transport.add("GET", BASE + "/comments", (200, list(comments), {}), query=PAGE)
    transport.add("GET", BASE + "/reviews", (200, list(reviews), {}), query=PAGE)


def call_publish(doc):
    return publish.publish_review("example/app", 42, HEAD, 7, doc, FILES)


def test_new_findings(golden, transport, monkeypatch):
    enable(monkeypatch)
    register_reads(transport)
    transport.add("POST", BASE + "/reviews", (200, {
        "id": 301, "submitted_at": "2026-01-15T00:00:00Z", "user": {"login": BOT},
    }, {}))
    transport.add("GET", BASE + "/reviews/301/comments", (200, [{
        "id": 801, "user": {"login": BOT},
        "body": "Stored finding.\n<!-- argus:fp=fp-inline -->",
    }], {}), query=PAGE)

    outcome = call_publish(rich_document())

    assert outcome.status == "posted"
    assert outcome.comments_posted == 1
    assert [(item["fingerprint"], item["placement"]) for item in outcome.items] == [
        ("fp-inline", "inline"), ("fp-outside", "summary"), ("fp-disputed", "summary"),
    ]
    transport.assert_all_used()
    golden.check("publish", "new-findings", outcome)


def test_all_already_posted(golden, transport, monkeypatch):
    enable(monkeypatch)
    register_reads(transport, comments=[{
        "id": 801, "user": {"login": BOT},
        "body": "Stored finding.\n<!-- argus:fp=fp-inline -->",
    }], reviews=[{
        "id": 300, "user": {"login": BOT},
        "body": f"<!-- argus:review=6 head={HEAD} -->\n<!-- argus:manifest fp= ufp= -->",
    }])

    outcome = call_publish(document(finding()))

    assert outcome.status == "skipped"
    assert outcome.reason == "nothing new"
    assert outcome.already_posted == 1
    transport.assert_all_used()
    golden.check("publish", "all-already-posted", outcome)


def test_footer_recovery(golden, transport, monkeypatch):
    enable(monkeypatch)
    register_reads(transport, comments=[{
        "id": 801, "pull_request_review_id": 301, "user": {"login": BOT},
        "path": "src/app.py", "line": None, "original_line": 11,
        "original_start_line": 10, "side": "RIGHT",
        "body": "Stored finding.\n<!-- argus:fp=fp-inline -->",
    }], reviews=[{
        "id": 301, "user": {"login": BOT}, "submitted_at": "2026-01-15T00:00:00Z",
        "body": f"Stored review.\n\n<!-- argus:review=7 head={HEAD} -->\n"
                "<!-- argus:manifest fp= ufp= -->",
    }])

    outcome = call_publish(document(finding()))

    assert outcome.status == "posted"
    assert outcome.reason == "recovered an existing review"
    assert outcome.items[0]["gh_comment_id"] == 801
    transport.assert_all_used()
    golden.check("publish", "footer-recovery", outcome)


def test_retry_422(golden, transport, monkeypatch):
    enable(monkeypatch)
    register_reads(transport)
    transport.add("POST", BASE + "/reviews", [
        (422, {"message": "The selected line is outside the diff."}, {}),
        (200, {"id": 301, "submitted_at": "2026-01-15T00:00:00Z"}, {}),
    ])
    transport.add("GET", BASE + "/reviews/301/comments", (200, [], {}), query=PAGE)

    outcome = call_publish(document(finding()))

    assert outcome.status == "posted"
    assert outcome.comments_posted == 0
    assert outcome.items[0]["placement"] == "summary"
    transport.assert_all_used()
    golden.check("publish", "retry-422", outcome)


def test_dry_run(golden, transport, monkeypatch):
    enable(monkeypatch, "dry-run")

    outcome = call_publish(rich_document())

    assert outcome.status == "dry_run"
    assert golden.requests == []
    transport.assert_all_used()
    golden.check("publish", "dry-run", outcome)


def test_head_moved(golden, transport, monkeypatch):
    enable(monkeypatch)
    transport.add("GET", BASE, (200, {"head": {"sha": "b" * 40}}, {}))

    outcome = call_publish(document(finding()))

    assert outcome.status == "skipped"
    assert "head moved" in outcome.reason
    transport.assert_all_used()
    golden.check("publish", "head-moved", outcome)
