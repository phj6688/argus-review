"""Publishing permission checks and collaborator invitation requests."""

from argus import publish_access


def test_check_ok(golden, transport):
    publish_access.clear_cache()
    transport.add("GET", "/repos/example/app", (200, {"permissions": {"push": True}}, {}))

    result = {
        "fresh": publish_access.check_access("example/app"),
        "cached": publish_access.check_access("example/app"),
    }

    assert result["fresh"]["state"] == "ok"
    assert result["cached"] == result["fresh"]
    transport.assert_all_used()
    golden.check("publish-access", "check-ok", result)


def test_check_missing_403_404_401(golden, transport):
    publish_access.clear_cache()
    cases = [("example/forbidden", 403), ("example/missing", 404), ("example/rejected", 401)]
    for repo, status in cases:
        transport.add("GET", f"/repos/{repo}", (status, {"message": "Access unavailable."}, {}))
    transport.add("GET", "/repos/example/readonly", (200, {"permissions": {"push": False}}, {}))

    result = {repo: publish_access.check_access(repo) for repo, _ in cases}
    result["example/readonly"] = publish_access.check_access("example/readonly")

    assert [entry["state"] for entry in result.values()] == ["unknown", "missing", "unknown", "missing"]
    transport.assert_all_used()
    golden.check("publish-access", "check-missing-403-404-401", result)


def test_ensure_invite_flow(golden, transport, monkeypatch):
    publish_access.clear_cache()
    monkeypatch.setenv("ARGUS_PUBLISH_AUTO_INVITE", "1")
    monkeypatch.setenv("ARGUS_WATCH_OWNERS", "example")
    transport.add("GET", "/repos/example/app", [
        (404, {"message": "Repository is not visible."}, {}),
        (200, {"permissions": {"push": True}}, {}),
    ])
    transport.add("GET", "/user", (200, {"login": "example-bot"}, {}))
    transport.add("PUT", "/repos/example/app/collaborators/example-bot", (201, {}, {}))
    transport.add("GET", "/user/repository_invitations", (200, [
        {"id": 90, "repository": {"full_name": "example/other"}},
        {"id": 91, "repository": {"full_name": "example/app"}},
    ], {}), query={"per_page": "100"})
    transport.add("PATCH", "/user/repository_invitations/91", (204, None, {}))

    result = publish_access.ensure_access("example/app")

    assert result["state"] == "ok"
    assert result["grant"] == "granted"
    transport.assert_all_used()
    golden.check("publish-access", "ensure-invite-flow", result)


def test_ensure_invite_off(golden, transport):
    publish_access.clear_cache()
    transport.add("GET", "/repos/example/app", (404, {"message": "Repository is not visible."}, {}))

    result = publish_access.ensure_access("example/app")

    assert result["state"] == "missing"
    assert result["grant"] == "skipped"
    transport.assert_all_used()
    golden.check("publish-access", "ensure-invite-off", result)
