"""Behaviour tests for the Jarvis intent adapter (TASK recovery + prompt slot)."""

import pytest

from argus.jarvis import intent
from argus.jarvis.intent import PullRequest


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setenv("ARGUS_LINEAR_REPOS", " example/widgets , EXAMPLE/argus ")
    monkeypatch.setenv("ARGUS_LINEAR_KEY_PREFIX", "TASK")


def _pr(repo: str, title: str = "", body: str = "", commits=None) -> PullRequest:
    return PullRequest(repo=repo, title=title, body=body,
                       commit_messages=commits or [], author_association="OWNER")


def test_extract_finds_ids_across_title_body_commits_deduped_ordered():
    ids = intent.extract_issue_ids("fix TASK-12", "see TASK-3 and TASK-12", ["TASK-3 wip"])
    assert ids == ["TASK-12", "TASK-3"]


def test_extract_is_case_insensitive_and_normalises_upper():
    assert intent.extract_issue_ids("done task-45") == ["TASK-45"]


def test_extract_returns_empty_when_no_id():
    assert intent.extract_issue_ids("just a chore", "", []) == []


def test_non_adopting_repo_is_intent_unknown_even_with_an_id():
    result = intent.recover(_pr("example/echo", title="TASK-99 thing"))
    assert result.status == "intent_unknown"
    assert result.issue_ids == []
    assert "does not file" in result.reason.lower() or "not in scope" in result.reason.lower()


def test_adopting_repo_with_id_is_recovered():
    result = intent.recover(_pr("example/widgets", body="implements TASK-230"))
    assert result.status == "recovered"
    assert result.issue_ids == ["TASK-230"]


def test_adopting_repo_without_id_is_intent_unknown():
    result = intent.recover(_pr("example/widgets", title="chore: bump deps"))
    assert result.status == "intent_unknown"
    assert "no linear issue" in result.reason.lower()


@pytest.mark.parametrize("repo,status", [
    ("widgets", "intent_unknown"), ("other/widgets", "intent_unknown"),
    ("EXAMPLE/Widgets", "recovered"),
])
def test_only_full_repo_matches(repo, status):
    assert intent.recover(_pr(repo, title="TASK-1")).status == status


def test_intent_slot_recovered_carries_issue_text():
    result = intent.recover(_pr("example/argus", title="TASK-461 engine"))
    issue = {"identifier": "TASK-461", "title": "Fix the engine token",
             "description": "Problem: token missing.\nAcceptance: review passes."}
    slot = intent.intent_slot(result, issue)
    assert "TASK-461" in slot
    assert "Fix the engine token" in slot
    assert "Acceptance: review passes." in slot


def test_intent_slot_unknown_is_explicit_and_warns_not_to_guess():
    result = intent.recover(_pr("example/echo", title="random"))
    slot = intent.intent_slot(result, None)
    assert "intent unknown" in slot.lower()
    assert "do not" in slot.lower() or "don't" in slot.lower()


def test_intent_slot_recovered_but_unfetched_names_id_without_claiming_unknown():
    # A real id was recovered but the Linear fetch returned nothing. The slot must
    # name the id and must NOT contradict itself by also claiming intent unknown.
    result = intent.recover(_pr("example/widgets", title="TASK-234 filtering"))
    slot = intent.intent_slot(result, None)
    assert "TASK-234" in slot
    assert "intent unknown" not in slot.lower()
    assert "do not" in slot.lower() or "could not" in slot.lower()


@pytest.mark.parametrize("text,expected", [
    ("X.TASK-1", ["X.TASK-1"]), ("XzTASK-1", []),
    ("OTHERX.TASK-1", []), ("X.TASK-1suffix", []),
    ("see x.task-123 and X.TASK-3", ["X.TASK-123", "X.TASK-3"]),
])
def test_prefix_is_literal_and_matches_whole_issue_keys(monkeypatch, text, expected):
    monkeypatch.setenv("ARGUS_LINEAR_KEY_PREFIX", "X.TASK")
    assert intent.extract_issue_ids(text) == expected


@pytest.mark.parametrize("association", [None, "NONE", "UNKNOWN", "CONTRIBUTOR"])
def test_untrusted_author_cannot_recover_intent(association):
    pr = PullRequest("example/argus", title="TASK-1", author_association=association)
    assert intent.recover(pr).status == "intent_unknown"


@pytest.mark.parametrize("prefix", ["", "   "])
def test_empty_prefix_disables_extraction(monkeypatch, prefix):
    monkeypatch.setenv("ARGUS_LINEAR_KEY_PREFIX", prefix)
    assert intent.extract_issue_ids("TASK-123") == []
