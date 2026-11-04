"""The GitLab URL boundary accepts only safe paths and typed route inputs."""

import os
import time

import httpx
import pytest


@pytest.fixture(autouse=True)
def configured_gitlab(monkeypatch):
    for name in list(os.environ):
        if name.startswith("ARGUS_GITLAB_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ARGUS_GITLAB_URL", "https://gitlab.test")


def test_build_checks_a_project_url():
    from argus.gitlab import guard

    url = guard.build("project", project_id=42)
    assert str(url) == "https://gitlab.test/api/v4/projects/42"
    assert guard.check(url) == url
    assert isinstance(url, httpx.URL)


SHA = "ab12" * 10
ROOT = "https://gitlab.test/api/v4"
SAMPLES = {
    "project": ({"project_id": 1}, {}, "/projects/1"),
    "project_by_path": ({"project_path": "grp/sub/proj"}, {}, "/projects/grp%2Fsub%2Fproj"),
    "mr_list": ({"project_id": 1}, {}, "/projects/1/merge_requests"),
    "mr": ({"project_id": 1, "iid": 2}, {}, "/projects/1/merge_requests/2"),
    "mr_versions": ({"project_id": 1, "iid": 2}, {}, "/projects/1/merge_requests/2/versions"),
    "mr_diffs": ({"project_id": 1, "iid": 2}, {}, "/projects/1/merge_requests/2/diffs"),
    "mr_raw_diffs": ({"project_id": 1, "iid": 2}, {}, "/projects/1/merge_requests/2/raw_diffs"),
    "mr_discussions": ({"project_id": 1, "iid": 2}, {}, "/projects/1/merge_requests/2/discussions"),
    "mr_discussion": ({"project_id": 1, "iid": 2, "discussion_id": SHA}, {}, f"/projects/1/merge_requests/2/discussions/{SHA}"),
    "mr_discussion_notes": ({"project_id": 1, "iid": 2, "discussion_id": SHA}, {}, f"/projects/1/merge_requests/2/discussions/{SHA}/notes"),
    "mr_notes": ({"project_id": 1, "iid": 2}, {}, "/projects/1/merge_requests/2/notes"),
    "file_raw": ({"project_id": 1, "file_path": "dir/a.py"}, {"ref": "main"}, "/projects/1/repository/files/dir%2Fa.py/raw"),
    "compare": ({"project_id": 1}, {"from": "main", "to": "feature/x"}, "/projects/1/repository/compare"),
    "merge_base": ({"project_id": 1}, {"refs[]": ["main", "feature/x"]}, "/projects/1/repository/merge_base"),
    "members_all": ({"project_id": 1, "user_id": 2}, {}, "/projects/1/members/all/2"),
    "user": ({}, {}, "/user"),
    "version": ({}, {}, "/version"),
    "group_projects": ({"group": "grp/sub"}, {}, "/groups/grp%2Fsub/projects"),
}
QUERY_VALUES = {
    "state": "opened", "updated_after": "2026-01-01T12:34:56+01:00",
    "updated_before": "2026-01-02T12:34:56.123456Z", "order_by": "created_at",
    "sort": "desc", "per_page": 100, "page": 1_000_000, "target_branch": "feature/x",
    "scope": "all", "unidiff": True, "ref": "main", "from": "main", "to": SHA,
    "straight": False, "refs[]": ["main", "feature/x"], "include_subgroups": True,
    "archived": False, "simple": True, "with_shared": False, "min_access_level": 30,
}


@pytest.mark.parametrize("value", [
    "1/2", "1%2F2", "1%252F2", "..", "https://evil.example/", "@evil.example",
    "evil.example", True, 0, -1, 10**18, 1.0,
])
def test_build_refuses_non_integer_or_out_of_range_ids(value):
    from argus.gitlab import guard

    for route, parts in [
        ("project", {"project_id": value}),
        ("mr", {"project_id": 1, "iid": value}),
        ("members_all", {"project_id": 1, "user_id": value}),
    ]:
        with pytest.raises(guard.GitLabGuardError):
            guard.build(route, **parts)


@pytest.mark.parametrize("value", [
    "a/../b", "a%2Fb", "a%252Fb", "a/b\\c", "a/b\x00", "https://evil.example/x",
    "evil.example@x", "a//b", "/a/b", "a/b/", "-a/b", "a/.", "a/..",
    "/".join(["a"] * 23), "_a/b", ".a/b", "a/" + "b" * 256,
    "/".join(["a" * 255] * 5), "a/b?c", "a/b#c", "", None,
])
def test_build_refuses_invalid_project_and_group_paths(value):
    from argus.gitlab import guard

    for route, key in [("project_by_path", "project_path"), ("group_projects", "group")]:
        with pytest.raises(guard.GitLabGuardError):
            guard.build(route, **{key: value})


@pytest.mark.parametrize("value", [
    "a/../b", "a%2f..%2fb", "%252e%252e/x", "a\\b", "a\x00b", "a%00b",
    "https://evil.example/x", "//evil.example/x", "/etc/passwd", "C:/x", "a//b", "./a", "",
    "a/", "a/./b", "a\nb", "a\tb", "a\x7fb", "x" * 4097, None, 5,
    "%2e/a", "a/%252e/b", "C%3A/x", "a%5cb", "a%255cb", "a%2500b",
])
def test_build_refuses_unsafe_file_paths(value):
    from argus.gitlab import guard

    with pytest.raises(guard.GitLabGuardError):
        guard.build("file_raw", project_id=1, file_path=value, query={"ref": "main"})


@pytest.mark.parametrize("route,key", [
    ("file_raw", "file_path"), ("project_by_path", "project_path"), ("group_projects", "group"),
])
def test_build_rejects_deeply_encoded_oversized_parts_promptly(route, key):
    from argus.gitlab import guard

    parts, query, _ = SAMPLES[route]
    parts = parts | {key: "%" + "25" * 40000 + "2F"}
    start = time.perf_counter()
    with pytest.raises(guard.GitLabGuardError):
        guard.build(route, **parts, query=query)
    assert time.perf_counter() - start < 1


@pytest.mark.parametrize("url_type", [str, httpx.URL])
def test_check_rejects_deeply_encoded_oversized_urls_promptly(url_type):
    from argus.gitlab import guard

    path = "%" + "25" * 10000 + "2F"
    url = url_type(ROOT + "/projects/1/repository/files/" + path + "/raw?ref=main")
    start = time.perf_counter()
    with pytest.raises(guard.GitLabGuardError):
        guard.check(url)
    assert time.perf_counter() - start < 1


@pytest.mark.parametrize("url_type", [str, httpx.URL])
@pytest.mark.parametrize("length", [8192, 8193])
def test_check_limits_url_length(url_type, length):
    from argus.gitlab import guard

    url = url_type(ROOT + "/" + "x" * (length - len(ROOT) - 1))
    if length == 8192:
        assert str(guard.check(url)) == str(url)
    else:
        with pytest.raises(guard.GitLabGuardError):
            guard.check(url)


def test_check_limits_segments_expanded_by_url_encoding():
    from argus.gitlab import guard

    url = ROOT + "/projects/1/repository/files/" + "é" * 2049 + "/raw?ref=main"
    with pytest.raises(guard.GitLabGuardError):
        guard.check(url)


@pytest.mark.parametrize("value", ["a" * 39, "a" * 41, "A" * 40, "g" * 40, "../..", "https://evil.example", 5])
def test_discussion_ids_are_lowercase_full_shas(value):
    from argus.gitlab import guard

    with pytest.raises(guard.GitLabGuardError):
        guard.build("mr_discussion", project_id=1, iid=1, discussion_id=value)


@pytest.mark.parametrize("value", ["..", " ", "a\nb", "-x", "a//b", "x.lock", "a/.b", "https://evil.example", "a.", "a/", "a..b", "a.lock/b"])
def test_ref_grammar_is_shared_by_build_and_check(value):
    from urllib.parse import quote
    from argus.gitlab import guard

    with pytest.raises(guard.GitLabGuardError):
        guard.build("file_raw", project_id=1, file_path="a", query={"ref": value})
    with pytest.raises(guard.GitLabGuardError):
        guard.check(ROOT + "/projects/1/repository/files/a/raw?ref=" + quote(value, safe=""))


@pytest.mark.parametrize("route,parts", [
    ("unknown", {}), ("project", {}), ("project", {"project_id": 1, "extra": 2}),
    ("user", {"host": "evil.example"}), ("user", {"url": "https://evil.example"}),
    ("user", {"base": "https://evil.example"}), ("user", {"project_id": 1}),
])
def test_routes_accept_only_their_exact_parts(route, parts):
    from argus.gitlab import guard

    with pytest.raises(guard.GitLabGuardError):
        guard.build(route, **parts)


def test_unconfigured_and_invalid_configuration(monkeypatch):
    from argus.gitlab import guard

    monkeypatch.delenv("ARGUS_GITLAB_URL")
    for call in [lambda: guard.build("user"), lambda: guard.check(ROOT + "/user")]:
        with pytest.raises(guard.GitLabGuardError, match="GitLab is not configured"):
            call()
    monkeypatch.setenv("ARGUS_GITLAB_URL", "http://bad.test")
    for call in [lambda: guard.build("user"), lambda: guard.check(ROOT + "/user")]:
        with pytest.raises(guard.GitLabGuardError):
            call()


@pytest.mark.parametrize("path,encoded", [
    ("src/dir/a b.py", "src%2Fdir%2Fa%20b.py"),
    ("src/é.py", "src%2F%C3%A9.py"), ("a@b+c.py", "a%40b%2Bc.py"),
    ("a?#.py", "a%3F%23.py"), ("x" * 4096, "x" * 4096),
])
def test_file_paths_are_encoded_as_one_segment(path, encoded):
    from argus.gitlab import guard

    url = guard.build("file_raw", project_id=1, file_path=path, query={"ref": "main"})
    assert str(url) == ROOT + "/projects/1/repository/files/" + encoded + "/raw?ref=main"
    assert guard.check(url) == url


def test_valid_path_and_id_boundaries():
    from argus.gitlab import guard

    for path in ["/".join(["a"] * 22), "A_1.b-c/" + "x" * 255, "/".join(["a" * 255] * 3 + ["b" * 254, "c"])]:
        url = guard.build("project_by_path", project_path=path)
        assert url.raw_path.count(b"/") == 4
        assert guard.check(url) == url
    for value in [1, 10**18 - 1]:
        assert str(guard.build("project", project_id=value)) == ROOT + f"/projects/{value}"
        assert str(guard.build("group_projects", group=value)) == ROOT + f"/groups/{value}/projects"
    for value in [True, 0, -1, 10**18, 1.0]:
        with pytest.raises(guard.GitLabGuardError):
            guard.build("group_projects", group=value)


@pytest.mark.parametrize("prefix", ["", "/gitlab", "/a/b"])
def test_every_route_round_trips_with_exact_path_and_allowed_queries(monkeypatch, prefix):
    from argus.gitlab import guard

    monkeypatch.setenv("ARGUS_GITLAB_URL", "https://gitlab.test" + prefix)
    for name, route in guard.ROUTES.items():
        parts, required, path = SAMPLES[name]
        assert route.name == name
        url = guard.build(name, **parts, query=required)
        assert url.raw_path.split(b"?")[0].decode() == prefix + "/api/v4" + path
        assert guard.check(str(url)) == url
        assert guard.check(url) == url
        for key in route.query:
            query = required | {key: QUERY_VALUES[key]}
            checked = guard.build(name, **parts, query=query)
            assert guard.check(checked) == checked
            expected = QUERY_VALUES[key]
            texts = [str(v) for v in expected] if isinstance(expected, list) else [str(expected).lower() if isinstance(expected, bool) else str(expected)]
            assert checked.params.get_list(key) == texts
        with pytest.raises(guard.GitLabGuardError):
            guard.build(name, **parts, query=required | {"unknown": "x"})
        with pytest.raises(guard.GitLabGuardError):
            guard.check(url.copy_add_param("unknown", "x"))


@pytest.mark.parametrize("path", [
    "/gitlab-evil/api/v4/user", "/gitlabx/api/v4/user", "/gitlab2/api/v4/user",
    "/GITLAB/api/v4/user", "/api/v4/user", "/x/gitlab/api/v4/user",
])
def test_prefix_matches_whole_case_sensitive_segments(monkeypatch, path):
    from argus.gitlab import guard

    monkeypatch.setenv("ARGUS_GITLAB_URL", "https://gitlab.test/gitlab")
    with pytest.raises(guard.GitLabGuardError):
        guard.check("https://gitlab.test" + path)


@pytest.mark.parametrize("path", ["/api/v3/x", "/api/v4", "/api/v4/", "/other/api/v4/x"])
def test_api_root_must_be_exact_and_have_a_resource(path):
    from argus.gitlab import guard

    with pytest.raises(guard.GitLabGuardError):
        guard.check("https://gitlab.test" + path)


def test_unmatched_api_paths_are_allowed_without_encoding_or_query():
    from argus.gitlab import guard

    assert str(guard.check(ROOT + "/projects/1/unknown")) == ROOT + "/projects/1/unknown"
    for suffix in ["?page=1", "?", "/%61", "/%252F"]:
        with pytest.raises(guard.GitLabGuardError):
            guard.check(ROOT + "/projects/1/unknown" + suffix)


@pytest.mark.parametrize("url", [
    "http://gitlab.test/api/v4/user", "https://gitlab.test:8443/api/v4/user",
    "https://evil.example/api/v4/user", "https://gitlab.test./api/v4/user",
    "https://user:pw@gitlab.test/api/v4/user", "https://gitlab.test@evil.example/api/v4/user",
    "https://gitlab.test.evil.example/api/v4/user", ROOT + "/user#fragment", ROOT + "/user#",
    ROOT + "//user", ROOT + "/projects/1/../2", ROOT + "/projects/1/./x",
    ROOT + "/projects/%2e%2e/2", ROOT + "/a\\b", ROOT + "/a\tb", ROOT + "/a\nb",
    ROOT + "/%00", ROOT + "/%5c", ROOT + "/%7f", ROOT + "/%0a", ROOT + "/%C2%85",
    "https://@gitlab.test/api/v4/user", "https://gitlab.test:bad/api/v4/user", None, 5,
])
def test_check_refuses_unsafe_urls_with_only_guard_errors(url):
    from argus.gitlab import guard

    with pytest.raises(guard.GitLabGuardError):
        guard.check(url)


def test_httpx_normalization_and_ipv6_configuration(monkeypatch):
    from argus.gitlab import guard

    assert str(guard.check("https://GITLAB.TEST:443/api/v4/user")) == ROOT + "/user"
    monkeypatch.setenv("ARGUS_GITLAB_URL", "https://[2001:db8::1]:8443/gitlab")
    url = guard.build("user")
    assert str(url) == "https://[2001:db8::1]:8443/gitlab/api/v4/user"
    assert guard.check(url) == url
    with pytest.raises(guard.GitLabGuardError):
        guard.check("https://[2001:db8::1]/gitlab/api/v4/user")
    monkeypatch.setenv("ARGUS_GITLAB_URL", "https://127.0.0.1")
    assert guard.build("user").host == "127.0.0.1"


def test_configured_ascii_internationalized_hostname_matches_httpx(monkeypatch):
    from argus.gitlab import guard

    monkeypatch.setenv("ARGUS_GITLAB_URL", "https://xn--bcher-kva.example")
    url = guard.build("user")
    assert str(url) == "https://xn--bcher-kva.example/api/v4/user"
    assert guard.check("https://bücher.example/api/v4/user") == url
    for hostname in ["xn--bcher-kva.example.", "xn--bcher-kva.example.evil.test", "bücher.evil.test"]:
        with pytest.raises(guard.GitLabGuardError):
            guard.check("https://" + hostname + "/api/v4/user")


@pytest.mark.parametrize("key,value", [
    ("per_page", 0), ("per_page", 101), ("page", 0), ("page", 1_000_001),
    ("state", "bogus"), ("scope", "bogus"), ("sort", "ASC"), ("order_by", "id"),
    ("updated_after", "2026-01-01T12:00:00"), ("updated_after", "2026-01-01 12:00:00Z"),
    ("updated_after", "2026-02-30T12:00:00Z"), ("updated_after", "2026-01-01T12:00:00+99:00"),
])
def test_query_value_validation_is_shared(key, value):
    from urllib.parse import urlencode
    from argus.gitlab import guard

    with pytest.raises(guard.GitLabGuardError):
        guard.build("mr_list", project_id=1, query={key: value})
    with pytest.raises(guard.GitLabGuardError):
        guard.check(ROOT + "/projects/1/merge_requests?" + urlencode({key: value}))


@pytest.mark.parametrize("method", ["build", "check"])
@pytest.mark.parametrize("key", ["updated_after", "updated_before"])
@pytest.mark.parametrize("offset", ["+00:60", "+01:99", "+24:00", "-00:60", "-01:99", "-24:00"])
def test_datetime_refuses_invalid_offsets(method, key, offset):
    from urllib.parse import urlencode
    from argus.gitlab import guard

    query = {key: "2026-01-01T12:00:00" + offset}
    with pytest.raises(guard.GitLabGuardError):
        if method == "build":
            guard.build("mr_list", project_id=1, query=query)
        else:
            guard.check(ROOT + "/projects/1/merge_requests?" + urlencode(query))


@pytest.mark.parametrize("key", ["updated_after", "updated_before"])
@pytest.mark.parametrize("offset", ["Z", "+00:00", "-05:30", "+14:00"])
def test_datetime_accepts_valid_offsets_in_build_and_check(key, offset):
    from urllib.parse import urlencode
    from argus.gitlab import guard

    value = "2026-01-01T12:00:00" + offset
    query = {key: value}
    assert guard.build("mr_list", project_id=1, query=query).params[key] == value
    url = ROOT + "/projects/1/merge_requests?" + urlencode(query)
    assert guard.check(url).params[key] == value


@pytest.mark.parametrize("value", [
    "2026-00-01T12:00:00Z", "2026-13-01T12:00:00Z", "2026-01-00T12:00:00Z",
    "2026-01-32T12:00:00Z", "2026-01-01T24:00:00Z", "2026-01-01T12:60:00Z",
    "2026-01-01T12:00:60Z",
])
def test_datetime_refuses_invalid_calendar_and_clock_fields(value):
    from urllib.parse import urlencode
    from argus.gitlab import guard

    query = {"updated_after": value}
    with pytest.raises(guard.GitLabGuardError):
        guard.build("mr_list", project_id=1, query=query)
    with pytest.raises(guard.GitLabGuardError):
        guard.check(ROOT + "/projects/1/merge_requests?" + urlencode(query))


def test_query_python_types_and_route_specific_keys_are_strict():
    from datetime import datetime
    from argus.gitlab import guard

    for route, key, value in [
        ("mr_list", "page", "1"), ("mr_list", "page", True), ("mr_list", "page", 1.0),
        ("mr_list", "page", [1]), ("mr_list", "page", (1,)), ("mr_list", "state", 1),
        ("mr_list", "updated_after", datetime(2026, 1, 1)),
        ("group_projects", "archived", "yes"), ("group_projects", "archived", "true"),
        ("group_projects", "archived", 1), ("group_projects", "min_access_level", 25),
        ("group_projects", "min_access_level", "30"), ("mr_diffs", "ref", "main"),
    ]:
        parts = SAMPLES[route][0]
        with pytest.raises(guard.GitLabGuardError):
            guard.build(route, **parts, query={key: value})
    for query in [[], "page=1", 5]:
        with pytest.raises(guard.GitLabGuardError):
            guard.build("mr_list", project_id=1, query=query)
    for suffix in [
        "/groups/1/projects?archived=yes", "/groups/1/projects?min_access_level=25",
        "/projects/1/merge_requests/2/diffs?ref=main", "/projects/1/merge_requests?page=1&page=2",
        "/projects/1/merge_requests?page=" + "9" * 5000,
    ]:
        with pytest.raises(guard.GitLabGuardError):
            guard.check(ROOT + suffix)


def test_required_and_multi_value_queries():
    from argus.gitlab import guard

    for name in ["file_raw", "compare", "merge_base"]:
        parts, query, path = SAMPLES[name]
        for key in query:
            missing = {k: v for k, v in query.items() if k != key}
            with pytest.raises(guard.GitLabGuardError):
                guard.build(name, **parts, query=missing)
            with pytest.raises(guard.GitLabGuardError):
                guard.check(httpx.URL(ROOT + path, params=missing))
    for values in [["main"], ["main", "other", "third"], "main", ["main", 5]]:
        with pytest.raises(guard.GitLabGuardError):
            guard.build("merge_base", project_id=1, query={"refs[]": values})
    for values in [["main"], ["main", "other", "third"]]:
        with pytest.raises(guard.GitLabGuardError):
            guard.check(httpx.URL(ROOT + SAMPLES["merge_base"][2], params={"refs[]": values}))
    url = guard.build("merge_base", project_id=1, query={"refs[]": ("main", "feature/x")})
    assert url.params.multi_items() == [("refs[]", "main"), ("refs[]", "feature/x")]


def test_enum_members_and_datetime_offsets_round_trip():
    from datetime import datetime, timedelta, timezone
    from argus.gitlab import guard

    for key, values in {
        "state": ["opened", "closed", "merged", "locked", "all"],
        "scope": ["created_by_me", "assigned_to_me", "all"],
        "order_by": ["created_at", "updated_at"], "sort": ["asc", "desc"],
    }.items():
        for value in values:
            assert guard.build("mr_list", project_id=1, query={key: value}).params[key] == value
    for key, values in {
        "order_by": ["id", "name", "path", "created_at", "updated_at", "last_activity_at"],
        "min_access_level": [5, 10, 15, 20, 30, 40, 50],
    }.items():
        for value in values:
            assert guard.build("group_projects", group=1, query={key: value}).params[key] == str(value)
    value = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=1)))
    url = guard.build("mr_list", project_id=1, query={"updated_after": value})
    assert b"%2B01%3A00" in url.query
    assert url.params["updated_after"] == value.isoformat()


@pytest.mark.parametrize("encoded", ["a%252Fb", "a%2fb", "%2e%2e", "a%5Cb", "%61", "%FF", "%ED%A0%80"])
def test_check_requires_canonical_encoded_parts(encoded):
    from argus.gitlab import guard

    with pytest.raises(guard.GitLabGuardError):
        guard.check(ROOT + "/projects/1/repository/files/" + encoded + "/raw?ref=main")
    with pytest.raises(guard.GitLabGuardError):
        guard.check(ROOT + "/projects/grp%2F" + encoded)


def test_errors_never_echo_input_values():
    from argus.gitlab import guard

    marker = "SECRET" + "VALUE"
    for call in [
        lambda: guard.build(marker), lambda: guard.build("project", project_id=marker),
        lambda: guard.build("user", **{marker: "x"}),
        lambda: guard.build("mr_list", project_id=1, query={marker: "x"}),
        lambda: guard.build("mr_list", project_id=1, query={"page": marker}),
        lambda: guard.check("https://" + marker + "@evil.test/api/v4/user"),
        lambda: guard.check(ROOT + "/" + marker + "\n"),
    ]:
        with pytest.raises(guard.GitLabGuardError) as error:
            call()
        assert marker not in str(error.value)


def test_check_fuzz_only_returns_configured_host_or_guard_error():
    import random
    from argus.gitlab import guard

    randomizer = random.Random(16)
    alphabet = "abc012/%?#@:.\\\x00\t\n\x7f\x85é\ud800"
    for _ in range(500):
        suffix = "".join(randomizer.choices(alphabet, k=randomizer.randrange(50)))
        try:
            url = guard.check(ROOT + "/" + suffix)
        except guard.GitLabGuardError:
            continue
        assert isinstance(url, httpx.URL)
        assert url.host == "gitlab.test"


def test_guard_never_constructs_a_client_or_sends_a_request(monkeypatch):
    from argus.gitlab import guard

    def forbidden(*args, **kwargs):
        pytest.fail("URL validation must not create clients or send requests")

    assert not hasattr(guard, "Client")
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.Client, "__init__", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", forbidden)
    url = guard.build("user")
    assert guard.check(url) == url


def test_build_checks_its_result(monkeypatch):
    from argus.gitlab import guard

    def refuse(url):
        raise guard.GitLabGuardError("refused")

    monkeypatch.setattr(guard, "check", refuse)
    with pytest.raises(guard.GitLabGuardError, match="refused"):
        guard.build("user")
