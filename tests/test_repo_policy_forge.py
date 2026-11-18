"""GitLab access requires a live path, a configured host and an explicit allow."""

import random
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from argus import repo_policy as policy
from argus.api import db, main
from argus.forge import keys
from argus.forge.types import RepoRecord, RepoRef


@pytest.fixture(autouse=True)
def isolated_policy(monkeypatch, request, tmp_path):
    for name in ("ARGUS_GITLAB_URL", "ARGUS_GITLAB_ALLOW", "ARGUS_DENY_OWNERS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARGUS_DB_PATH", str(tmp_path / "argus.db"))
    monkeypatch.setenv("ARGUS_GITLAB_URL", "https://h")
    calls = []

    def no_network(*args, **kwargs):
        calls.append(True)
        raise AssertionError("unexpected network call")

    for name in ("get", "post", "request"):
        monkeypatch.setattr(httpx, name, no_network)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    # The in-process API client needs a local event-loop socket pair.
    if "api" not in request.fixturenames:
        monkeypatch.setattr(socket, "socket", no_network)
        monkeypatch.setattr(socket, "create_connection", no_network)
        monkeypatch.setattr(httpx.Client, "send", no_network)
    yield
    assert not calls


def _ref(project_id=12, host="h"):
    return RepoRef("gitlab", host, None, project_id)


def _record(path="acme/x", archived=False):
    return RepoRecord(path, archived, "main")


@pytest.mark.parametrize("path,entry,project_id,allowed", [
    ("acme/x", "gitlab:h/acme", 12, True),
    ("acme/sub/x", "gitlab:h/acme", 12, True),
    ("acmecorp/x", "gitlab:h/acme", 12, False),
    ("acme-x/y", "gitlab:h/acme", 12, False),
    ("AcMe/SUB/x", "gitlab:H/ACME/sub", 12, True),
    ("acme/submarine/x", "gitlab:h/acme/sub", 12, False),
    ("acme/x", "gitlab:h#12", 12, True),
    ("acme/x", "gitlab:h#12", 13, False),
    ("acme/x", "gitlab:other/acme", 12, False),
    ("acme/x", "acme", 12, False),
    ("acme/x", "acme/x", 12, False),
    ("acme/x", "gitlab:h/", 12, False),
    ("acme/x", "bitbucket:h/acme", 12, False),
])
@pytest.mark.parametrize("parsed", [False, True])
def test_live_path_and_id_allow_rules(path, entry, project_id, allowed, parsed):
    if parsed:
        try:
            entry = keys.parse_entry(entry)
        except ValueError:
            pass
    result = policy.gitlab_decision(_ref(project_id), _record(path), [], [entry])
    assert result == policy.GitLabDecision(allowed, "allowed" if allowed else "not allowlisted")


@pytest.mark.parametrize("ref,record,url,reason", [
    (None, _record(), "https://h", "not a gitlab ref"),
    (RepoRef("github", "github.com", "acme/x", None), _record(), "https://h", "not a gitlab ref"),
    (_ref(True), _record(), "https://h", "not a gitlab ref"),
    (_ref(0), _record(), "https://h", "not a gitlab ref"),
    (_ref("12"), _record(), "https://h", "not a gitlab ref"),
    (_ref(), _record(), "", "gitlab not configured"),
    (_ref(), _record(), "not-a-url", "gitlab not configured"),
    (_ref(host="other"), _record(), "https://h", "host not configured"),
    (_ref(), None, "https://h", "project not resolved"),
    (_ref(), _record(None), "https://h", "project path unknown"),
    (_ref(), _record(""), "https://h", "project path unknown"),
    (_ref(), _record("acme//x"), "https://h", "project path unknown"),
    (_ref(), _record("/acme/x"), "https://h", "project path unknown"),
    (_ref(), _record("acme/x/"), "https://h", "project path unknown"),
    (_ref(), _record(42), "https://h", "project path unknown"),
    (_ref(), _record(archived=True), "https://h", "project archived"),
    (_ref(), _record(archived=None), "https://h", "project archived"),
    (_ref(), _record(archived="unknown"), "https://h", "project archived"),
])
def test_decision_preconditions(ref, record, url, reason, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_URL", url)
    assert policy.gitlab_decision(ref, record, [], ["gitlab:h#12"]) == policy.GitLabDecision(False, reason)


@pytest.mark.parametrize("path", ["Blockedco/app", "blockedco/sub/app", "BLOCKEDCO/x"])
@pytest.mark.parametrize("allow", ["gitlab:h#5", "gitlab:h/Blockedco"])
def test_hard_deny_cannot_be_bypassed(path, allow, monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "blockedco")
    assert policy.gitlab_decision(_ref(5), _record(path), [], [allow]) == policy.GitLabDecision(False, "hard deny owner")
    assert policy.is_denied("blockedco/x")
    assert policy.is_denied("Blockedco/x")
    with pytest.raises(policy.RepoDenied):
        policy.validate_repo("blockedco/x")


def test_transfer_is_checked_against_current_path(monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "blockedco")
    assert policy.gitlab_decision(_ref(5), _record("other/x"), [], ["gitlab:h#5"]).allowed
    assert policy.gitlab_decision(_ref(5), _record("blockedco/x"), [], ["gitlab:h#5"]).reason == "hard deny owner"


@pytest.mark.parametrize("deny,path,project_id,denied", [
    ("acme", "acme/x", 12, False), ("acme/x", "acme/x", 12, False),
    ("gitlab:h/acme", "acme/x", 12, True), ("gitlab:h/acme", "acmecorp/x", 12, False),
    ("gitlab:h#7", "acme/x", 7, True), ("gitlab:h#7", "acme/x", 12, False),
    ("gitlab:other/acme", "acme/x", 12, False),
])
@pytest.mark.parametrize("parsed", [False, True])
def test_qualified_denies(deny, path, project_id, denied, parsed):
    entry = keys.parse_entry(deny) if parsed else deny
    result = policy.gitlab_decision(_ref(project_id), _record(path), [entry], [f"gitlab:h#{project_id}"])
    assert result == policy.GitLabDecision(not denied, "deny entry" if denied else "allowed")
    assert not policy.is_denied("acme/x", ["gitlab:h/acme"])


@pytest.mark.parametrize("deny,reason", [
    ("gitlab:h/acme", "deny entry"), ("gitlab:h#12", "deny entry"),
    ("gitlab:h/", "hard deny entry invalid"), ("GITLAB:h#012", "hard deny entry invalid"),
    ("ACME/X", "hard deny owner"),
])
def test_hard_deny_qualified_and_full_path(deny, reason, monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", deny)
    assert policy.gitlab_decision(_ref(), _record(), [], ["gitlab:h#12"]).reason == reason


@pytest.mark.parametrize("deny,reason", [
    ("gitlab:h/", "deny entry invalid"), ("GitLab:h#012", "deny entry invalid"),
    ("bitbucket:h/x", "allowed"), ("not a valid owner", "allowed"),
])
def test_invalid_denies_fail_closed_only_for_gitlab(deny, reason):
    assert policy.gitlab_decision(_ref(), _record(), [deny], ["gitlab:h#12"]).reason == reason


def test_deny_wins_over_allow_and_empty_allow_denies():
    assert policy.gitlab_decision(_ref(), _record(), ["gitlab:h/acme"], ["gitlab:h#12"]).reason == "deny entry"
    assert policy.gitlab_decision(_ref(), _record(), [], []).reason == "not allowlisted"


def test_unexpected_inputs_never_allow_or_raise():
    for record, denies, allows in [
        (object(), [], ["gitlab:h#12"]), (_record(), None, ["gitlab:h#12"]),
        (_record(), [], None), (_record(), [object()], ["gitlab:h#12"]),
        (_record(), [], [keys.PolicyEntry("path", "h")]),
        (_record(), [], [keys.PolicyEntry("id", "h", project_id=True)]),
    ]:
        assert not policy.gitlab_decision(_ref(), record, denies, allows).allowed


@pytest.mark.parametrize("deny", ["gitlab:h/acme", "gitlab:h#12", "", b"gitlab:h/acme"])
def test_scalar_deny_container_cannot_silently_erase_a_deny(deny):
    assert not policy.gitlab_decision(_ref(), _record(), deny, ["gitlab:h#12"]).allowed


@pytest.mark.parametrize("allow", ["gitlab:h/acme", "gitlab:h#12", "", b"gitlab:h/acme"])
def test_scalar_allow_container_cannot_grant_access(allow):
    assert not policy.gitlab_decision(_ref(), _record(), [], allow).allowed


def test_preparsed_and_generator_entries():
    result = policy.gitlab_decision(
        _ref(), SimpleNamespace(path="acme/x", archived=False),
        (keys.parse_entry(x) for x in ["other"]),
        (keys.parse_entry(x) for x in ["gitlab:h/acme"]),
    )
    assert result == policy.GitLabDecision(True, "allowed")


def test_is_denied_stays_pure_and_github_identical(monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "blocked,other/private")
    extra = ["db-denied", "acme/private", "gitlab:h/acme"]

    def previous(name, extra_deny=()):
        name = name.casefold()
        owner = name.split("/", 1)[0]
        denied = policy.hard_deny_owners() | {entry.casefold() for entry in extra_deny}
        return owner in denied or name in denied

    for key in ("gitlab:h#1", "bitbucket:x#1", "a:b/c", "example/app:tag", ":"):
        assert policy.is_denied(key)
    rng = random.Random(1395)
    for _ in range(500):
        name = rng.choice(["blocked", "other", "acme", "db-denied", "safe"])
        name += "/" + rng.choice(["private", "app", "repo", "sub/app", ""])
        if rng.choice([True, False]):
            name = name.upper()
        assert policy.is_denied(name, extra) == previous(name, extra)


@pytest.mark.parametrize("url,key", [
    ("https://h", "gitlab:h#12"), ("https://H", "gitlab:h#12"),
    ("https://H:8443/gitlab", "gitlab:h:8443#12"),
    ("https://[2001:DB8::1]:443", "gitlab:[2001:db8::1]#12"),
])
def test_validate_explicit_gitlab_opt_in(url, key, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_URL", url)
    with pytest.raises(ValueError, match="^repo must be owner/name with a valid owner and repository name$"):
        policy.validate_repo(key)
    assert policy.validate_repo(key, allow_gitlab=True) == key
    assert policy.validate_repo("Acme/App", [], allow_gitlab=True) == "Acme/App"


@pytest.mark.parametrize("url,exception", [("", ValueError), ("https://other", policy.RepoDenied)])
def test_validate_gitlab_configuration(url, exception, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_URL", url)
    with pytest.raises(exception):
        policy.validate_repo("gitlab:h#12", allow_gitlab=True)


@pytest.mark.parametrize("key", ["bitbucket:x#1", "GitLab:h#1", "gitlab:H#1", "gitlab:h#01"])
@pytest.mark.parametrize("allow_gitlab", [False, True])
def test_validate_rejects_unknown_and_noncanonical_keys(key, allow_gitlab):
    with pytest.raises(ValueError):
        policy.validate_repo(key, allow_gitlab=allow_gitlab)


@pytest.mark.parametrize("allow,hard,reason", [
    ("acme", "", "entries must be gitlab:HOST/group or gitlab:HOST#ID"),
    ("gitlab:h/", "", "ARGUS_GITLAB_ALLOW:"),
    ("gitlab:h/Blockedco", "blockedco", "an entry overlaps a hard-deny owner"),
    ("gitlab:h/acme/sub", "acme/sub", "an entry overlaps a hard-deny owner"),
    ("gitlab:h#12", "gitlab:h/", "ARGUS_DENY_OWNERS: invalid gitlab entry"),
    ("", "GitLab:h#012", "ARGUS_DENY_OWNERS: invalid gitlab entry"),
])
def test_load_refusal_is_safe(allow, hard, reason, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", allow)
    monkeypatch.setenv("ARGUS_DENY_OWNERS", hard)
    with pytest.raises(policy.GitLabPolicyError, match=reason):
        policy.gitlab_allow_entries()
    result = policy.gitlab_policy()
    assert not result.enabled and result.allow == ()
    assert reason in result.refusal
    assert "blockedco" not in result.refusal.casefold()
    if allow:
        assert allow not in result.refusal


def test_load_parses_allow_entries(monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", "gitlab:H:443/Acme/Sub,gitlab:h#12")
    entries = (keys.parse_entry("gitlab:h/acme/sub"), keys.parse_entry("gitlab:h#12"))
    assert policy.gitlab_allow_entries() == entries
    assert policy.gitlab_policy() == policy.GitLabPolicy(entries, None)
    assert policy.gitlab_policy().enabled


def test_config_deny_validator_preserves_raw_values(monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme")
    values = ["gitlab:H:443/Acme", "gitlab:h#12", "acme", "acme/app"]
    update = main.ConfigUpdate(deny=values, model=main.DEFAULT_MODEL, bugCap=4, minConfidence=0.5)
    assert update.deny == values


@pytest.mark.parametrize("value", ["gitlab:h/", "bitbucket:x/y", "acme/..", "a/b/c"])
def test_config_deny_validator_rejects_bad_entries(value):
    with pytest.raises(ValidationError):
        main.ConfigUpdate(deny=[value], model=main.DEFAULT_MODEL, bugCap=4, minConfidence=0.5)


@pytest.fixture
def api(tmp_path, monkeypatch):
    from argus import publish_access

    path = str(tmp_path / "argus.db")
    for key in ("ARGUS_PUBLISH", "ARGUS_PUBLISH_REPOS", "ARGUS_PUBLISH_TOKEN",
                "ARGUS_PUBLISH_BOT_LOGIN", "ARGUS_WATCH_OWNERS", "GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    db.init_db(path)
    publish_access.clear_cache()
    for name in ("ensure_access", "check_access"):
        monkeypatch.setattr(publish_access, name, Mock(side_effect=AssertionError("unexpected access check")))
    with TestClient(main.app) as client:
        yield client, path, publish_access
    publish_access.clear_cache()


def test_publish_repos_rejects_gitlab_before_access_or_save(api):
    client, path, access = api
    response = client.put("/config/publish-repos", json={"repos": ["gitlab:h#1"]})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid repository name: gitlab:h#1"}
    access.ensure_access.assert_not_called()
    access.check_access.assert_not_called()
    assert db.get_config(path) is None
    assert db.get_publish_repos(path) is None
