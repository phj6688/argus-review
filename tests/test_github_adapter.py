"""Preserve review call order and runtime replacements of GitHub readers."""

from unittest.mock import Mock

import pytest

from conftest import stub_github_client
from argus import diff, errors, forge, github, review
from argus.forge.errors import ForgeDenied
from argus import live
from argus.schema import Review


@pytest.fixture
def adapter():
    from argus.forge.github_adapter import GitHubForge

    return GitHubForge()


@pytest.mark.parametrize("method,module,attribute,args", [
    ("change_files", diff, "fetch_pr_files", ("owner/name", 7)),
    ("pr_meta", live, "_pr_meta", ("owner/name", 7)),
    ("head_sha", live, "_pr_head_sha", ("owner/name", 7)),
    ("read_file", live, "_fetch_file_at", ("owner/name", "src/app.py", "head")),
    ("house_rules", review, "fetch_house_rules", ("owner/name",)),
    ("file_reader", review, "_file_reader", ("owner/name", "head")),
    ("classify", errors, "classify_error", (RuntimeError("failure"),)),
])
def test_methods_resolve_module_attributes_at_call_time(
    adapter, monkeypatch, method, module, attribute, args,
):
    original_result = object()
    replacement_result = object()
    original = Mock(return_value=original_result)
    replacement = Mock(return_value=replacement_result)
    monkeypatch.setattr(module, attribute, original)
    assert getattr(adapter, method)(*args) is original_result
    monkeypatch.setattr(module, attribute, replacement)
    assert getattr(adapter, method)(*args) is replacement_result
    original.assert_called_once_with(*args)
    replacement.assert_called_once_with(*args)


def test_file_reader_keeps_two_argument_replacement_contract(adapter, monkeypatch):
    sentinel = object()
    monkeypatch.setattr(review, "_file_reader", lambda repo, head_sha: sentinel)
    assert adapter.file_reader("owner/name", "head") is sentinel


def test_plain_assignment_replacements_reach_existing_file_reader(adapter):
    original_file_at = live._fetch_file_at
    original_house_rules = review.fetch_house_rules
    read = review._file_reader("owner/name", "head")
    try:
        live._fetch_file_at = lambda repo, path, ref: f"{repo}:{path}@{ref}"
        review.fetch_house_rules = lambda repo: f"rules for {repo}"
        assert adapter.read_file("owner/name", "first.py", "head") == "owner/name:first.py@head"
        assert read("second.py") == "owner/name:second.py@head"
        assert adapter.house_rules("owner/name") == "rules for owner/name"
    finally:
        live._fetch_file_at = original_file_at
        review.fetch_house_rules = original_house_rules


def test_review_files_wrapper_keeps_diff_patch_point(monkeypatch):
    stub_github_client(monkeypatch)
    files = diff.FileList([diff.FilePatch("a.py", "modified", "@@ -0,0 +1 @@\n+pass\n")])
    files.unseen = True
    fetch = Mock(return_value=files)
    monkeypatch.setattr(diff, "fetch_pr_files", fetch)
    assert review.fetch_pr_files("owner/name", 7) is files
    fetch.assert_called_once_with("owner/name", 7)


@pytest.mark.parametrize("meta_head", ["head", None])
def test_run_review_preserves_reader_call_order(monkeypatch, meta_head):
    stub_github_client(monkeypatch)
    calls = []

    def files(repo, number):
        calls.append("files")
        return [diff.FilePatch("a.py", "modified", "@@ -1 +1 @@\n-old()\n+new()\n")]

    def meta(repo, number):
        calls.append("meta")
        return {"title": "", "body": "", "commits": [], "head_sha": meta_head}

    def head(repo, number):
        calls.append("head")
        return "head"

    def reader(repo, head_sha):
        calls.append("file reader")
        assert head_sha == "head"
        return lambda path: "new()\n"

    def house_rules(repo):
        calls.append("house rules")
        return "repository rules"

    def deep_review(prompt, rules, model, intent_section=None, pr_section=""):
        calls.append("model")
        assert rules == "repository rules"
        assert "new()" in prompt
        return Review()

    monkeypatch.setattr(review, "fetch_pr_files", files)
    monkeypatch.setattr(live, "_pr_meta", meta)
    monkeypatch.setattr(live, "_pr_head_sha", head)
    monkeypatch.setattr(review, "_file_reader", reader)
    monkeypatch.setattr(review, "fetch_house_rules", house_rules)
    monkeypatch.setattr(review, "_deep_review", deep_review)
    monkeypatch.setattr(review, "_intent_for_prompt", lambda *args: None)
    monkeypatch.setattr(review, "_suppress_known_fps", lambda repo, pr, findings: findings)
    monkeypatch.setattr(live, "context_source", lambda *args: None)
    monkeypatch.setattr(review, "_probe_lane", lambda *args: (
        [], [], {"lane": "probe", "status": "skipped"}))
    monkeypatch.setattr(review, "_ruff_lane", lambda *args: (
        [], {"lane": "ruff", "status": "skipped"}))
    monkeypatch.setattr(review, "_config_lane", lambda *args: (
        [], {"lane": "config", "status": "skipped"}))
    monkeypatch.setattr(review, "_judge_lane", lambda *args: {"lane": "judge", "status": "skipped"})

    result = review.run_review("owner/name", 7)

    expected = ["files", "meta"]
    if meta_head is None:
        expected.append("head")
    assert calls == expected + ["file reader", "house rules", "model"]
    assert result.head_sha == "head"
    assert result.raw_count == 0


def test_capabilities(adapter):
    assert adapter.forge == "github"
    assert adapter.supports_review_submit is True
    assert adapter.supports_auto_invite is True
    assert adapter.supports_outdated_flag is False
    assert adapter.supports_html_markers is False
    assert adapter.supports_drafts is False


def test_forge_for_returns_github_singleton():
    from argus.forge.github_adapter import GitHubForge

    selected = forge.forge_for("owner/name")
    assert isinstance(selected, GitHubForge)
    assert forge.forge_for("owner/name") is selected
    assert forge.forge_for("another/repo") is selected


@pytest.mark.parametrize("key,message", [
    ("gitlab:gitlab.example.com#12", "gitlab forge is not enabled"),
    ("bitbucket:x#1", "unknown forge"),
])
def test_forge_for_denies_unavailable_forges(key, message):
    with pytest.raises(ForgeDenied, match=message):
        forge.forge_for(key)


@pytest.mark.parametrize("key", ["", "owner", "owner/name/extra", "gitlab:gitlab.example.com#0"])
def test_forge_for_preserves_malformed_key_errors(key):
    from argus.forge.keys import parse_repo_key

    with pytest.raises(ValueError) as parsed:
        parse_repo_key(key)
    with pytest.raises(ValueError) as selected:
        forge.forge_for(key)
    assert str(selected.value) == str(parsed.value)


def test_forge_for_uses_registered_forge(monkeypatch):
    registered = object()
    monkeypatch.setitem(forge._REGISTRY, "gitlab", registered)
    assert forge.forge_for("gitlab:gitlab.example.com#12") is registered


def test_run_review_rejects_gitlab_before_http(monkeypatch):
    calls = []

    def unexpected_get(*args, **kwargs):
        calls.append(args)
        pytest.fail("GitLab input must be rejected before an HTTP request")

    stub_github_client(monkeypatch, get=unexpected_get)
    with pytest.raises(ValueError):
        review.run_review("gitlab:gitlab.example.com#12", 1)
    assert calls == []


def test_diff_refs_are_absent(adapter):
    assert adapter.diff_refs("owner/name", 7) is None


def test_classify_preserves_github_error_category(adapter):
    error = github.GitHubRateLimited(123.0)
    assert adapter.classify(error) == errors.classify_error(error) == "github_rate_limited"
