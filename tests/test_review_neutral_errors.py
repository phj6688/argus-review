"""Forge failures must escape recovery intended for optional review context."""

import pytest

from conftest import stub_github_client
from argus import github, review
from argus.errors import HeadMoved
from argus.forge.errors import ForgeAuthError, ForgeDenied, ForgeRateLimited
from argus.jarvis import live
from argus.schema import Finding, Severity


class NeutralAuth(ForgeAuthError):
    pass


class NeutralDenied(ForgeDenied):
    pass


class NeutralRate(ForgeRateLimited):
    pass


@pytest.fixture(params=[NeutralAuth, NeutralDenied, NeutralRate])
def neutral_error(request):
    if request.param is NeutralRate:
        return NeutralRate(123.0, "test")
    return request.param("test")


def test_neutral_errors_are_ineligible_for_model_fallback(neutral_error):
    assert review.fallback_eligible(neutral_error) is False


@pytest.mark.parametrize("error", [
    github.GitHubAuthError("test"), github.GitHubDenied("test"),
    github.GitHubRateLimited(123.0), HeadMoved("changed"),
])
def test_existing_failures_stay_ineligible_for_model_fallback(error):
    assert review.fallback_eligible(error) is False


def test_plain_runtime_error_stays_eligible_for_model_fallback():
    assert review.fallback_eligible(RuntimeError("x")) is True


def test_run_review_reraises_neutral_metadata_failure(monkeypatch, neutral_error):
    stub_github_client(monkeypatch)
    monkeypatch.setattr(review, "fetch_pr_files", lambda repo, pr: [])

    def failed_meta(repo, pr):
        raise neutral_error

    def unexpected_head(*args):
        pytest.fail("Review continued after a forge metadata failure")

    monkeypatch.setattr(live, "_pr_meta", failed_meta)
    monkeypatch.setattr(review, "_head_sha_for", unexpected_head)
    with pytest.raises(type(neutral_error)) as raised:
        review.run_review("owner/name", 7)
    assert raised.value is neutral_error


def test_head_sha_reraises_neutral_failure(monkeypatch, neutral_error):
    def failed_head(repo, pr):
        raise neutral_error

    monkeypatch.setattr(live, "_pr_head_sha", failed_head)
    with pytest.raises(type(neutral_error)) as raised:
        review._head_sha_for("owner/name", 7)
    assert raised.value is neutral_error


def test_head_sha_still_tolerates_plain_runtime_error(monkeypatch):
    def failed_head(repo, pr):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(live, "_pr_head_sha", failed_head)
    assert review._head_sha_for("owner/name", 7) is None


def test_reanchor_reraises_neutral_file_read_failure(monkeypatch, neutral_error):
    def failed_read(repo, path, ref):
        raise neutral_error

    monkeypatch.setattr(live, "_fetch_file_at", failed_read)
    read = review._file_reader("owner/name", "head")
    finding = Finding(
        file="a.py", line=2, severity=Severity.major, confidence=0.8,
        title="Missing timeout", description="The request can hang.",
        proposed_fix="Add a timeout.", evidence="response = client.get(url)",
    )
    with pytest.raises(type(neutral_error)) as raised:
        review._reanchor_findings([finding], read)
    assert raised.value is neutral_error
