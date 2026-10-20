"""Query policy and quota isolation with local transport responses."""

import time
from email.utils import formatdate

import httpx
import pytest

from argus import github


@pytest.fixture(autouse=True)
def state(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "read-secret")
    for name, value in (("_quota", {}), ("_pause_until", 0), ("_graphql_pause_until", 0)):
        monkeypatch.setattr(github, name, value, raising=False)


@pytest.mark.parametrize("query,variables", [
    ('query { repository(owner:"prefix-ACME-CORP-suffix", name:"x") { id } }', {}),
    ('query { viewer { login } }', {"nested": [{"value": "prefix-acme-corp-suffix"}]}),
    ('query($owner:String!) { viewer { login } }', {"owner": "AcMe-CoRp"}),
])
def test_denied_owner_refused_before_transport(monkeypatch, caplog, query, variables):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": {}})
    before = github.refusal_count()
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(httpx, "post", http.post)
        with pytest.raises(github.GitHubDenied):
            github.graphql_read(query, variables)
    assert requests == []
    assert github.refusal_count() == before + 1
    assert "github: refused request to denied owner acme-corp" in caplog.text


@pytest.mark.parametrize("query,variables,denied", [
    ('query { repository(owner:"acme", name:"repo") { id } }', {}, True),
    ('query { repository(name:"REPO", owner:"ACME") { id } }', {}, True),
    ('query($o:String!, $n:String!) { repository(owner:$o, name:$n) { id } }',
     {"o": "AcMe", "n": "RePo"}, True),
    ('query { repository(owner:"acme", name:"public") { id } }', {}, False),
    ('query($o:String!, $n:String!) { repository(owner:$o, name:$n) { id } }',
     {"o": "acme", "n": "public"}, False),
    ('query repository($o:String!, $n:String!) { repository(owner:$o, name:$n) { id } }',
     {"o": "acme", "n": "public"}, False),
    ('query { a: repository(owner:"acme", name:"public") { id } '
     'b: repository(owner:"other", name:"repo") { id } }', {}, False),
    ('query { repository(owner:"acme", name:$missing) { id } }', {}, True),
    ('query { repository(owner:$missing, name:"public") { id } }', {}, True),
    ('query { repository(owner:"other", name:$n) { id } }', {"n": None}, True),
    ('query { repository(owner:"other") { id } }', {}, True),
    ('query { organization(login:$missing) { id } }', {}, True),
    ('query { user(login:$missing) { id } }', {}, True),
    ('query { user(login:"acme") { id } }', {}, False),
    ('query { organization(login:"acme") { id } }', {}, False),
    ('query { repository(owner:"other", name:"public") { id } '
     '# repository(owner:"acme", name:"repo")\n}', {}, False),
])
def test_repository_deny_matches_resolved_identity(monkeypatch, query, variables, denied):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "AcMe/RePo")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": {"ok": True}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(httpx, "post", http.post)
        if denied:
            with pytest.raises(github.GitHubDenied):
                github.graphql_read(query, variables)
            assert requests == []
        else:
            assert github.graphql_read(query, variables) == {"ok": True}
            assert len(requests) == 1


@pytest.mark.parametrize("query,variables", [
    ('query { repository(owner:"ACME", name:"public") { id } }', {}),
    ('query { repository(owner:$o, name:"public") { id } }', {"o": "Acme"}),
    ('query { organization(login:"Acme") { id } }', {}),
    ('query { user(login:$who) { id } }', {"who": "Acme"}),
    ('query { viewer { login } }', {"login": "ACME"}),
])
def test_owner_deny_refuses_all_repositories_and_logins(monkeypatch, query, variables):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must refuse before transport"))
    with pytest.raises(github.GitHubDenied):
        github.graphql_read(query, variables)


@pytest.mark.parametrize("status,headers,body", [
    (200, {"x-ratelimit-remaining": "0"}, {"data": {}}),
    (403, {"retry-after": "120"}, {"message": "rate limit"}),
    (429, {"retry-after": "date"}, {"message": "rate limit"}),
    (200, {}, {"errors": [{"type": "RATE_LIMITED", "message": "private body"}]}),
    (200, {"no-reset": "1"}, {"errors": [{"type": "RATE_LIMITED"}]}),
])
def test_query_pause_does_not_pause_rest(monkeypatch, caplog, status, headers, body):
    caplog.set_level("INFO", logger="argus.github")
    now = 1800000000
    monkeypatch.setattr(github.time, "time", lambda: now)
    headers = dict(headers)
    if not headers.pop("no-reset", None):
        headers["x-ratelimit-reset"] = str(now + 120)
    if headers.get("retry-after") == "date":
        headers["retry-after"] = formatdate(now + 120, usegmt=True)
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers=headers, json=body)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(httpx, "post", http.post)
        with pytest.raises(github.GraphQLRateLimited):
            github.graphql_read("query { viewer { login } }", {"owner": "allowed"})
        assert github.pause_until() == 0
        with pytest.raises(github.GraphQLRateLimited) as error:
            github.graphql_read("query { viewer { login } }", {})
        assert error.value.reset_at == now + (120 if "x-ratelimit-reset" in headers else 60)
        assert len(requests) == 1
        now += 121
        with pytest.raises(github.GraphQLRateLimited):
            github.graphql_read("query { viewer { login } }", {})
        assert len(requests) == 2
    assert "github quota:" not in caplog.text
    if "x-ratelimit-remaining" in headers:
        assert github.quota_snapshot()["graphql"]["remaining"] == 0


def test_rest_pause_blocks_query_without_request(monkeypatch):
    monkeypatch.setattr(github, "_pause_until", time.time() + 60)
    requests = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: requests.append(r))) as http:
        monkeypatch.setattr(httpx, "post", http.post)
        with pytest.raises(github.GitHubRateLimited):
            github.graphql_read("query { viewer { login } }", {})
    assert requests == []


def test_other_query_errors_hide_body(monkeypatch):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
        200, json={"errors": [{"type": "OTHER", "message": "private body"}]}
    ))) as http:
        monkeypatch.setattr(httpx, "post", http.post)
        with pytest.raises(RuntimeError, match="^Read query returned errors$"):
            github.graphql_read("query { viewer { login } }", {})
    assert github.pause_until() == 0
    assert github._graphql_pause_until == 0


@pytest.mark.parametrize("query,variables,denied", [
    ('query { organization(login:"acme") { repository(name:"repo") { id } } }', {}, True),
    ('query { user(login:"acme") { repository(name:"repo") { id } } }', {}, True),
    ('query($l:String!) { organization(login:$l) { repository(name:"REPO") { id } } }',
     {"l": "ACME"}, True),
    ('query { organization(login:"acme") { repository(name:"public") { id } } }', {}, False),
    ('query { user(login:"acme") { repository(name:"public") { id } } }', {}, False),
    ('query { organization(login:"acme") { a: repository(name:"public") { id } '
     'b: repository(name:"repo") { id } } }', {}, True),
    # The parent scope ends with its braces, so a later bare repository has no owner.
    ('query { organization(login:"acme") { id } repository(name:"public") { id } }', {}, True),
    ('query { organization(login:"acme") { id } '
     'organization(login:"other") { repository(name:"repo") { id } } }', {}, False),
    ('query { organization(login:$missing) { repository(name:"public") { id } } }', {}, True),
    ('query { organization(login:"acme") { repository(name:$missing) { id } } }', {}, True),
    ('query { organization(login:"acme") { team { repository(name:"repo") { id } } } }', {}, True),
])
def test_nested_repository_resolves_against_parent_login(monkeypatch, query, variables, denied):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme/repo")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": {"ok": True}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(httpx, "post", http.post)
        if denied:
            with pytest.raises(github.GitHubDenied):
                github.graphql_read(query, variables)
            assert requests == []
        else:
            assert github.graphql_read(query, variables) == {"ok": True}


@pytest.mark.parametrize("query", [
    'query { organization(login:"acme") { repository(name:"repo") { id } } }',
    'query { user(login:"acme") { repository(name:"repo") { id } } }',
])
def test_nested_repository_allowed_with_empty_deny_list(monkeypatch, query):
    monkeypatch.delenv("ARGUS_DENY_OWNERS", raising=False)
    with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": {"ok": True}}))) as http:
        monkeypatch.setattr(httpx, "post", http.post)
        assert github.graphql_read(query, {}) == {"ok": True}
