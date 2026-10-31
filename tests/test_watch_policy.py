import httpx
import pytest

from argus import github
from argus.repo_policy import RepoDenied, is_denied, validate_repo


@pytest.mark.parametrize("name", ["example/argus", "a-b/a.b_c-d", "A/X"])
def test_valid(name):
    assert validate_repo(name) == name


@pytest.mark.parametrize(
    "name", ["x/..", "x/.", "x/%2e", "x/y/z", "x/\\y", "/repositories/1", "x /y"]
)
def test_invalid(name):
    with pytest.raises(ValueError):
        validate_repo(name)


def test_deny(monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    with pytest.raises(RepoDenied):
        validate_repo("AcMe-CoRp/secret-repo")
    assert is_denied("a/b", ["A"])
    assert is_denied("a/b", ["A/B"])
    assert not is_denied("a/b", ["a/c"])


@pytest.mark.parametrize(
    "path",
    [
        "/repos/ACME-CORP/secret-repo",
        "/orgs/acme-corp",
        "/users/acme-corp",
        "/repositories/1",
        "/repos/x/%252e",
        "/repos/example/x/../../../repos/acme-corp/secret-repo",
    ],
)
def test_denied_before_transport(path, monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("request sent"))
    with pytest.raises(github.GitHubDenied):
        github.get(path)


def test_denied_redirect(monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    response = httpx.Response(
        200, json={}, request=httpx.Request("GET", "https://api.github.com/repos/acme-corp/secret-repo")
    )
    monkeypatch.setattr(httpx, "get", lambda *a, **k: response)
    with pytest.raises(github.GitHubDenied):
        github.get("/repos/example/x")


@pytest.mark.parametrize(
    "path", ["/repos/x/repos/acme-corp/y", "/users/x/orgs/ACME-CORP", "/repos/x/%2e%2e"]
)
def test_nested_or_encoded_owner_paths(path, monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("request sent"))
    with pytest.raises(github.GitHubDenied):
        github.get(path)


def test_publish_rejects_denied_before_transport(monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    from argus import publish

    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "acme-corp/secret-repo")
    # The allowlist now screens denied owners itself; bypass it to keep exercising the transport guard.
    monkeypatch.setattr(publish, "repo_allowlist", publish.env_repo_allowlist)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("request sent"))
    before = github.refusal_count()
    outcome = publish.publish_review("acme-corp/secret-repo", 1, "a" * 40, 1, {}, [])
    assert outcome.status == "error"
    assert "GitHubDenied" in outcome.reason
    assert github.refusal_count() == before + 1


def test_denied_publish_redirect_does_not_return_body(monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    from argus import publish
    from types import SimpleNamespace

    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "example/a")
    monkeypatch.setenv("ARGUS_PUBLISH_TOKEN", "fake")
    monkeypatch.setattr(publish, "render_review", lambda *a, **k: {})
    response = httpx.Response(
        200, json={}, request=httpx.Request("GET", "https://api.github.com/repos/acme-corp/secret-repo")
    )
    fake = SimpleNamespace(
        get=lambda *a, **k: response, post=lambda *a, **k: pytest.fail("post sent")
    )
    outcome = publish.publish_review("example/a", 1, "a" * 40, 1, {}, [], http=fake)
    assert outcome.status == "error"
    assert "GitHubDenied" in outcome.reason


def test_safe_content_path_encoding_is_allowed(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    url = "https://api.github.com/repos/example/a/contents/a%20file.py"
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: httpx.Response(200, json={}, request=httpx.Request("GET", url)),
    )
    assert github.get("/repos/example/a/contents/a file.py").status_code == 200
