"""Repository identities and policy entries must have unambiguous boundaries."""

import random
import string

import pytest

from argus.forge import keys
from argus.forge.types import RepoRef


def _parsed_forges(key):
    try:
        return {keys.parse_repo_key(key).forge}
    except ValueError:
        return set()


def test_seeded_repository_key_round_trips_and_forge_separation():
    rng = random.Random(1395)
    alnum = string.ascii_letters + string.digits
    github_keys, gitlab_keys = set(), set()
    for _ in range(10000):
        owner = rng.choice(alnum) + "".join(rng.choices(alnum + "-", k=rng.randrange(39)))
        name = rng.choice(alnum) + "".join(rng.choices(alnum + "._-", k=rng.randrange(100)))
        key = f"{owner}/{name}"
        github_keys.add(key)
        assert _parsed_forges(key) == {"github"}
        assert keys.parse_repo_key(key) == RepoRef("github", "github.com", key, None)
        assert keys.canonicalize_key(key) == key
        assert keys.is_github_key(key) and ":" not in key

    for _ in range(10000):
        if rng.randrange(20) == 0:
            host = f"[2001:db8::{rng.randrange(1, 65536):x}]"
        else:
            host = ".".join(
                "h" + "".join(rng.choices(string.ascii_lowercase + string.digits + "-", k=rng.randrange(12))) + "z"
                for _ in range(rng.randrange(1, 5))
            )
        port = rng.choice([None, 8443, 65535, 1])
        authority = host if port is None else f"{host}:{port}"
        project_id = rng.randrange(1, 10**9 + 1)
        key = f"gitlab:{authority}#{project_id}"
        gitlab_keys.add(key)
        assert _parsed_forges(key) == {"gitlab"}
        assert keys.canonicalize_key(key) == key
        assert keys.parse_repo_key(key) == RepoRef("gitlab", authority, None, project_id)
        assert keys.gitlab_key(authority, project_id) == key
        assert not keys.is_github_key(key)
        variants = [
            (f"gitlab:{authority.upper()}#{project_id}", key),
            # Replacing a non-default port changes the identity to the default host.
            (f"gitlab:{host}:443#{project_id}", f"gitlab:{host}#{project_id}"),
        ]
        for variant, canonical in variants:
            with pytest.raises(ValueError):
                keys.parse_repo_key(variant)
            assert keys.canonicalize_key(variant) == canonical
        for variant in (
            f"gitlab:{authority}/#{project_id}", f"gitlab:{authority}#{project_id}/",
            f"gitlab:{authority}#0{project_id}", f"gitlab:{authority}/group/project",
            f"gitlab:{authority}#0",
        ):
            with pytest.raises(ValueError):
                keys.parse_repo_key(variant)
            with pytest.raises(ValueError):
                keys.canonicalize_key(variant)
    assert github_keys.isdisjoint(gitlab_keys)


@pytest.mark.parametrize("key", ["bitbucket:x#1", "GitLab:h#1", "a:b/c"])
def test_unknown_forge_is_rejected(key):
    with pytest.raises(ValueError, match="^unknown forge$"):
        keys.parse_repo_key(key)


@pytest.mark.parametrize("text,gitlab,github", [
    ("gitlab:h#1", True, False), ("gitlab:", True, False),
    ("GitLab:h#1", False, False), ("bitbucket:x#1", False, False),
    ("a:b/c", False, False), ("acme/app", False, True), ("", False, True),
])
def test_forge_predicates(text, gitlab, github):
    assert keys.is_gitlab_key(text) is gitlab
    assert keys.is_github_key(text) is github


@pytest.mark.parametrize("text,expected", [
    ("gitlab:h/acme", ("path", "h", ("acme",), None, None)),
    ("gitlab:H:443/Acme/Sub", ("path", "h", ("acme", "sub"), None, None)),
    ("GITLAB:[2001:DB8::1]:8443/_Acme/sub.repo", ("path", "[2001:db8::1]:8443", ("_acme", "sub.repo"), None, None)),
    ("gitlab:h#12", ("id", "h", (), 12, None)),
    ("GitLab:H:443#12", ("id", "h", (), 12, None)),
    ("acme", ("owner", None, (), None, "acme")),
    ("Acme/App", ("owner", None, (), None, "acme/app")),
])
def test_policy_entries(text, expected):
    assert keys.parse_entry(text) == keys.PolicyEntry(*expected)


@pytest.mark.parametrize("text", [
    "gitlab:h/", "gitlab:h/acme/", "gitlab:h/a//b", "gitlab:h/.acme", "gitlab:h/-acme",
    "gitlab:h/" + "/".join(["a"] * 21), "gitlab:h#012", "gitlab:h#",
    "gitlab:h/acme#12", "bitbucket:x/y", "", "a/b/c", "acme/.", "acme/..",
    " acme", "acme\n", "gitlab:h/a b", "gitlab:h/café",
])
def test_invalid_policy_entries(text):
    with pytest.raises(ValueError):
        keys.parse_entry(text)


def test_policy_depth_boundary():
    assert len(keys.parse_entry("gitlab:h/" + "/".join(["a"] * 20)).segments) == 20


@pytest.mark.parametrize("authority", [
    "", "h/", "h/path", "user@h", "h#h", "h.", ".h", "h..x", "-h", "h-",
    "h_x", "h:0", "h:0443", "h:65536", "h:999999", "h:+443", "h:٤٤٣",
    "h:", "a" * 64, ".".join(["a" * 63] * 4), "[invalid]", "2001:db8::1",
    "[2001:db8::1]suffix", "[fe80::1%eth0]", "h\\x", "h%20", "h?x",
    "h\x00", "h\x7f", "h\x85", "h\n", " h", "h ", "K.example",
])
def test_invalid_host_is_rejected_without_echo(authority):
    text = f"gitlab:{authority}#1"
    for parse in (keys.canonicalize_key, keys.parse_repo_key, keys.parse_entry):
        with pytest.raises(ValueError) as caught:
            parse(text)
        assert text not in str(caught.value)


@pytest.mark.parametrize("suffix", ["", "0", "01", "-1", "+1", "1.0", "١", "1#2", "1\n", "1\x00", "1" * 19, "1" * 4301])
def test_invalid_id(suffix):
    with pytest.raises(ValueError):
        keys.canonicalize_key(f"gitlab:h#{suffix}")


@pytest.mark.parametrize("host,project_id", [
    ("H", 1), ("h:443", 1), ("h", True), ("h", False), ("h", 0),
    ("h", -1), ("h", "1"), ("h", 1.0), ("h", None), ("h/", 1),
    (None, 1), (False, 1), (42, 1), (b"h", 1),
])
def test_key_builder_requires_canonical_parts(host, project_id):
    with pytest.raises(ValueError):
        keys.gitlab_key(host, project_id)


@pytest.mark.parametrize("text", ["", "a", "a/b/c", "/x", "a/.", "a/..", "-a/x", "a/x\n", "é/x", "a" * 40 + "/x", "a/" + "x" * 101])
def test_github_grammar(text):
    assert keys.canonicalize_key(text) == text
    with pytest.raises(ValueError):
        keys.parse_repo_key(text)


def test_uppercase_prefix_is_only_accepted_at_input_boundary():
    assert keys.canonicalize_key("GiTlAb:H:443#1") == "gitlab:h#1"


def test_shared_github_patterns():
    from argus import repo_policy

    assert repo_policy._OWNER is keys.GITHUB_OWNER
    assert repo_policy._NAME is keys.GITHUB_NAME
