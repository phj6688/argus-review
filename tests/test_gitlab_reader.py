"""Exercise reader coverage and revision boundaries with recorded responses."""

import importlib
import json
import os
from pathlib import Path

import httpx
import pytest

from argus import context, errors, forge, review
from argus.diff import FileList, FilePatch, change_stats
from argus.forge.errors import ForgeDenied, ForgeError
from argus.forge.types import DiffRefs, RepoRef
from argus.gitlab.client import GitLabAuthError, GitLabClient, GitLabDenied, GitLabHTTPError
from argus.tokens import HeuristicEstimator

FIXTURES = Path(__file__).parent / "fixtures/gitlab/spike_a"
HOST = "gitlab.example.test"
KEY = f"gitlab:{HOST}#1"
PROJECT = "/api/v4/projects/1"
MR = PROJECT + "/merge_requests/5"
HEAD = "a" * 40
OTHER_HEAD = "b" * 40


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())["response"]


def response(data):
    return httpx.Response(200, json=data["body"], headers=data["headers"])


def entry(**fields):
    return fixture("q01_diffs_pp100_p1")["body"][0] | fields


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("ARGUS_GITLAB_", "ARGUS_CONTEXT_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ARGUS_GITLAB_URL", f"https://{HOST}")
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", "gl" + "pat-" + "x" * 20)
    monkeypatch.setattr(GitLabClient, "retry_delay", 0)


@pytest.fixture
def make_reader():
    clients = []

    def make(*, mr=None, entries=None, routes=None):
        from argus.gitlab.reader import GitLabForge

        data = fixture("q04_mr_many")
        data["body"].update({"sha": HEAD, "changes_count": "1"} | (mr or {}))
        versions = fixture("q03_versions")
        for version in versions["body"]:
            version["head_commit_sha"] = HEAD
        diffs = fixture("q01_diffs_pp100_p2")
        diffs["body"] = [entry()] if entries is None else entries
        table = {
            (MR, "1"): response(data),
            (MR + "/versions", "1"): response(versions),
            (MR + "/diffs", "1"): response(diffs),
        } | (routes or {})
        requests = []

        def handle(request):
            requests.append(request)
            assert request.method == "GET"
            assert request.url.path.startswith("/api/v4/projects/")
            assert "/changes" not in request.url.path
            result = table[(request.url.path, request.url.params.get("page", "1"))]
            return result(request) if callable(result) else result

        client = GitLabClient(transport=httpx.MockTransport(handle))
        clients.append(client)
        return GitLabForge(client), requests, table, client

    yield make
    for client in clients:
        client.close()


def test_normal_mr_and_rename(make_reader):
    items = [entry(), entry(new_path="new.py", old_path="old.py", new_file=False,
                           renamed_file=True), entry(new_path="gone.py", deleted_file=True,
                                                     new_file=False)]
    reader, requests, _, _ = make_reader(entries=items, mr={"changes_count": "3"})
    files = reader.change_files(KEY, 5)
    assert isinstance(files, FileList)
    assert files == [
        FilePatch("bulk/file_001.py", "added", "@@ -0,0 +1 @@\n+VALUE = 1\n"),
        FilePatch("new.py", "renamed", "@@ -0,0 +1 @@\n+VALUE = 1\n",
                  previous_filename="old.py"),
        FilePatch("gone.py", "removed", "@@ -0,0 +1 @@\n+VALUE = 1\n"),
    ]
    assert files.unseen is False
    assert change_stats(files) == (3, 3, 0)
    assert [r.url.path for r in requests] == [
        MR, MR + "/versions", MR + "/diffs", MR, MR + "/versions",
    ]


@pytest.mark.parametrize("name,path,status", [
    ("q02_diffs_binary", "assets/logo.png", "modified"),
    ("q02_diffs_toolarge", "big/huge_dump.txt", "added"),
])
def test_unavailable_patches_mark_partial_coverage(make_reader, name, path, status):
    reader, _, _, _ = make_reader(routes={(MR + "/diffs", "1"): response(fixture(name))})
    files = reader.change_files(KEY, 5)
    assert files == [FilePatch(path, status, None)]
    assert files.unseen is True
    assert review._llm_file_counts(files, context.RenderedContext())["files_unseen"] is True


def test_generated_files_keep_no_patch(make_reader):
    generated = next(item for item in fixture("q02_diffs_generated")["body"]
                     if item["generated_file"])
    assert generated["diff"]
    reader, _, _, _ = make_reader(entries=[generated])
    files = reader.change_files(KEY, 5)
    assert files == [FilePatch("package-lock.json", "added", None)]
    assert files.unseen is True


def test_collapsed_file_marks_unseen(make_reader):
    reader, _, _, _ = make_reader(entries=[entry(collapsed=True)])
    files = reader.change_files(KEY, 5)
    assert files[0].patch is None
    assert files.unseen is True


def test_150_files_use_all_pages(make_reader):
    routes = {}
    for page, start, stop in [(1, 1, 101), (2, 101, 151)]:
        data = fixture(f"q01_diffs_pp100_p{page}")
        data["body"] = [data["body"][0] | {"new_path": f"bulk/file_{n:03}.py",
                                         "old_path": f"bulk/file_{n:03}.py"}
                        for n in range(start, stop)]
        routes[(MR + "/diffs", str(page))] = response(data)
    reader, requests, _, _ = make_reader(mr={"changes_count": "150"}, routes=routes)
    files = reader.change_files(KEY, 5)
    assert len(files) == len({f.filename for f in files}) == 150
    assert files[0].filename == "bulk/file_001.py"
    assert files[-1].filename == "bulk/file_150.py"
    assert files.unseen is False
    paged = [r for r in requests if r.url.path.endswith(("/versions", "/diffs"))]
    assert all(r.url.params["per_page"] == "100" for r in paged)
    assert [r.url.params.get("page", "1") for r in paged] == ["1", "1", "2", "1"]


@pytest.mark.parametrize("fields,unseen", [
    ({}, True), ({"renamed_file": True}, False),
    ({"a_mode": "100644", "b_mode": "100755"}, False),
    ({"deleted_file": True}, True),
    ({"deleted_file": True, "a_mode": "100644", "b_mode": "0"}, True),
    ({"new_file": True, "a_mode": "0", "b_mode": "100644"}, True),
    ({"deleted_file": True, "a_mode": "100644", "b_mode": "100755"}, True),
    ({"new_file": True, "a_mode": "100644", "b_mode": "100755"}, True),
    ({"a_mode": "0", "b_mode": "100644"}, True),
    ({"a_mode": "100644", "b_mode": "0"}, True),
])
def test_pre_18_4_empty_diff_rules(make_reader, fields, unseen):
    item = entry(diff="", new_file=False, a_mode="100644", b_mode="100644") | fields
    del item["collapsed"], item["too_large"]
    reader, _, _, _ = make_reader(entries=[item])
    files = reader.change_files(KEY, 5)
    assert len(files) == 1
    assert files[0].patch is None
    assert files.unseen is unseen


def test_empty_diff_with_false_flags_is_complete(make_reader):
    reader, _, _, _ = make_reader(entries=[entry(diff="")])
    files = reader.change_files(KEY, 5)
    assert files[0].patch is None
    assert files.unseen is False


@pytest.mark.parametrize("count,unseen", [
    ("1000+", True), ("384+", True), ("2", True), (2, True),
    (None, True), ("", True), ("unknown", True), ("1", False), (1, False),
])
def test_changes_count_controls_coverage(make_reader, count, unseen):
    reader, _, _, _ = make_reader(mr={"changes_count": count})
    assert reader.change_files(KEY, 5).unseen is unseen


@pytest.mark.parametrize("fields", [{"new_path": "../bad.py"},
    {"renamed_file": True, "old_path": "../bad.py"}])
def test_unsafe_paths_are_skipped(make_reader, fields):
    reader, _, _, _ = make_reader(entries=[entry(**fields)])
    files = reader.change_files(KEY, 5)
    assert files == []
    assert files.unseen is True


def test_missing_new_path_uses_old_path(make_reader):
    item = entry(new_file=False)
    del item["new_path"]
    reader, _, _, _ = make_reader(entries=[item])
    assert reader.change_files(KEY, 5)[0].filename == "bulk/file_001.py"


def test_unified_headers_are_removed(make_reader):
    item = fixture("q02_diffs_ladder_unidiff")["body"][0]
    reader, _, _, _ = make_reader(entries=[item])
    patch = reader.change_files(KEY, 5)[0].patch
    assert patch == item["diff"].split("\n", 2)[2]
    assert patch.startswith("@@")


def test_binary_words_inside_a_hunk_keep_the_patch(make_reader):
    patch = "@@ -0,0 +1 @@\n+Binary files a/x and b/x differ\n"
    reader, _, _, _ = make_reader(entries=[entry(diff=patch)])
    files = reader.change_files(KEY, 5)
    assert files[0].patch == patch
    assert files.unseen is False


@pytest.mark.parametrize("method", ["diff_refs", "change_files"])
def test_newest_version_with_another_head_raises_head_moved(make_reader, method):
    data = fixture("q03_versions")
    data["body"] = [data["body"][0] | {"head_commit_sha": OTHER_HEAD, "id": 100},
                    data["body"][1] | {"head_commit_sha": HEAD, "id": 8},
                    data["body"][2] | {"head_commit_sha": HEAD, "id": 13,
                                       "base_commit_sha": "c" * 40, "start_commit_sha": "d" * 40}]
    reader, requests, _, _ = make_reader(routes={(MR + "/versions", "1"): response(data)})
    with pytest.raises(errors.HeadMoved) as raised:
        getattr(reader, method)(KEY, 5)
    assert review.fallback_eligible(raised.value) is False
    assert not any(r.url.path.endswith("/diffs") for r in requests)


def test_version_matches_head_and_highest_id(make_reader):
    data = fixture("q03_versions")
    version = data["body"][0]
    data["body"] = [version | {"head_commit_sha": OTHER_HEAD, "id": 8},
                    version | {"head_commit_sha": HEAD, "id": 100,
                               "base_commit_sha": "c" * 40, "start_commit_sha": "d" * 40},
                    version | {"head_commit_sha": HEAD, "id": 13},
                    version | {"head_commit_sha": None, "id": 1}]
    reader, _, _, _ = make_reader(routes={(MR + "/versions", "1"): response(data)})
    assert reader.diff_refs(KEY, 5) == DiffRefs("c" * 40, "d" * 40, HEAD)
    assert reader.change_files(KEY, 5).unseen is False


@pytest.mark.parametrize("method", ["diff_refs", "change_files"])
@pytest.mark.parametrize("newest_head", [HEAD, OTHER_HEAD])
def test_versions_only_read_first_page(make_reader, method, newest_head):
    data = fixture("q03_versions")
    data["body"] = [data["body"][0] | {"head_commit_sha": newest_head, "id": 100}]
    data["headers"]["x-next-page"] = "2"
    second = fixture("q03_versions")
    second["body"] = [second["body"][0] | {"head_commit_sha": HEAD, "id": 99}]
    second["headers"]["x-next-page"] = "3"
    reader, requests, _, _ = make_reader(routes={
        (MR + "/versions", "1"): response(data),
        (MR + "/versions", "2"): response(second),
    })
    if newest_head == HEAD:
        getattr(reader, method)(KEY, 5)
    else:
        with pytest.raises(errors.HeadMoved):
            getattr(reader, method)(KEY, 5)
    versions = [r for r in requests if r.url.path.endswith("/versions")]
    assert len(versions) == (2 if method == "change_files" and newest_head == HEAD else 1)
    assert all(r.url.params.get("page", "1") == "1" for r in versions)
    assert all(r.url.params["per_page"] == "100" for r in versions)


@pytest.mark.parametrize("method", ["diff_refs", "change_files"])
@pytest.mark.parametrize("versions", [
    [None], [{"head_commit_sha": HEAD}],
    [{"id": None, "head_commit_sha": HEAD}],
    [{"id": "1", "head_commit_sha": HEAD}],
    [{"id": True, "head_commit_sha": HEAD}],
    [{"id": 1.5, "head_commit_sha": HEAD}],
    [{"id": 1, "head_commit_sha": 42}],
    [{"id": 100, "head_commit_sha": HEAD, "base_commit_sha": HEAD,
      "start_commit_sha": HEAD}, {"id": 1, "head_commit_sha": False}],
])
def test_invalid_versions_cannot_fallback(make_reader, method, versions):
    reader, _, _, _ = make_reader(routes={
        (MR + "/versions", "1"): httpx.Response(200, json=versions),
    })
    with pytest.raises(GitLabHTTPError, match="Invalid GitLab reader response") as raised:
        getattr(reader, method)(KEY, 5)
    assert raised.value.reason == "http"
    assert review.fallback_eligible(raised.value) is False


@pytest.mark.parametrize("method", ["diff_refs", "change_files"])
@pytest.mark.parametrize("ref", ["base_commit_sha", "start_commit_sha"])
@pytest.mark.parametrize("fields", [{}, {"value": None}, {"value": ""},
                                    {"value": 42}, {"value": False}],
                         ids=["missing", "null", "empty", "number", "boolean"])
def test_invalid_selected_version_refs_cannot_fallback(make_reader, method, ref, fields):
    version = fixture("q03_versions")["body"][0] | {"head_commit_sha": HEAD}
    del version[ref]
    if fields:
        version[ref] = fields["value"]
    reader, _, _, _ = make_reader(routes={
        (MR + "/versions", "1"): httpx.Response(200, json=[version]),
    })
    with pytest.raises(GitLabHTTPError, match="Invalid GitLab reader response") as raised:
        getattr(reader, method)(KEY, 5)
    assert raised.value.reason == "http"
    assert review.fallback_eligible(raised.value) is False


@pytest.mark.parametrize("page", ["1", "2"])
def test_invalid_diff_entries_cannot_fallback(make_reader, page):
    routes = {(MR + "/diffs", page): httpx.Response(200, json=[None])}
    if page == "2":
        routes[(MR + "/diffs", "1")] = httpx.Response(
            200, json=[entry()], headers={"x-next-page": "2"})
    reader, _, _, _ = make_reader(routes=routes)
    with pytest.raises(GitLabHTTPError, match="Invalid GitLab reader response") as raised:
        reader.change_files(KEY, 5)
    assert raised.value.reason == "http"
    assert review.fallback_eligible(raised.value) is False


@pytest.mark.parametrize("method", ["diff_refs", "change_files"])
@pytest.mark.parametrize("versions", [[], [{"id": 100, "head_commit_sha": OTHER_HEAD}]])
def test_missing_matching_version_raises_head_moved(make_reader, method, versions):
    reader, requests, _, _ = make_reader(mr={"diff_refs": None}, routes={
        (MR + "/versions", "1"): httpx.Response(200, json=versions),
    })
    with pytest.raises(errors.HeadMoved) as raised:
        getattr(reader, method)(KEY, 5)
    assert review.fallback_eligible(raised.value) is False
    assert errors.classify_error(raised.value) == "head_moved"
    assert not any(r.url.path.endswith("/diffs") for r in requests)


@pytest.mark.parametrize("method", ["head_sha", "diff_refs", "change_files"])
def test_missing_head_raises_head_moved(make_reader, method):
    reader, requests, _, _ = make_reader(mr={"sha": None})
    with pytest.raises(errors.HeadMoved):
        getattr(reader, method)(KEY, 5)
    assert len(requests) == 1


def test_head_race_raises_instead_of_returning_files(make_reader):
    reader, requests, table, _ = make_reader()
    original = table[(MR, "1")].json()
    payloads = iter([original, original | {"sha": OTHER_HEAD}])
    table[(MR, "1")] = lambda _: httpx.Response(200, json=next(payloads))
    with pytest.raises(errors.HeadMoved):
        reader.change_files(KEY, 5)
    assert [r.url.path for r in requests] == [MR, MR + "/versions", MR + "/diffs", MR]


@pytest.mark.parametrize("changed", [
    {}, {"id": 100}, {"base_commit_sha": "c" * 40},
    {"start_commit_sha": "d" * 40}, {"head_commit_sha": OTHER_HEAD},
], ids=["unchanged", "new-id-same-head", "base", "start", "head"])
def test_version_rechecked_after_all_diff_pages(make_reader, changed):
    reader, requests, table, _ = make_reader(mr={"changes_count": "2"}, routes={
        (MR + "/diffs", "1"): httpx.Response(
            200, json=[entry()], headers={"x-next-page": "2"}),
        (MR + "/diffs", "2"): httpx.Response(
            200, json=[entry(new_path="bulk/file_002.py")]),
    })
    original = table[(MR + "/versions", "1")].json()
    payloads = iter([original, [original[0] | changed, *original[1:]]])
    table[(MR + "/versions", "1")] = lambda _: httpx.Response(200, json=next(payloads))
    if changed:
        with pytest.raises(errors.HeadMoved) as raised:
            reader.change_files(KEY, 5)
        assert review.fallback_eligible(raised.value) is False
    else:
        files = reader.change_files(KEY, 5)
        assert [file.filename for file in files] == ["bulk/file_001.py", "bulk/file_002.py"]
        assert files.unseen is False
    assert [r.url.path for r in requests] == [
        MR, MR + "/versions", MR + "/diffs", MR + "/diffs", MR, MR + "/versions",
    ]


def test_fork_reads_head_through_target_project(make_reader):
    data = fixture("q10_mr_fork")
    raw = fixture("q10_file_raw_at_head_sha")
    path = PROJECT + "/repository/files/src/app.py/raw"
    reader, requests, _, _ = make_reader(routes={
        (MR, "1"): response(data),
        (path, "1"): httpx.Response(200, text=raw["body"], headers=raw["headers"]),
    })
    meta = reader.change_meta(KEY, 5)
    assert meta.source_repo == RepoRef("gitlab", HOST, None, data["body"]["source_project_id"])
    assert reader.read_file(KEY, "src/app.py", meta.head_sha) == raw["body"]
    request = requests[-1]
    assert request.url.raw_path.split(b"?", 1)[0] == b"/api/v4/projects/1/repository/files/src%2Fapp.py/raw"
    assert dict(request.url.params) == {"ref": data["body"]["sha"]}
    assert int(request.url.path.split("/")[4]) == data["body"]["target_project_id"]
    assert int(request.url.path.split("/")[4]) != data["body"]["source_project_id"]


def test_reader_fork_source_must_pass_live_policy(make_reader, monkeypatch, tmp_path):
    from argus import repo_policy
    from argus.api import db
    from argus.gitlab import projects

    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", f"gitlab:{HOST}/allowed")
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "blocked")
    path = str(tmp_path / "projects.db")
    db.init_db(path)
    target = {"id": 1, "path_with_namespace": "allowed/app", "archived": False,
              "default_branch": "main", "visibility": "private"}
    source = target | {"id": 43, "path_with_namespace": "blocked/fork"}
    reader, requests, _, client = make_reader(mr={"source_project_id": 43}, routes={
        (PROJECT, "1"): httpx.Response(200, json=target),
        ("/api/v4/projects/43", "1"): httpx.Response(200, json=source),
    })
    meta = reader.change_meta(KEY, 5)
    decision = projects.live_deny_change(KEY, meta, client=client, db_path=path)
    assert decision == repo_policy.GitLabDecision(False, "fork_source_denied")
    assert [r.url.path for r in requests] == [MR, PROJECT, "/api/v4/projects/43"]


def test_missing_file_returns_none(make_reader):
    reader, _, _, _ = make_reader(routes={
        (PROJECT + "/repository/files/absent.py/raw", "1"): httpx.Response(404),
    })
    assert reader.read_file(KEY, "absent.py", HEAD) is None


def test_guard_refused_file_returns_none(make_reader):
    reader, requests, _, _ = make_reader()
    assert reader.read_file(KEY, "../odd.py", HEAD) is None
    assert requests == []


@pytest.mark.parametrize("status,error", [(403, GitLabDenied), (401, GitLabAuthError),
                                         (500, GitLabHTTPError)])
def test_file_failures_propagate(make_reader, status, error):
    reader, _, _, _ = make_reader(routes={
        (PROJECT + "/repository/files/a.py/raw", "1"): httpx.Response(status),
    })
    with pytest.raises(error):
        reader.read_file(KEY, "a.py", HEAD)


def test_file_decode_replaces_invalid_utf8(make_reader):
    reader, _, _, _ = make_reader(routes={
        (PROJECT + "/repository/files/a.py/raw", "1"): httpx.Response(200, content=b"x\xff"),
    })
    assert reader.read_file(KEY, "a.py", HEAD) == "x\ufffd"


@pytest.mark.parametrize("branch,status,text,expected", [
    ("main", 200, "rules", "rules"), ("main", 404, "", None),
    (None, 200, "", None), ("", 200, "", None), (12, 200, "", None),
    ("main", 200, "x" * (review.HOUSE_RULES_MAX_CHARS + 10), "x" * review.HOUSE_RULES_MAX_CHARS),
    ("main", 200, "", ""),
], ids=["found", "missing", "null-branch", "empty-branch", "invalid-branch", "truncated", "empty-file"])
def test_house_rules_use_default_branch(make_reader, branch, status, text, expected):
    project = fixture("q05_project_by_path_ok")
    project["body"]["default_branch"] = branch
    path = PROJECT + "/repository/files/CLAUDE.md/raw"
    reader, requests, _, _ = make_reader(routes={
        (PROJECT, "1"): response(project), (path, "1"): httpx.Response(status, text=text),
    })
    assert reader.house_rules(KEY) == expected
    assert [r.url.path for r in requests] == ([PROJECT, path] if branch == "main" else [PROJECT])
    if branch == "main":
        assert dict(requests[1].url.params) == {"ref": "main"}


@pytest.mark.parametrize("state,expected,merged", [("opened", "open", False),
    ("locked", "open", False), ("closed", "closed", False), ("merged", "closed", True)])
def test_change_meta_states(make_reader, state, expected, merged):
    reader, _, _, _ = make_reader(mr={"state": state, "draft": True, "diff_refs": None})
    meta = reader.change_meta(KEY, 5)
    assert (meta.state, meta.merged, meta.draft, meta.base_sha) == (expected, merged, True, None)
    assert meta.head_sha == HEAD


@pytest.mark.parametrize("name,bot", [("q07_mr_human", False), ("q07_mr_projtoken", True),
                                     ("q07_mr_serviceaccount", True)])
def test_change_meta_authors(make_reader, name, bot):
    data = fixture(name)
    reader, _, _, _ = make_reader(routes={(MR, "1"): response(data)})
    meta = reader.change_meta(KEY, 5)
    assert meta.author_is_bot is bot
    assert meta.author_login == data["body"]["author"]["username"]
    assert meta.author_id == str(data["body"]["author"]["id"])
    assert meta.base_sha == data["body"]["diff_refs"]["base_sha"]
    assert meta.title == data["body"]["title"]
    assert meta.updated_at == data["body"]["updated_at"]
    assert reader.head_sha(KEY, 5) == data["body"]["sha"]


@pytest.mark.parametrize("fields", [{}, {"source_project_id": None}])
def test_change_meta_optional_fields_and_legacy_draft(make_reader, fields):
    data = {"state": "opened", "work_in_progress": True} | fields
    reader, _, _, _ = make_reader(routes={(MR, "1"): httpx.Response(200, json=data)})
    meta = reader.change_meta(KEY, 5)
    assert meta.draft is True
    assert meta.head_sha is meta.base_sha is meta.author_id is meta.author_login is meta.source_repo is None
    assert meta.author_is_bot is False


@pytest.mark.parametrize("source_id", ["1", True, False, 1.0, 0, -1, 10**18, [], {}])
def test_change_meta_invalid_source_project_cannot_fallback(make_reader, source_id):
    reader, _, _, _ = make_reader(mr={"source_project_id": source_id})
    with pytest.raises(GitLabHTTPError, match="Invalid GitLab merge request source project") as raised:
        reader.change_meta(KEY, 5)
    assert raised.value.reason == "http"
    assert review.fallback_eligible(raised.value) is False


@pytest.mark.parametrize("source_id", [1, 10**18 - 1])
def test_change_meta_valid_source_project_boundaries(make_reader, source_id):
    reader, _, _, _ = make_reader(mr={"source_project_id": source_id})
    assert reader.change_meta(KEY, 5).source_repo == RepoRef("gitlab", HOST, None, source_id)


def test_pr_meta_legacy_shape(make_reader):
    reader, _, _, _ = make_reader(mr={"title": "Fix it", "description": None,
                                     "source_branch": "feature", "author": {"username": "dev"}})
    assert reader.pr_meta(KEY, 5) == {
        "title": "Fix it", "body": "", "commits": [], "head_sha": HEAD,
        "head_committed_at": None, "author_association": None, "branch": "feature", "author": "dev",
    }


@pytest.mark.parametrize("data", [[], None, "bad", {"state": "unknown"}])
def test_bad_metadata_is_a_forge_http_error(make_reader, data):
    reader, _, _, _ = make_reader(routes={(MR, "1"): httpx.Response(200, json=data)})
    with pytest.raises(GitLabHTTPError) as raised:
        reader.change_meta(KEY, 5)
    assert raised.value.reason == "http"


def test_reader_resolves_shared_client_at_call_time(make_reader, monkeypatch):
    from argus.gitlab.reader import GitLabForge

    _, requests, _, client = make_reader()
    reader = GitLabForge()
    module = importlib.import_module("argus.gitlab.client")
    monkeypatch.setattr(module, "client", lambda: client)
    assert reader.head_sha(KEY, 5) == HEAD
    assert len(requests) == 1
    monkeypatch.setattr(module, "client", lambda: None)
    with pytest.raises(ForgeDenied, match="GitLab is not configured"):
        reader.head_sha(KEY, 5)


@pytest.mark.parametrize("method,args", [
    ("change_meta", (5,)), ("pr_meta", (5,)), ("head_sha", (5,)),
    ("diff_refs", (5,)), ("change_files", (5,)), ("read_file", ("a.py", HEAD)),
    ("house_rules", ()),
])
def test_reader_refuses_other_host_before_request(make_reader, method, args):
    reader, requests, _, _ = make_reader()
    with pytest.raises(ForgeDenied):
        getattr(reader, method)("gitlab:other.test#1", *args)
    assert requests == []


@pytest.mark.parametrize("authority", [HOST + ":8443", "[::1]:8443"])
def test_reader_matches_canonical_host_and_port(make_reader, monkeypatch, authority):
    monkeypatch.setenv("ARGUS_GITLAB_URL", f"https://{authority}")
    reader, _, _, _ = make_reader()
    assert reader.head_sha(f"gitlab:{authority}#1", 5) == HEAD


def test_reader_has_no_changes_route():
    from argus.gitlab import reader

    assert "/changes" not in Path(reader.__file__).read_text()


def test_forge_registration_and_capabilities(monkeypatch):
    from argus.gitlab.reader import GitLabForge, gitlab_forge

    selected = forge.forge_for(KEY)
    assert isinstance(selected, GitLabForge)
    assert selected is gitlab_forge
    assert selected.forge == "gitlab"
    for capability in ("supports_review_submit", "supports_auto_invite", "supports_outdated_flag",
                       "supports_html_markers", "supports_drafts"):
        assert getattr(selected, capability) is False
    sentinel = object()
    monkeypatch.setitem(forge._REGISTRY, "gitlab", sentinel)
    assert forge.forge_for(KEY) is sentinel


@pytest.mark.parametrize("url", ["", "http://invalid.test"])
def test_unconfigured_forge_is_denied(monkeypatch, url):
    monkeypatch.setenv("ARGUS_GITLAB_URL", url)
    with pytest.raises(ForgeDenied, match="gitlab forge is not enabled"):
        forge.forge_for(KEY)


def test_file_reader_runtime_replacement_and_loader(make_reader, monkeypatch):
    reader, _, _, _ = make_reader()
    sentinel = object()
    monkeypatch.setattr(review, "_file_reader", lambda repo, sha: sentinel)
    assert reader.file_reader(KEY, HEAD) is sentinel
    monkeypatch.setattr(review, "_file_reader", lambda repo, sha, loader: loader)
    def loader(path):
        return "content"

    assert reader.file_reader(KEY, HEAD, loader) is loader


def test_classify_uses_runtime_delegate(make_reader, monkeypatch):
    reader, _, _, _ = make_reader()
    error = GitLabHTTPError("failed")
    assert reader.classify(error) == "gitlab_http"
    monkeypatch.setattr(errors, "classify_error", lambda exc: "replacement")
    assert reader.classify(error) == "replacement"


def test_gitlab_http_errors_cannot_fallback():
    error = httpx.ConnectError("failed", request=httpx.Request("GET", f"https://{HOST}/api/v4/projects/1"))
    assert review.from_gitlab(error) is True
    assert review.fallback_eligible(error) is False
    assert review.fallback_eligible(GitLabHTTPError("failed")) is False
    assert review.fallback_eligible(ForgeError("failed")) is False
    assert review.fallback_eligible(RuntimeError("provider failed")) is True
    provider = httpx.ConnectError("failed", request=httpx.Request("POST", "https://provider.test/review"))
    assert review.fallback_eligible(provider) is True


@pytest.mark.parametrize("url", ["", "http://invalid.test"])
def test_from_gitlab_false_without_valid_configuration(monkeypatch, url):
    monkeypatch.setenv("ARGUS_GITLAB_URL", url)
    error = httpx.ConnectError("failed", request=httpx.Request("GET", f"https://{HOST}/"))
    assert review.from_gitlab(error) is False


def test_from_gitlab_false_without_http_request():
    assert review.from_gitlab(RuntimeError("failed")) is False
    assert review.from_gitlab(httpx.ConnectError("failed")) is False


def test_context_file_auth_error_is_not_swallowed():
    def read(path):
        raise GitLabAuthError("denied")

    files = [FilePatch("a.py", "added", "@@ -0,0 +1 @@\n+pass\n")]
    with pytest.raises(GitLabAuthError):
        context.render_review_context(files, read, 120000)


def test_context_symbols_auth_error_is_not_swallowed():
    def read(path):
        raise GitLabAuthError("denied")

    files = [FilePatch("a.py", "added", "@@ -0,0 +1 @@\n+def load_data(): pass\n")]
    with pytest.raises(GitLabAuthError):
        context.changed_symbols(files, read)


def test_context_mirror_auth_error_is_not_swallowed():
    class Source:
        def cochanged(self, path, limit):
            raise GitLabAuthError("denied")

    with pytest.raises(GitLabAuthError):
        context.context_blocks_preview([FilePatch("a.py", "modified", None)], None,
                                       120000, HeuristicEstimator(), Source(),
                                       pairing=False, cochange=True)


def test_change_files_auth_failure_cannot_fallback(make_reader, monkeypatch):
    from argus import repo_policy

    reader, requests, _, _ = make_reader(routes={(MR + "/diffs", "1"): httpx.Response(401)})
    monkeypatch.setitem(forge._REGISTRY, "gitlab", reader)
    monkeypatch.setattr(repo_policy, "validate_repo", lambda repo: repo)
    monkeypatch.setattr(review, "complete_tool", lambda *args, **kwargs: pytest.fail("Unexpected provider call"))
    with pytest.raises(GitLabAuthError) as raised:
        review.run_review(KEY, 5)
    assert review.fallback_eligible(raised.value) is False
    assert requests[-1].url.path == MR + "/diffs"
