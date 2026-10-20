import base64

import httpx
import pytest

from argus import github
from argus.diff import fetch_pr_files
from argus.review import fetch_house_rules


@pytest.mark.parametrize("file", [
    "app/repositories/user_repo.py", "docs/a:b.md", "a%25b.txt",
    "app/repos/acme-corp/secret-repo.py", "contents/repos/acme-corp/secret-repo.py",
])
def test_guard_allows_contents_file_paths(file, monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    url = github.guard_url(f"https://api.github.com/repos/example/x/contents/{file}")
    assert url.path == f"/repos/example/x/contents/{file.replace('%25', '%')}"


@pytest.mark.parametrize("path", [
    "/repos/example/x/contents/../../../../repos/acme-corp/secret-repo/pulls",
    "/repos/example/x/contents/%2e%2e/file.py",
    "/repos/acme-corp/secret-repo/contents/x",
    "/repos/example/x/contents/app//file.py",
    "/repos/example/x/contents/app\\file.py",
    "/repos/example/x/contents/app%2ffile.py",
    "/repos/example/x/contents/app%5cfile.py",
    "/repos/example/x/contents-extra/repos/acme-corp/secret-repo/pulls",
])
def test_guard_refuses_unsafe_contents_paths(path, monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    with pytest.raises(github.GitHubDenied):
        github.guard_url(f"https://api.github.com{path}")


def test_guard_checks_normalized_contents_traversal(monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    url = httpx.URL(
        "https://api.github.com/repos/example/x/contents/../../../../repos/acme-corp/secret-repo/pulls"
    )
    assert url.path == "/repos/acme-corp/secret-repo/pulls"
    with pytest.raises(github.GitHubDenied):
        github.guard_url(url)


def _resp(status: int, json=None) -> httpx.Response:
    return httpx.Response(
        status, json=json, request=httpx.Request("GET", "https://api.github.com/x")
    )


def _stub(monkeypatch, response: httpx.Response) -> None:
    monkeypatch.setattr(github.httpx, "get", lambda *a, **k: response)


def test_get_without_token_raises(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(github.GitHubAuthError):
        github.get("/repos/x/y/pulls/1/files")


def _redirecting_httpx_get(moved_from: str, moved_to: str, payload):
    """A drop-in for httpx.get that drives a real client over a MockTransport.

    The transport answers `moved_from` with a 301 to `moved_to` and serves the
    payload there, so httpx's own redirect machinery decides the outcome: the
    call resolves only if production passes follow_redirects=True. Returns
    (replacement_get, calls) where calls records the followed-through paths.
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == moved_from:
            return httpx.Response(301, headers={"Location": moved_to})
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)

    def replacement_get(url, **kwargs):
        # Honour exactly the redirect policy production passes through; if it
        # omits follow_redirects the client defaults to False and the 301 raises.
        with httpx.Client(
            transport=transport,
            follow_redirects=kwargs.get("follow_redirects", False),
        ) as client:
            return client.get(url, params=kwargs.get("params"), headers=kwargs.get("headers"))

    return replacement_get, calls


def test_get_follows_301_for_renamed_repo(monkeypatch):
    # GitHub returns 301 Moved Permanently for a renamed/moved repo. The PRs
    # endpoint must follow it and return the body from the new location, or the
    # watcher's per-tick /pulls call raises and that repo is never reviewed.
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    payload = [{"number": 7, "head": {"sha": "deadbeef"}}]
    replacement, calls = _redirecting_httpx_get(
        "/repos/example/service/pulls",
        "https://api.github.com/repos/example/service-renamed/pulls",
        payload,
    )
    monkeypatch.setattr(github.httpx, "get", replacement)

    response = github.get("/repos/example/service/pulls", {"state": "open"})

    assert response.status_code == 200
    assert response.json() == payload
    # Proof the redirect was actually traversed to the renamed location.
    assert "/repos/example/service-renamed/pulls" in calls


def test_fetch_pr_files_parses_payload(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    payload = [
        {"filename": "a.py", "status": "modified", "patch": "@@ -1 +1 @@\n+x"},
        {"filename": "b.py", "status": "added"},  # missing patch key -> None
    ]
    _stub(monkeypatch, _resp(200, payload))
    files = fetch_pr_files("example/argus", 12)
    assert [f.filename for f in files] == ["a.py", "b.py"]
    assert files[1].patch is None


def test_fetch_pr_files_rejected_token_raises(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "bad")
    _stub(monkeypatch, _resp(401, {"message": "Bad credentials"}))
    with pytest.raises(github.GitHubAuthError):
        fetch_pr_files("example/argus", 12)


def test_fetch_pr_files_paginates(monkeypatch):
    # A full 100-file page means "keep going"; a short page ends it. A large PR
    # must be fetched whole, not truncated at the first page.
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    page1 = [{"filename": f"f{i}.py", "status": "modified", "patch": "+x"} for i in range(100)]
    page2 = [{"filename": "last.py", "status": "added", "patch": "+y"}]
    pages = iter([_resp(200, page1), _resp(200, page2)])
    monkeypatch.setattr(github.httpx, "get", lambda *a, **k: next(pages))
    files = fetch_pr_files("example/argus", 12)
    assert len(files) == 101
    assert files[-1].filename == "last.py"


def test_fetch_house_rules_decodes_content(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    encoded = base64.b64encode(b"# House rules\nbe terse").decode()
    _stub(monkeypatch, _resp(200, {"content": encoded}))
    assert "House rules" in fetch_house_rules("example/argus")


def test_fetch_house_rules_absent_returns_none(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    _stub(monkeypatch, _resp(404, {"message": "Not Found"}))
    assert fetch_house_rules("example/argus") is None


def test_fetch_house_rules_transient_error_returns_none(monkeypatch):
    # House rules are best-effort: a 5xx blip reviews without them, never crashes.
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    _stub(monkeypatch, _resp(500, {"message": "Server Error"}))
    assert fetch_house_rules("example/argus") is None


def test_fetch_house_rules_directory_payload_returns_none(monkeypatch):
    # GitHub returns a JSON list when the path is a directory, not an object.
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    _stub(monkeypatch, _resp(200, [{"name": "CLAUDE.md", "type": "file"}]))
    assert fetch_house_rules("example/argus") is None


def test_fetch_house_rules_auth_error_raises(monkeypatch):
    # An under-scoped or expired token must fail loudly, not be read as "no
    # rules" -- otherwise every review silently loses its house-rule grounding.
    monkeypatch.setenv("GITHUB_TOKEN", "bad")
    _stub(monkeypatch, _resp(403, {"message": "Forbidden"}))
    with pytest.raises(github.GitHubAuthError):
        fetch_house_rules("example/argus")


def test_list_owned_repos_paginates_and_skips_archived(monkeypatch):
    # A full 100-repo page means "keep going"; a short page ends it. Archived repos
    # are dropped so the watcher never spends a review on a frozen repo.
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    page1 = [{"full_name": f"example/r{i}", "archived": False} for i in range(100)]
    page2 = [
        {"full_name": "example/active", "archived": False},
        {"full_name": "example/frozen", "archived": True},
    ]
    first = _resp(200, page1)
    first.headers["Link"] = '<https://api.github.com/user/repos?page=2>; rel="next"'
    pages = iter([first, _resp(200, page2)])
    monkeypatch.setattr(github.httpx, "get", lambda *a, **k: next(pages))

    repos = github.list_owned_repos()

    assert len(repos) == 101                 # 100 + active; frozen dropped
    assert "example/active" in repos
    assert "example/frozen" not in repos


def test_list_owned_repos_requests_owner_affiliation(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    captured = {}

    def capturing_get(*a, **k):
        captured.update(k.get("params") or {})
        return _resp(200, [])

    monkeypatch.setattr(github.httpx, "get", capturing_get)
    github.list_owned_repos()

    assert captured["affiliation"] == "owner"
    assert captured["per_page"] == 100
