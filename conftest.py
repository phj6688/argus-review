from types import SimpleNamespace

import pytest

from argus import github


def stub_github_client(monkeypatch, *, get=None, post=None):
    def unexpected(*args, **kwargs):
        pytest.fail("Unexpected GitHub HTTP request")

    client = SimpleNamespace(
        get=get if get is not None else unexpected,
        post=post if post is not None else unexpected,
    )
    monkeypatch.setattr(github, "_client", lambda: client)
    return client


@pytest.fixture(autouse=True)
def api_auth_mode(monkeypatch):
    # Existing tests exercise endpoint behavior; auth tests opt into token mode.
    monkeypatch.setenv("ARGUS_AUTH_MODE", "proxy")


@pytest.fixture(autouse=True)
def model_provider(monkeypatch):
    # POST /reviews refuses to start without a provider. Tests that stub the
    # engine still need to pass that check; tests about a missing provider clear
    # these variables themselves.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://gateway.test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-token")


@pytest.fixture
def gitlab_event(monkeypatch, tmp_path):
    import os

    import httpx

    from argus import review
    from argus.api import db
    from argus.gitlab import client as client_module, projects
    from argus.gitlab.reader import HOUSE_RULE_FILES

    for name in list(os.environ):
        if name.startswith("ARGUS_GITLAB_"):
            monkeypatch.delenv(name)
    host = "gitlab.example.test:8443"
    prefix = "/gitlab/api/v4"
    key = f"gitlab:{host}#42"
    token = "gl" + "pat-" + "x" * 20
    monkeypatch.setenv("ARGUS_GITLAB_URL", f"https://{host}/gitlab")
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", token)
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", key)
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "blockedorg")
    monkeypatch.setenv("ARGUS_PUBLISH", "0")
    monkeypatch.delenv("ARGUS_FALLBACK_MODEL", raising=False)
    path = str(tmp_path / "entry.db")
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    db.init_db(path)
    project = prefix + "/projects/42"
    mr = project + "/merge_requests/7"
    head = "a" * 40
    state = SimpleNamespace(
        host=host, key=key, path_key=f"gitlab:{host}/allowed/sub/app",
        url=f"https://{host}/gitlab/allowed/sub/app/-/merge_requests/7",
        token=token, path=path, prefix=prefix, project=project, mr=mr, head=head,
        requests=[], responses={
            project: {"id": 42, "path_with_namespace": "allowed/sub/app", "archived": False,
                      "default_branch": "main", "visibility": "private"},
            prefix + "/projects/allowed/sub/app": {"id": 42},
            mr: {"sha": head, "state": "opened", "title": "Change", "description": "",
                 "diff_refs": {"base_sha": "b" * 40}, "source_project_id": 42,
                 "source_branch": "feature", "changes_count": "1"},
            mr + "/versions": [{"id": 1, "head_commit_sha": head,
                                  "base_commit_sha": "b" * 40, "start_commit_sha": "c" * 40}],
            mr + "/diffs": [{"new_path": "note.txt", "old_path": "note.txt", "new_file": True,
                              "diff": "@@ -0,0 +1 @@\n+hello\n", "a_mode": "0", "b_mode": "100644"}],
            project + "/repository/files/note.txt/raw": httpx.Response(200, text="hello\n"),
            project + f"/repository/files/{HOUSE_RULE_FILES[0]}/raw": httpx.Response(404),
            prefix + "/user": {"username": "review-bot", "bot": True},
        },
    )

    def handle(request):
        state.requests.append(request)
        assert request.method == "GET"
        assert request.url.path in state.responses, request.url.path
        response = state.responses[request.url.path]
        if callable(response):
            return response(request)
        return response if isinstance(response, httpx.Response) else httpx.Response(200, json=response)

    def unexpected(*args, **kwargs):
        pytest.fail("Unexpected network or model request")

    client = client_module.GitLabClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(client_module.GitLabClient, "retry_delay", 0)
    monkeypatch.setattr(client_module, "client", lambda: client)
    monkeypatch.setattr(projects, "shared_client", lambda: client)
    monkeypatch.setattr(github, "get", unexpected)
    monkeypatch.setattr(review, "complete_tool", unexpected)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", unexpected)

    def deny(kind):
        if kind == "denied":
            db.save_config(path, repos=[], model="test", bug_cap=5, min_confidence=0.5, deny=[key])
        elif kind == "not_allowlisted":
            monkeypatch.setenv("ARGUS_GITLAB_ALLOW", f"gitlab:{host}#99")
        elif kind == "hard_denied":
            state.responses[project]["path_with_namespace"] = "BlockedOrg/app"
        elif kind == "fork":
            state.responses[mr]["source_project_id"] = 99
            state.responses[prefix + "/projects/99"] = {
                "id": 99, "path_with_namespace": "BlockedOrg/fork", "archived": False,
                "default_branch": "main", "visibility": "private",
            }
            return [project, mr, project, prefix + "/projects/99"]
        else:
            raise AssertionError(kind)
        return [project]

    state.deny = deny
    yield state
    client.close()
