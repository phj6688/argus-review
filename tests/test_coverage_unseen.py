"""A listing that never delivered some files must not report a full read."""

import pytest

from argus import review as review_mod
from argus import verdict
from argus.diff import FileList, FilePatch, parse_unified_diff
from argus.schema import Review
from argus.review import _llm_file_counts, run_diff_review

PATCH = "@@ -1,2 +1,3 @@\n import os\n+print(1)\n x = 1\n"


def _file(name="src/app.py", patch=PATCH):
    return FilePatch(filename=name, status="modified", patch=patch)


def _rendered(files):
    from argus.context import render_review_context
    from argus.tokens import HeuristicEstimator
    return render_review_context(files, None, 120_000, HeuristicEstimator())


def _ran_lane(files):
    return {"lane": verdict.LANE_LLM, "status": verdict.LANE_RAN,
            **_llm_file_counts(files, _rendered(files))}


@pytest.mark.parametrize("omit", [False, True])
def test_unseen_listing_is_partial_and_leaves_files_omitted_alone(omit):
    files = FileList([_file(), _file("src/other.py", patch=None)] if omit else [_file()], unseen=True)
    lane = _ran_lane(files)
    assert lane["files_unseen"] is True
    assert lane["files_omitted"] == (1 if omit else 0)
    assert verdict.coverage_level([lane]) == verdict.COVERAGE_PARTIAL


@pytest.mark.parametrize("files", [FileList([_file()]), [_file()],
                                   FileList([_file(), _file("b.py", patch=None)])])
def test_seen_listing_keeps_the_lane_dict_and_level(files):
    lane = _ran_lane(files)
    assert "files_unseen" not in lane
    expected = verdict.COVERAGE_PARTIAL if lane["files_omitted"] else verdict.COVERAGE_FULL
    assert verdict.coverage_level([lane]) == expected


def test_unsafe_section_in_a_diff_marks_the_lane_unseen(monkeypatch):
    text = ("diff --git a/ok.py b/ok.py\n--- a/ok.py\n+++ b/ok.py\n" + PATCH +
            "diff --git a/../escape.py b/../escape.py\n--- a/../escape.py\n+++ b/../escape.py\n" + PATCH)
    files = parse_unified_diff(text)
    assert files.unseen is True
    monkeypatch.setattr(review_mod, "_deep_review", lambda *a, **k: Review(findings=[]))
    result = run_diff_review(files, repo=None)
    lane = next(rec for rec in result.coverage if rec["lane"] == verdict.LANE_LLM)
    assert lane["status"] == verdict.LANE_RAN and lane["files_unseen"] is True
    assert verdict.coverage_level(result.coverage) == verdict.COVERAGE_PARTIAL


def test_skipped_lane_does_not_lift_or_lower_the_level():
    lane = {"lane": verdict.LANE_LLM, "status": verdict.LANE_SKIPPED, "files_unseen": True}
    assert verdict.coverage_level([lane]) == verdict.COVERAGE_NONE


def test_unseen_flag_must_be_exactly_true():
    loose = FileList([_file()])
    loose.unseen = "yes"
    assert "files_unseen" not in _llm_file_counts(loose, _rendered(loose))
