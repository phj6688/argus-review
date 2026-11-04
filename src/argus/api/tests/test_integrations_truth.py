"""E2-b: /integrations advertises only real routes and the real MCP tool names
(imported from the server), never the fabricated set."""
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from argus.api import db, main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_DB_PATH", str(tmp_path / "argus.db"))
    db.init_db(str(tmp_path / "argus.db"))
    return TestClient(main.app)


def test_mcp_tools_are_real_and_no_fakes(client):
    tools = set(client.get("/integrations").json()["mcp"]["tools"])
    assert tools == set(main._mcp_tool_names())
    fakes = {"list_open_findings", "get_pr_review", "search_findings",
             "get_report", "rerun_review"}
    assert tools.isdisjoint(fakes)


def test_endpoints_are_a_superset_of_real_routes(client):
    advertised = {
        (e["method"], e["path"])
        for e in client.get("/integrations").json()["api"]["endpoints"]
    }
    real = {
        (method, route.path)
        for route in main.app.routes if isinstance(route, APIRoute)
        for method in (route.methods - {"HEAD", "OPTIONS"})
    }
    assert real <= advertised


SECRETS = {
    "GITHUB_TOKEN": "ghp_secretgithubtokenvalue0123456789",
    "ARGUS_API_TOKEN": "a" * 16 + "deadbeefcafe" + "b" * 36,
    "OPENROUTER_API_KEY": "sk-or-v1-secretopenrouterkey0123456789",
    "ARGUS_PUBLISH_TOKEN": "ghp_secretpublishtokenvalue987654321",
}
CLEARED = (
    "ARGUS_PROVIDER", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "ARGUS_WATCH_ENABLED", "ARGUS_WATCH_OWNERS",
    "ARGUS_WATCH_INTERVAL", "ARGUS_PUBLISH", "ARGUS_PUBLISH_REPOS", "ARGUS_PUBLISH_TOKEN", "ARGUS_PUBLISH_AUTO_INVITE",
    "GITHUB_TOKEN", "OPENROUTER_API_KEY",
    "ARGUS_GITLAB_URL", "ARGUS_GITLAB_TOKEN", "ARGUS_GITLAB_ALLOW", "ARGUS_DENY_OWNERS",
)


@pytest.fixture
def clean_env(monkeypatch):
    for name in CLEARED:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _health(body):
    return {row["label"]: row for row in body["health"]}


def test_no_secret_value_appears_in_the_payload(client, clean_env):
    for name, value in SECRETS.items():
        clean_env.setenv(name, value)
    clean_env.setenv("ARGUS_AUTH_MODE", "token")
    clean_env.setenv("ARGUS_PUBLISH", "1")
    clean_env.setenv("ARGUS_PUBLISH_REPOS", "acme/one,acme/two")
    response = client.get("/integrations", headers={"Authorization": f"Bearer {SECRETS['ARGUS_API_TOKEN']}"})
    for name, value in SECRETS.items():
        assert value not in response.text, name
        assert value[:8] not in response.text, name
    body = response.json()
    assert _health(body)["GitHub token"]["status"] == "configured"
    assert "tokenMasked" not in body["api"]
    assert body["api"]["auth"] == "token"


def test_empty_env_reports_missing_and_off(client, clean_env):
    body = client.get("/integrations").json()
    rows = _health(body)
    assert rows["GitHub token"] == {"label": "GitHub token", "status": "missing", "state": "warn"}
    assert rows["PR watcher"]["status"] == "off"
    assert rows["Publishing"]["status"] == "off"
    assert rows["Model provider"]["status"] == "not configured"
    assert rows["Auth"]["status"] == "proxy"
    assert body["mcp"]["status"] == "stdio"
    assert body["github"]["token"] == "missing"
    assert body["api"]["auth"] == "proxy"


def test_provider_and_watcher_rows_reflect_env(client, clean_env):
    clean_env.setenv("OPENROUTER_API_KEY", SECRETS["OPENROUTER_API_KEY"])
    clean_env.setenv("ARGUS_PROVIDER", "openrouter")
    clean_env.setenv("ARGUS_WATCH_ENABLED", "1")
    clean_env.setenv("ARGUS_WATCH_OWNERS", "acme, Widgets")
    clean_env.setenv("ARGUS_WATCH_INTERVAL", "120")
    clean_env.setenv("ARGUS_AUTH_MODE", "proxy")
    body = client.get("/integrations").json()
    rows = _health(body)
    assert rows["Model provider"]["status"].startswith("openrouter · ")
    assert rows["Model provider"]["state"] == "ok"
    assert rows["PR watcher"]["status"] == "on · 2 owners · every 120s"
    assert body["github"]["watcher"] == {
        "enabled": True, "owners": ["acme", "widgets"], "intervalSeconds": 120, "watchedRepos": 0,
    }
    assert rows["Auth"]["status"] == "proxy"
    assert body["api"]["auth"] == "proxy"


def test_publishing_modes(client, clean_env):
    clean_env.setenv("ARGUS_PUBLISH", "dry-run")
    clean_env.setenv("ARGUS_PUBLISH_REPOS", "acme/one, acme/two")
    body = client.get("/integrations").json()
    assert _health(body)["Publishing"]["status"] == "dry-run · 2 repos"
    assert body["github"]["publishing"] == {
        "mode": "dry-run", "repoCount": 2, "tokenConfigured": False, "source": "env", "autoInvite": False,
    }
    clean_env.setenv("ARGUS_PUBLISH", "1")
    assert _health(client.get("/integrations").json())["Publishing"]["state"] == "warn"
    clean_env.setenv("ARGUS_PUBLISH_TOKEN", SECRETS["ARGUS_PUBLISH_TOKEN"])
    row = _health(client.get("/integrations").json())["Publishing"]
    assert row == {"label": "Publishing", "status": "on · 2 repos", "state": "ok"}


def test_no_webhook_or_github_app_fields(client, clean_env):
    raw = client.get("/integrations").text.lower()
    assert "webhook" not in raw
    assert "github app" not in raw


def test_base_url_drops_credentials(client, clean_env, monkeypatch):
    monkeypatch.setattr(main, "API_BASE_URL", "https://user:pw-secret@argus.example.com:8443/api?key=abc#frag")
    body = client.get("/integrations").json()
    assert body["api"]["baseUrl"] == "https://argus.example.com:8443/api"
    assert "pw-secret" not in str(body)


@pytest.mark.parametrize("bad", ["https://[broken", "https://user:pw@host:notaport/x", "not a url"])
def test_malformed_base_url_returns_empty_not_500(client, clean_env, monkeypatch, bad):
    monkeypatch.setattr(main, "API_BASE_URL", bad)
    response = client.get("/integrations")
    assert response.status_code == 200
    assert response.json()["api"]["baseUrl"] == ""


def test_gitlab_unset_preserves_exact_response_bytes(client, clean_env):
    response = client.get("/integrations")
    assert set(response.json()) == {"health", "warning", "github", "api", "mcp"}
    assert "gitlab" not in response.json()
    clean_env.setenv("ARGUS_GITLAB_URL", "")
    clean_env.setenv("ARGUS_GITLAB_ALLOW", "invalid")
    assert client.get("/integrations").content == response.content


def test_gitlab_valid_policy_has_no_refusal(client, clean_env):
    clean_env.setenv("ARGUS_GITLAB_URL", "https://gitlab.example.test")
    clean_env.setenv("ARGUS_GITLAB_ALLOW", "gitlab:gitlab.example.test/group")
    block = client.get("/integrations").json()["gitlab"]
    assert block == {
        "host": "gitlab.example.test", "baseUrl": "https://gitlab.example.test", "token": "missing",
        "identity": {"status": "skipped", "reason": "token_missing"},
        "publish": {"mode": "0", "projectCount": 0}, "allowCount": 1, "refusal": None,
        "trustUnknown": 0,
        "watch": {"enabled": False, "projectCount": 0, "lastPolledAt": None,
                  "lastError": None, "trustUnknown": 0, "forkSourceDenied": 0},
    }


def test_gitlab_invalid_policy_reports_safe_refusal(client, clean_env):
    from contextlib import closing

    import httpx
    from argus.gitlab import client as client_module, status

    token = "gl" + "pat-" + "x" * 20
    clean_env.setenv("ARGUS_GITLAB_URL", "https://gitlab.example.test")
    clean_env.setenv("ARGUS_GITLAB_TOKEN", token)
    clean_env.setenv("ARGUS_GITLAB_ALLOW", "https://private.test/" + token)
    clean_env.setattr(status, "_identity_cache", None)
    with closing(client_module.GitLabClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(401)))) as connection:
        clean_env.setattr(client_module, "client", lambda: connection)
        response = client.get("/integrations")
    block = response.json()["gitlab"]
    assert block["refusal"] == "ARGUS_GITLAB_ALLOW: invalid entry"
    assert set(block) == {"host", "baseUrl", "token", "identity", "publish", "allowCount", "refusal",
                          "trustUnknown", "watch"}
    assert block["token"] == "configured"
    assert block["identity"] == {"status": "failed", "reason": "gitlab_auth"}
    assert token not in response.text
    assert "https://private.test/" not in response.text
    assert block["baseUrl"] == "https://gitlab.example.test"


def test_gitlab_base_url_preserves_prefix_and_port(client, clean_env):
    clean_env.setenv("ARGUS_GITLAB_URL", "https://gitlab.example.test:8443/gitlab/")
    block = client.get("/integrations").json()["gitlab"]
    assert block["host"] == "gitlab.example.test:8443"
    assert block["baseUrl"] == "https://gitlab.example.test:8443/gitlab"


def test_gitlab_unknown_trust_health_warning(client, clean_env):
    from argus.api import watch_store

    clean_env.setenv("ARGUS_GITLAB_URL", "https://gitlab.example.test")
    clean_env.setenv("ARGUS_WATCH_TRUSTED_ONLY", "1")
    before = client.get("/integrations").json()["health"]
    assert "GitLab trust" not in {row["label"] for row in before}
    watch_store.record_changes(db.db_path(), "gitlab:gitlab.example.test#42", [{
        "number": 1, "head": {"sha": "a" * 40},
        "user": {"login": "argus-dev", "type": "User"}, "author_trusted": None,
    }], [], False)
    body = client.get("/integrations").json()
    assert body["gitlab"]["trustUnknown"] == 1
    assert body["health"] == before + [{
        "label": "GitLab trust", "status": "1 merge requests skipped, membership unknown", "state": "warn",
    }]
