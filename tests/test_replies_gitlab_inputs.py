"""Exercise comparison consumers with GitLab inputs and local responses."""

import json
from unittest.mock import Mock

import httpx
import pytest

from argus import acceptance, client, github, replies
from argus.api import db
import test_acceptance as acceptance_fixtures
import test_gitlab_compare as compare_fixtures
from test_gitlab_compare import COMPARE, HEAD, KEY, MERGE_BASE, OLD, PROJECT, entry, fixture

configured = compare_fixtures.configured
make_compare = compare_fixtures.make_compare
database = acceptance_fixtures.database
NOW = "2026-11-04T00:00:00+00:00"
UNSURE = {"verdict": "unsure", "quote": ""}


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Unexpected GitHub or model request")

    monkeypatch.setattr(github, "get", fail)
    monkeypatch.setattr(client, "complete_tool", fail)


@pytest.fixture
def finding(database):
    fid = acceptance_fixtures.seed(database, repo=KEY)
    with db.connect(database) as conn:
        item = dict(conn.execute("SELECT * FROM published_findings WHERE id=?", (fid,)).fetchone())
    return item | {"description": "No validation", "evidence": "value", "model": "configured-reviewer"}


@pytest.fixture
def run():
    return {"started_at": NOW, "finished_at": None, "repos": 0,
            "prs_checked": 0, "prs_skipped_final": 0, "failures": 0,
            "stopped_by_rate_limit": False, "graphql_paused": False,
            "compares_not_ancestor": 0, "reconciled_found": 0, "reconciled_missing": 0,
            "bot": replies.counts(), "rechecks_attempted": 0, "writes_attempted": 0}


def test_acceptance_comparisons_use_gitlab(database, finding, run, make_compare):
    _, requests, _ = make_compare()
    comparisons = {}
    with db.connect(database) as conn:
        acceptance._comparisons(conn, KEY, 3, [finding], HEAD, comparisons, NOW, run)
    assert [request.url.path for request in requests] == [COMPARE, MERGE_BASE]
    assert comparisons[(KEY, OLD, HEAD)]["status"] == "ahead"
    assert acceptance_fixtures.rows(database, "finding_outcomes")[0]["value"] == "true"
    assert run["failures"] == run["compares_not_ancestor"] == 0


def test_checked_stores_gitlab_comparison(database, finding, run, make_compare, monkeypatch):
    _, requests, _ = make_compare()
    recheck = Mock(return_value=UNSURE)
    monkeypatch.setattr(replies, "recheck", recheck)
    with db.connect(database) as conn:
        result = replies._checked(conn, finding, HEAD, NOW, run)
    assert result == UNSURE | {"head_sha": HEAD}
    assert finding["comparison"]["status"] == "ahead"
    assert finding["comparison"]["files"][0]["filename"] == "app.py"
    recheck.assert_called_once_with(KEY, finding, HEAD)
    assert [request.url.path for request in requests] == [COMPARE, MERGE_BASE]
    assert acceptance_fixtures.rows(database, "bot_actions")[0]["value"] == "unsure"


def test_unchanged_stores_gitlab_comparison(finding, make_compare):
    _, requests, _ = make_compare()
    assert replies._unchanged(finding, HEAD) is False
    assert finding["comparison"]["status"] == "ahead"
    assert finding["comparison"]["files"][0]["filename"] == "app.py"
    assert [request.url.path for request in requests] == [COMPARE, MERGE_BASE]


@pytest.mark.parametrize("provider,status,count", [
    ("gitlab", "diverged", 1), ("github", "behind", 1),
    ("github", "diverged", 1), ("github", "ahead", 0),
])
def test_non_ancestor_counter_is_memoized(database, finding, run, make_compare, monkeypatch,
                                       provider, status, count):
    old = fixture("b14_old_head_readable")["id"]
    finding["head_sha"] = old
    repo = KEY if provider == "gitlab" else "owner/repo"
    finding["repo"] = repo
    _, requests, _ = make_compare(payload=fixture("b14_heads_False"),
                                   base=fixture("b14_heads_merge_base"))
    get = Mock(return_value=httpx.Response(200, json={"status": status, "files": []},
               request=httpx.Request("GET", "https://api.github.com/comparison")))
    if provider == "github":
        monkeypatch.setattr(github, "get", get)
    comparisons = {}
    with db.connect(database) as conn:
        acceptance._comparisons(conn, repo, 3, [finding], HEAD, comparisons, NOW, run)
        conn.execute("DELETE FROM finding_outcomes")
        acceptance._comparisons(conn, repo, 3, [finding], HEAD, comparisons, NOW, run)
    assert run["compares_not_ancestor"] == count
    assert run["failures"] == 0
    assert comparisons[(repo, old, HEAD)]["status"] == status
    if provider == "gitlab":
        assert [request.url.path for request in requests] == [COMPARE, MERGE_BASE]
    else:
        get.assert_called_once_with(f"/repos/{repo}/compare/{old}...{HEAD}")


@pytest.mark.parametrize("operation", ["recheck", "comparisons", "unchanged", "checked"])
def test_incomplete_comparisons_remain_unknown(database, finding, run, make_compare, operation):
    forge, requests, _ = make_compare(payload={"diffs": [entry()], "compare_timeout": True})
    comparison = forge.compare(KEY, OLD, HEAD)
    assert comparison["status"] == "ahead" and comparison["complete"] is False
    assert all(file["patch"] for file in comparison["files"])
    finding["comparison"] = comparison
    with db.connect(database) as conn:
        if operation == "recheck":
            assert replies.recheck(KEY, finding, HEAD) == UNSURE
        elif operation == "comparisons":
            acceptance._comparisons(conn, KEY, 3, [finding], HEAD,
                                    {(KEY, OLD, HEAD): comparison}, NOW, run)
            assert acceptance_fixtures.rows(database, "finding_outcomes")[0]["value"] == "unknown"
            assert run["failures"] == 0
        elif operation == "checked":
            assert replies._checked(conn, finding, HEAD, NOW, run) == UNSURE | {"head_sha": HEAD}
        else:
            finding["path"] = "unlisted.py"
            assert replies._unchanged(finding, HEAD) is False
    assert [request.url.path for request in requests] == [COMPARE, MERGE_BASE]


def test_complete_recheck_reads_gitlab_files_and_reaches_model(finding, make_compare, monkeypatch):
    raw = PROJECT + "/repository/files/app.py/raw"

    def file_raw(request):
        code = "return value\n" if request.url.params["ref"] == HEAD else "old_value\n"
        return httpx.Response(200, text=code * 60)

    forge, requests, _ = make_compare(routes={raw: file_raw})
    finding["comparison"] = forge.compare(KEY, OLD, HEAD)
    complete = Mock(return_value={"verdict": "fixed", "quote": "return value"})
    monkeypatch.setattr(client, "complete_tool", complete)
    assert replies.recheck(KEY, finding, HEAD) == {"verdict": "fixed", "quote": "return value"}
    assert [request.url.path for request in requests] == [COMPARE, MERGE_BASE, raw, raw]
    assert [request.url.params["ref"] for request in requests[2:]] == [HEAD, OLD]
    complete.assert_called_once()
    model, _, prompt, spec = complete.call_args.args
    assert model == "configured-reviewer" and spec is replies._CHECK
    assert json.loads(prompt)["current_range"] == [10, 12]


def test_gitlab_read_file_missing_returns_none(make_compare):
    raw = PROJECT + "/repository/files/app.py/raw"
    _, requests, _ = make_compare(routes={raw: httpx.Response(404)})
    assert replies._read_file(KEY, "app.py", HEAD) is None
    assert [request.url.path for request in requests] == [raw]


def test_gitlab_b13_insertion_shifts_anchor_without_changing_region(finding, make_compare):
    patch = "@@ -1,3 +1,5 @@\n first\n+inserted_one\n+inserted_two\n second\n third\n"
    forge, _, _ = make_compare(payload={"diffs": [entry(diff=patch)]})
    comparison = forge.compare(KEY, OLD, HEAD)
    assert acceptance.region_changed(finding, comparison["files"]) is False
    assert replies._anchor(finding, comparison["files"][0]) == (12, 14)


def test_gitlab_b13_edit_changes_finding_region(finding, make_compare):
    forge, _, _ = make_compare()
    comparison = forge.compare(KEY, OLD, HEAD)
    assert acceptance.region_changed(finding, comparison["files"]) is True
    assert replies._anchor(finding, comparison["files"][0]) == (10, 12)


def test_identical_gitlab_comparison_stays_unchanged(finding):
    finding["comparison"] = {"status": "identical", "files": [], "complete": True}
    assert replies._unchanged(finding, HEAD) is True
