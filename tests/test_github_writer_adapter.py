"""Preserve publishing arguments and runtime replacements of GitHub writers."""

from unittest.mock import Mock, sentinel

import pytest
from conftest import stub_github_client

from argus import diff, publish, publish_access, verdict
from argus.api import db, main
from argus.forge.github_adapter import GitHubForge, github_forge
from argus.review import ReviewResult


@pytest.mark.parametrize("method,module,attribute,args,kwargs", [
    ("publish", publish, "publish_review",
     ("owner/repo", 7, "a" * 40, 12, sentinel.document, sentinel.files),
     {"transport": sentinel.transport}),
    ("bot_identity", publish, "_bot_login", (), {}),
    ("bot_identity", publish, "_bot_login", (sentinel.transport,), {}),
    ("bot_identity", publish, "_bot_login", (), {"transport": sentinel.transport}),
    ("check_access", publish_access, "check_access", ("owner/repo",), {"fresh": True}),
])
def test_writers_resolve_module_attributes_at_call_time(monkeypatch, method, module, attribute, args, kwargs):
    adapter = GitHubForge()
    original = Mock(return_value=sentinel.original)
    replacement = Mock(return_value=sentinel.replacement)
    monkeypatch.setattr(module, attribute, original)
    assert getattr(adapter, method)(*args, **kwargs) is sentinel.original
    monkeypatch.setattr(module, attribute, replacement)
    assert getattr(adapter, method)(*args, **kwargs) is sentinel.replacement
    original.assert_called_once_with(*args, **kwargs)
    replacement.assert_called_once_with(*args, **kwargs)


def test_bot_identity_preserves_zero_argument_patch(monkeypatch):
    monkeypatch.setattr(publish, "_bot_login", lambda: "publisher")
    assert GitHubForge().bot_identity() == "publisher"


def test_completed_review_publishes_through_selected_forge(tmp_path, monkeypatch):
    path = str(tmp_path / "argus.db")
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "owner/repo")
    stub_github_client(monkeypatch)
    db.init_db(path)
    head = "a" * 40
    review_id = db.create_review(path, "owner/repo", 7, "test", head_sha=head)
    db.save_result(path, review_id, ReviewResult(raw_count=0, bugs=[], nits=[], head_sha=head))
    files = [diff.FilePatch("app.py", "modified", "@@ -1 +1 @@\n-old()\n+new()\n")]
    fetch = Mock(return_value=files)
    writer = Mock(return_value=publish.PublishOutcome("posted"))
    adapter = Mock(wraps=github_forge)
    select = Mock(return_value=adapter)
    monkeypatch.setattr(diff, "fetch_pr_files", fetch)
    monkeypatch.setattr(publish, "publish_review", writer)
    monkeypatch.setattr(main, "forge_for", select)
    document = verdict.document(db.get_review(path, review_id), [], unverified=[])

    main._publish_completed_review(path, review_id, "owner/repo", 7)

    select.assert_called_once_with("owner/repo")
    adapter.change_files.assert_called_once_with("owner/repo", 7)
    fetch.assert_called_once_with("owner/repo", 7)
    adapter.publish.assert_called_once_with("owner/repo", 7, head, review_id, document, files)
    writer.assert_called_once_with("owner/repo", 7, head, review_id, document, files)
    assert db.get_review(path, review_id)["publish_status"] == "posted"


def test_github_publishing_gate_rejects_gitlab_before_allowlist(monkeypatch):
    repo = "gitlab:gitlab.example.com#12"
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", repo)
    allowlist = Mock(side_effect=AssertionError("must skip before reading posting policy"))
    monkeypatch.setattr(publish, "repo_allowlist", allowlist)
    assert publish.enabled_for(repo) is False
    allowlist.assert_not_called()
