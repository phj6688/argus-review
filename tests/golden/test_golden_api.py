"""Capture API responses with real persistence, reviews, and publishing."""

from fastapi.testclient import TestClient

from argus.api import db, main
from argus.review import ReviewResult
from argus.schema import Finding, Verdict, VerifiedFinding

from .harness import register_review


def _response(response):
    return {"status": response.status_code, "json": response.json()}


def _config(db_path, **extra):
    db.save_config(
        db_path, repos=[], model="model-a", bug_cap=5, min_confidence=0.55, **extra
    )


def test_create_review(golden, transport, provider, db_path):
    _config(db_path)
    register_review(transport, provider, metadata_calls=2)
    with TestClient(main.app) as client:
        created = client.post("/reviews", json={"repo": "example/app", "pr": 7})
        assert created.status_code == 201
        review_id = created.json()["id"]
        detail = client.get(f"/reviews/{review_id}")
        assert db.get_review(db_path, review_id)["status"] == "done"
        result = {"created": _response(created), "detail": _response(detail)}
    transport.assert_all_used()
    golden.check("api", "create-review", result)


def test_publish_completed_review(golden, transport, db_path, monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "example/app")
    monkeypatch.setenv("ARGUS_PUBLISH_BOT_LOGIN", "review-bot")
    _config(db_path)
    item = VerifiedFinding(
        finding=Finding(
            file="src/app.py", line=2, severity="major", confidence=0.92,
            title="Missing input check", description="The input is accepted without validation.",
            proposed_fix="Validate the value before using it.", category="input_handling",
            evidence="return accept(value)",
        ),
        verdict=Verdict(is_real=True, confidence=0.96, reasoning="The new path lacks validation."),
        fingerprint="fp-api-input",
    )
    review_id = db.create_review(db_path, "example/app", 7, "model-a", head_sha="a" * 40)
    db.save_result(
        db_path, review_id,
        ReviewResult(
            raw_count=1, bugs=[item], all_findings=[item], head_sha="a" * 40,
            files_changed=1, lines_added=1, lines_removed=1,
            coverage=[{"lane": "llm", "status": "ran", "files_reviewed": 1, "files_omitted": 0}],
        ),
    )
    transport.add("GET", "/repos/example/app/pulls/7/files", (200, [{
        "filename": "src/app.py", "status": "modified", "additions": 1, "deletions": 1,
        "patch": "@@ -1,2 +1,2 @@\n def run(value):\n-    return validate(value)\n+    return accept(value)\n",
    }], {}))
    transport.add("GET", "/repos/example/app/pulls/7", (200, {"head": {"sha": "a" * 40}}, {}))
    transport.add("GET", "/repos/example/app/pulls/7/comments", (200, [], {}))
    transport.add("GET", "/repos/example/app/pulls/7/reviews", (200, [], {}))
    transport.add("POST", "/repos/example/app/pulls/7/reviews", (200, {
        "id": 81, "submitted_at": "2026-01-15T00:00:00Z",
    }, {}))
    transport.add("GET", "/repos/example/app/pulls/7/reviews/81/comments", (200, [{
        "id": 91, "user": {"login": "review-bot"}, "path": "src/app.py", "line": 2,
        "body": "Posted input check\n<!-- argus:fp=fp-api-input -->",
    }], {}))
    with TestClient(main.app) as client:
        outcome = main._publish_completed_review(db_path, review_id, "example/app", 7)
        assert db.get_review(db_path, review_id)["publish_status"] == "posted"
        detail = client.get(f"/reviews/{review_id}")
        result = {"return": outcome, "detail": _response(detail)}
    transport.assert_all_used()
    golden.check("api", "publish-completed-review", result)


def test_integrations(golden, transport, db_path, monkeypatch):
    _config(db_path)
    monkeypatch.setenv("ARGUS_WATCH_OWNERS", "example,acme-org")
    monkeypatch.setenv("ARGUS_PUBLISH", "dry-run")
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "example/app")
    with TestClient(main.app) as client:
        result = client.get("/integrations")
    assert result.status_code == 200
    transport.assert_all_used()
    golden.check("api", "integrations", _response(result))


def test_config_update_deny(golden, transport, db_path):
    _config(db_path)
    with TestClient(main.app) as client:
        updated = client.put("/config", json={
            "repos": [{"name": "example/app", "watched": True}],
            "deny": ["acme-org/private", "example/blocked"], "watch_mode": "manual",
            "model": "model-a", "bugCap": 7, "minConfidence": 0.7,
        })
        assert updated.status_code == 200
        loaded = client.get("/config")
        assert loaded.json()["deny"] == ["acme-org/private", "example/blocked"]
        result = {"updated": _response(updated), "loaded": _response(loaded)}
    transport.assert_all_used()
    golden.check("api", "config-update-deny", result)


def test_publish_repos(golden, transport, db_path, monkeypatch):
    _config(db_path)
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    transport.add("GET", "/repos/example/app", (200, {"permissions": {"push": True}}, {}))
    with TestClient(main.app) as client:
        denied = client.put(
            "/config/publish-repos", json={"repos": ["example/app", "denied-org/private"]}
        )
        assert denied.status_code == 422
        assert db.get_publish_repos(db_path) is None
        allowed = client.put(
            "/config/publish-repos", json={"repos": [" Example/App ", "example/APP"]}
        )
        assert allowed.status_code == 200
        loaded = client.get("/config")
        assert loaded.json()["publish"]["repos"][0]["access"]["state"] == "ok"
        result = {
            "denied": _response(denied), "allowed": _response(allowed), "loaded": _response(loaded)
        }
    transport.assert_all_used()
    golden.check("api", "publish-repos", result)
