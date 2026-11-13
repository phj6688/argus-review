"""Publish allowlist updates remain useful when GitHub access cannot be granted."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from argus import github, publish
from argus.api import db, main


@pytest.fixture
def api(tmp_path, monkeypatch, caplog):
    from argus import publish_access

    path = str(tmp_path / "argus.db")
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    for key in ("ARGUS_PUBLISH", "ARGUS_PUBLISH_REPOS", "ARGUS_PUBLISH_TOKEN",
                "ARGUS_PUBLISH_BOT_LOGIN", "ARGUS_DENY_OWNERS", "ARGUS_WATCH_OWNERS",
                "ARGUS_WATCH_ENABLED", "GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    # The invite path is opt-in; tests that cover the default-off behaviour delete this.
    monkeypatch.setenv("ARGUS_PUBLISH_AUTO_INVITE", "1")
    db.init_db(path)
    publish_access.clear_cache()
    monkeypatch.setattr(publish_access.httpx, "request", Mock(side_effect=AssertionError("unexpected HTTP")))
    # Startup disables root propagation; capture Argus logs and restore its setup afterwards.
    logger = logging.getLogger("argus")
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    with TestClient(main.app) as client:
        yield client, path, publish_access
    publish_access.clear_cache()


def _mock_github(monkeypatch, access, steps):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, str(url), kwargs))
        expected_method, suffix, code, body = steps.pop(0)
        assert method == expected_method
        assert str(url) == f"https://api.github.com{suffix}"
        return httpx.Response(code, json=body, request=httpx.Request(method, url))

    monkeypatch.setattr(access.httpx, "request", request)
    return calls


def _tokens(monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH_TOKEN", "publish-private-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "reviewer-private-secret")
    monkeypatch.setenv("ARGUS_PUBLISH_BOT_LOGIN", "review-bot")
    monkeypatch.setenv("ARGUS_WATCH_OWNERS", "Example")


def _access(body):
    return body["repos"][0]["access"]


def test_config_defaults_and_env_payload(api, monkeypatch):
    client, _, _ = api
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "B/Two, a/One")
    monkeypatch.setenv("ARGUS_PUBLISH_BOT_LOGIN", "review-bot")
    monkeypatch.setenv("ARGUS_PUBLISH", "dry-run")
    body = client.get("/config").json()["publish"]
    assert set(body) == {"mode", "source", "botLogin", "tokenConfigured", "repos"}
    assert body | {"repos": []} == {
        "mode": "dry-run", "source": "env", "botLogin": "review-bot",
        "tokenConfigured": False, "repos": [],
    }
    assert [repo["name"] for repo in body["repos"]] == ["a/one", "b/two"]
    assert set(_access(body)) == {"state", "detail", "settingsUrl", "grant"}
    assert _access(body)["state"] == "unconfigured"


def test_put_normalizes_and_overrides_env_then_empty_disables(api, monkeypatch):
    client, path, _ = api
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "env/only")
    response = client.put("/config/publish-repos", json={"repos": [" Example/Repo ", "example/REPO", "A/B"]})
    assert response.status_code == 200
    assert response.json() == client.get("/config").json()["publish"]
    assert response.json()["source"] == "dashboard"
    assert response.json()["botLogin"] is None
    assert db.get_publish_repos(path) == ["example/repo", "a/b"]
    assert [repo["name"] for repo in response.json()["repos"]] == ["a/b", "example/repo"]
    assert publish.enabled_for("EXAMPLE/REPO")
    assert not publish.enabled_for("env/only")
    assert client.get("/integrations").json()["github"]["publishing"]["source"] == "dashboard"
    empty = client.put("/config/publish-repos", json={"repos": []}).json()
    assert empty["repos"] == []
    assert empty["source"] == "dashboard"
    assert not publish.enabled_for("env/only")
    assert client.get("/integrations").json()["github"]["publishing"]["repoCount"] == 0


def test_saving_allowlist_does_not_enable_posting(api):
    client, path, _ = api
    response = client.put("/config/publish-repos", json={"repos": ["example/repo"]})
    assert response.status_code == 200
    assert response.json()["mode"] == "off"
    assert db.get_publish_repos(path) == ["example/repo"]
    assert not publish.enabled_for("example/repo")


@pytest.mark.parametrize("name,detail", [
    ("denied-org/x", "repository denied: denied-org/x"),
    ("OtherDenied/x", "repository denied: otherdenied/x"),
    ("example/private", "repository denied: example/private"),
    ("foo", "invalid repository name: foo"),
    ("a/b/c", "invalid repository name: a/b/c"),
    ("../x", "invalid repository name: ../x"),
    ("   ", "invalid repository name: "),
])
@pytest.mark.parametrize("existing", [False, True])
def test_invalid_request_is_atomic(api, monkeypatch, name, detail, existing):
    client, path, _ = api
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "denied-org,otherdenied")
    db.save_config(path, repos=[], model=main.DEFAULT_MODEL, bug_cap=4,
                   min_confidence=0.5, deny=["example/private"])
    if existing:
        db.set_publish_repos(path, ["safe/old"])
    response = client.put("/config/publish-repos", json={"repos": ["safe/new", name]})
    assert response.status_code == 422
    assert response.json() == {"detail": detail}
    assert db.get_publish_repos(path) == (["safe/old"] if existing else None)


def test_validation_before_creating_config_and_list_limit(api):
    client, path, _ = api
    assert client.put("/config/publish-repos", json={"repos": ["foo"]}).status_code == 422
    assert db.get_config(path) is None
    assert client.put("/config/publish-repos", json={"repos": ["a/b"] * 501}).status_code == 422
    assert db.get_config(path) is None


def test_existing_repos_do_not_get_grants(api, monkeypatch):
    client, path, access = api
    db.save_config(path, repos=[], model=main.DEFAULT_MODEL, bug_cap=4, min_confidence=0.5)
    db.set_publish_repos(path, ["example/existing"])
    grant = Mock(side_effect=AssertionError("unexpected grant"))
    monkeypatch.setattr(access, "ensure_access", grant)
    assert client.put("/config/publish-repos", json={"repos": ["example/existing"]}).status_code == 200
    grant.assert_not_called()
    client.put("/config", json={"repos": [], "model": main.DEFAULT_MODEL, "bugCap": 5, "minConfidence": 0.6})
    assert db.get_publish_repos(path) == ["example/existing"]


def test_existing_push_access_makes_no_invitation(api, monkeypatch):
    client, _, access = api
    _tokens(monkeypatch)
    steps = [("GET", "/repos/example/repo", 200, {"permissions": {"push": True}})]
    calls = _mock_github(monkeypatch, access, steps)
    status = _access(client.put("/config/publish-repos", json={"repos": ["example/repo"]}).json())
    assert status["state"] == "ok"
    assert status["grant"] is None
    assert [method for method, _, _ in calls] == ["GET"]
    assert not steps


@pytest.mark.parametrize("direct_id", [True, False])
def test_grants_and_accepts_invitation(api, monkeypatch, caplog, direct_id):
    client, _, access = api
    _tokens(monkeypatch)
    steps = [
        ("GET", "/repos/example/repo", 404, {}),
        ("PUT", "/repos/example/repo/collaborators/review-bot", 201, {"id": 21} if direct_id else {}),
    ]
    if not direct_id:
        steps.append(("GET", "/user/repository_invitations?per_page=100", 200, [
            {"id": 9, "repository": {"full_name": "example/other"}},
            {"id": 21, "repository": {"full_name": "EXAMPLE/REPO"}},
        ]))
    steps.extend([
        ("PATCH", "/user/repository_invitations/21", 204, {}),
        ("GET", "/repos/example/repo", 200, {"permissions": {"push": True}}),
    ])
    calls = _mock_github(monkeypatch, access, steps)
    with caplog.at_level(logging.INFO, logger="argus.publish_access"):
        response = client.put("/config/publish-repos", json={"repos": ["example/repo"]})
    assert response.status_code == 200
    status = _access(response.json())
    assert status == {"state": "ok", "detail": "bot added and invitation accepted",
                      "settingsUrl": "https://github.com/example/repo/settings/access", "grant": "granted"}
    for method, _, kwargs in calls:
        token = "reviewer-private-secret" if method == "PUT" else "publish-private-secret"
        assert kwargs["headers"] == {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": github.API_VERSION}
        assert kwargs["timeout"] == 5.0
        if method == "PUT":
            assert kwargs["json"] == {"permission": "push"}
    assert len([record for record in caplog.records if record.name == "argus.publish_access"]) == 1
    for token in ("publish-private-secret", "reviewer-private-secret"):
        assert token not in response.text
        assert token not in caplog.text
    assert not steps


def test_invitation_refused_still_saves(api, monkeypatch):
    client, path, access = api
    _tokens(monkeypatch)
    steps = [("GET", "/repos/example/repo", 404, {}),
             ("PUT", "/repos/example/repo/collaborators/review-bot", 403, {"message": "reviewer-private-secret"})]
    _mock_github(monkeypatch, access, steps)
    response = client.put("/config/publish-repos", json={"repos": ["example/repo"]})
    assert response.status_code == 200
    assert db.get_publish_repos(path) == ["example/repo"]
    assert _access(response.json()) == {
        "state": "missing", "grant": "failed", "settingsUrl": "https://github.com/example/repo/settings/access",
        "detail": "reviewer token cannot add collaborators here (status 403)",
    }
    assert not steps


def test_unwatched_owner_is_skipped(api, monkeypatch):
    client, _, access = api
    _tokens(monkeypatch)
    monkeypatch.setenv("ARGUS_WATCH_OWNERS", "someone-else")
    calls = _mock_github(monkeypatch, access, [("GET", "/repos/example/repo", 404, {})])
    status = _access(client.put("/config/publish-repos", json={"repos": ["example/repo"]}).json())
    assert status["grant"] == "skipped"
    assert status["detail"] == "owner is not in ARGUS_WATCH_OWNERS, add the bot by hand"
    assert len(calls) == 1


def test_no_publish_token_never_calls_network(api, monkeypatch):
    client, _, access = api
    monkeypatch.setenv("ARGUS_PUBLISH_TOKEN", "   ")
    status = _access(client.put("/config/publish-repos", json={"repos": ["example/repo"]}).json())
    assert status["state"] == "unconfigured"
    assert status["detail"] == "ARGUS_PUBLISH_TOKEN is not set"
    assert status["grant"] is None
    access.httpx.request.assert_not_called()


@pytest.mark.parametrize("code,body,state,detail", [
    (200, {"permissions": {"push": False}}, "missing", "bot has no write access"),
    (404, {}, "missing", "bot cannot see this repository"),
    (401, {}, "unknown", "publish token was rejected"),
    (403, {}, "unknown", "GitHub refused the check (status 403)"),
    (429, {}, "unknown", "GitHub refused the check (status 429)"),
    (500, {"message": "publish-private-secret"}, "unknown", "GitHub access check failed (status 500)"),
])
def test_access_status_mapping(api, monkeypatch, code, body, state, detail):
    _, _, access = api
    _tokens(monkeypatch)
    _mock_github(monkeypatch, access, [("GET", "/repos/example/repo", code, body)])
    status = access.check_access("example/repo")
    assert status["state"] == state
    assert status["detail"] == detail
    assert status["grant"] is None


@pytest.mark.parametrize("error", [httpx.ConnectError("publish-private-secret"),
    github.GitHubDenied("publish-private-secret"), ValueError("publish-private-secret"),
    github.GitHubAuthError("publish-private-secret"), github.GitHubRateLimited(0),
    RuntimeError("publish-private-secret")])
def test_access_errors_are_sanitized(api, monkeypatch, caplog, error):
    client, _, access = api
    _tokens(monkeypatch)
    monkeypatch.setattr(access, "_request", Mock(side_effect=error))
    with caplog.at_level(logging.INFO):
        response = client.put("/config/publish-repos", json={"repos": ["example/repo"]})
    assert response.status_code == 200
    assert _access(response.json())["state"] == "unknown"
    assert "publish-private-secret" not in response.text + caplog.text


@pytest.mark.parametrize("push,ttl", [(True, 600), (False, 60)])
def test_cache_ttl_fresh_casefold_and_defensive_copy(api, monkeypatch, push, ttl):
    _, _, access = api
    _tokens(monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(access.time, "monotonic", lambda: clock[0])
    request = Mock(return_value=httpx.Response(200, json={"permissions": {"push": push}}))
    monkeypatch.setattr(access, "_request", request)
    first = access.check_access("Example/Repo")
    first["state"] = "mutated"
    assert access.check_access("example/repo")["state"] == ("ok" if push else "missing")
    clock[0] = ttl - 1
    access.check_access("example/repo")
    assert request.call_count == 1
    clock[0] = ttl
    access.check_access("example/repo")
    assert request.call_count == 2
    access.check_access("example/repo", fresh=True)
    assert request.call_count == 3
    access.clear_cache()
    access.check_access("example/repo")
    assert request.call_count == 4


def test_cache_is_safe_across_threads(api, monkeypatch):
    _, _, access = api
    _tokens(monkeypatch)
    monkeypatch.setattr(access, "_request", Mock(return_value=httpx.Response(200, json={"permissions": {"push": True}})))
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(access.check_access, ["example/repo"] * 50))
    assert all(status["state"] == "ok" for status in statuses)
    assert all(status["grant"] is None for status in statuses)


def test_config_access_checks_are_bounded_and_concurrent(api, monkeypatch):
    client, _, access = api
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", ",".join(f"example/repo{i}" for i in range(12)))
    barrier = threading.Barrier(8, timeout=5)
    lock = threading.Lock()
    count = [0]

    def check(repo):
        with lock:
            count[0] += 1
            number = count[0]
        if number <= 8:
            barrier.wait()
        return {"state": "ok", "detail": "bot can push", "settingsUrl": access.settings_url(repo), "grant": None}

    monkeypatch.setattr(access, "check_access", check)
    response = client.get("/config")
    assert response.status_code == 200
    assert len(response.json()["publish"]["repos"]) == 12


@pytest.mark.parametrize("code", [403, 404, 422, 500])
def test_invitation_accept_failure_has_fixed_details(api, monkeypatch, caplog, code):
    client, path, access = api
    _tokens(monkeypatch)
    steps = [("GET", "/repos/example/repo", 404, {}),
             ("PUT", "/repos/example/repo/collaborators/review-bot", 201, {"id": 3}),
             ("PATCH", "/user/repository_invitations/3", code, {"message": "publish-private-secret"})]
    _mock_github(monkeypatch, access, steps)
    with caplog.at_level(logging.INFO):
        response = client.put("/config/publish-repos", json={"repos": ["example/repo"]})
    assert response.status_code == 200
    status = _access(response.json())
    assert status["state"] == "missing"
    assert status["grant"] == "failed"
    assert status["detail"] == f"invitation sent but the bot could not accept it (status {code})"
    assert "publish-private-secret" not in response.text + caplog.text
    assert db.get_publish_repos(path) == ["example/repo"]
    assert not steps


@pytest.mark.parametrize("recheck", [200, 404])
def test_already_collaborator_rechecks_access(api, monkeypatch, recheck):
    _, _, access = api
    _tokens(monkeypatch)
    steps = [("GET", "/repos/example/repo", 404, {}),
             ("PUT", "/repos/example/repo/collaborators/review-bot", 204, {}),
             ("GET", "/repos/example/repo", recheck, {"permissions": {"push": True}})]
    _mock_github(monkeypatch, access, steps)
    status = access.ensure_access("example/repo")
    assert status["state"] == ("ok" if recheck == 200 else "missing")
    assert status["grant"] == ("granted" if recheck == 200 else "failed")
    assert not steps


@pytest.mark.parametrize("bot", ["bad/login", "-bad", "a" * 40, "bot\n", None])
def test_bad_bot_login_never_invites(api, monkeypatch, bot):
    _, _, access = api
    _tokens(monkeypatch)
    calls = _mock_github(monkeypatch, access, [("GET", "/repos/example/repo", 404, {})])
    monkeypatch.setattr(publish, "_bot_login", lambda: bot)
    status = access.ensure_access("example/repo")
    assert status["grant"] == "failed"
    assert status["detail"] == "bot login could not be resolved"
    assert len(calls) == 1


def test_missing_reviewer_token_never_invites(api, monkeypatch):
    _, _, access = api
    _tokens(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "  ")
    calls = _mock_github(monkeypatch, access, [("GET", "/repos/example/repo", 404, {})])
    status = access.ensure_access("example/repo")
    assert status["state"] == "missing"
    assert status["grant"] == "failed"
    assert status["detail"] == "GITHUB_TOKEN is not set"
    assert len(calls) == 1


def test_request_guards_and_observes_each_token_separately(api, monkeypatch):
    _, _, access = api
    _tokens(monkeypatch)
    guard = Mock(wraps=github.guard_url)
    observe = Mock(wraps=github.observe_response)
    monkeypatch.setattr(github, "guard_url", guard)
    monkeypatch.setattr(github, "observe_response", observe)
    steps = [("GET", "/repos/example/repo", 404, {}),
             ("PUT", "/repos/example/repo/collaborators/review-bot", 201, {"id": 8}),
             ("PATCH", "/user/repository_invitations/8", 204, {}),
             ("GET", "/repos/example/repo", 200, {"permissions": {"push": True}})]
    calls = _mock_github(monkeypatch, access, steps)
    assert access.ensure_access("example/repo")["grant"] == "granted"
    assert guard.call_count >= len(calls)
    assert [call.kwargs for call in observe.call_args_list] == [
        {"record_quota": False}, {}, {"record_quota": False}, {"record_quota": False},
    ]


def test_ensure_unexpected_failure_does_not_leak_or_raise(api, monkeypatch, caplog):
    client, path, access = api
    _tokens(monkeypatch)
    _mock_github(monkeypatch, access, [("GET", "/repos/example/repo", 404, {})])
    monkeypatch.setattr(access.repo_policy, "watch_owners", Mock(side_effect=RuntimeError("publish-private-secret")))
    with caplog.at_level(logging.INFO):
        response = client.put("/config/publish-repos", json={"repos": ["example/repo"]})
    assert response.status_code == 200
    assert _access(response.json())["grant"] == "failed"
    assert "publish-private-secret" not in response.text + caplog.text
    assert db.get_publish_repos(path) == ["example/repo"]


def test_grants_per_request_are_capped(api, monkeypatch):
    client, path, access = api
    granted = []

    def grant(repo):
        granted.append(repo)
        return access._status(repo, "ok", "bot can push")

    monkeypatch.setattr(access, "ensure_access", grant)
    monkeypatch.setattr(access, "check_access", lambda repo, **_: access._status(repo, "ok", "bot can push"))
    names = [f"example/r{i:02d}" for i in range(main._MAX_GRANTS_PER_REQUEST + 5)]
    body = client.put("/config/publish-repos", json={"repos": names}).json()
    assert len(body["repos"]) == len(names)
    assert sorted(granted) == names[:main._MAX_GRANTS_PER_REQUEST]


def test_env_screening_hides_bad_entries_and_first_save_succeeds(api, monkeypatch):
    client, path, access = api
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "env/good,foo,blocked/repo")
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "blocked")
    seen = []
    monkeypatch.setattr(access, "check_access",
                        lambda repo, **_: seen.append(repo) or access._status(repo, "ok", "bot can push"))
    body = client.get("/config").json()["publish"]
    assert body["source"] == "env"
    assert [r["name"] for r in body["repos"]] == ["env/good"]
    assert seen == ["env/good"]
    # The UI echoes the displayed list back on its first move; it must not 422.
    saved = client.put("/config/publish-repos", json={"repos": ["env/good", "env/new"]})
    assert saved.status_code == 200
    assert saved.json()["source"] == "dashboard"


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes"])
def test_auto_invite_off_makes_no_write_call(api, monkeypatch, value):
    client, _, access = api
    _tokens(monkeypatch)
    if value is None:
        monkeypatch.delenv("ARGUS_PUBLISH_AUTO_INVITE")
    else:
        monkeypatch.setenv("ARGUS_PUBLISH_AUTO_INVITE", value)
    calls = _mock_github(monkeypatch, access, [("GET", "/repos/example/repo", 404, {})])
    response = client.put("/config/publish-repos", json={"repos": ["example/repo"]})
    assert response.status_code == 200
    status = _access(response.json())
    assert status["state"] == "missing"
    assert status["grant"] == "skipped"
    assert "ARGUS_PUBLISH_AUTO_INVITE" in status["detail"]
    assert status["settingsUrl"] == "https://github.com/example/repo/settings/access"
    assert [method for method, _, _ in calls] == ["GET"]
    assert not any("collaborators" in url for _, url, _ in calls)


def test_auto_invite_flag_in_integrations(api, monkeypatch):
    client, _, _ = api
    assert client.get("/integrations").json()["github"]["publishing"]["autoInvite"] is True
    monkeypatch.setenv("ARGUS_PUBLISH_AUTO_INVITE", "0")
    assert client.get("/integrations").json()["github"]["publishing"]["autoInvite"] is False
