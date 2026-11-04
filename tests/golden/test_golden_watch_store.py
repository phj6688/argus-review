"""Snapshot durable queue changes through the production store."""

from datetime import timedelta

import pytest

from argus.api import db, watch_store
from argus.github import GitHubRateLimited
from argus.review import ReviewResult


def _pr(number=7, sha="a" * 40, **extra):
    return {
        "number": number,
        "head": {"sha": sha},
        "base": {"ref": "main"},
        "user": {"login": "maintainer", "type": "User"},
        "author_association": "MEMBER",
        "draft": False,
        **extra,
    }


def test_record_listing(golden, db_path, frozen_now):
    first = _pr(updated_at=(frozen_now - timedelta(minutes=1)).isoformat())
    second = _pr(8, "b" * 40, updated_at=frozen_now.isoformat())
    added = watch_store.record_listing(db_path, "example/app", [first, second], frozen_now)
    assert added == 2
    golden.check("watch-store", "listing-new", {"queued": added})

    replacement = _pr(sha="c" * 40, updated_at=frozen_now.isoformat())
    added = watch_store.record_listing(
        db_path, "example/app", [replacement, second], frozen_now + timedelta(minutes=1)
    )
    assert added == 1
    golden.check("watch-store", "listing-head-updated", {"queued": added})

    added = watch_store.record_listing(
        db_path, "example/app", [replacement], frozen_now + timedelta(minutes=2)
    )
    assert added == 0
    assert [row["status"] for row in watch_store.heads(db_path)] == [
        "superseded", "closed", "queued"
    ]
    golden.check("watch-store", "listing-closed", {"queued": added})


def test_fast_merges_and_requeue(golden, db_path, frozen_now):
    merged = _pr(
        merged_at=(frozen_now - timedelta(minutes=2)).isoformat(),
        updated_at=(frozen_now - timedelta(minutes=3)).isoformat(),
    )
    rejected = [
        _pr(8, "b" * 40, merged_at=None),
        _pr(9, "c" * 40, merged_at=(frozen_now - timedelta(minutes=61)).isoformat()),
        _pr(10, "d" * 40, merged_at=frozen_now.isoformat(), author_association="NONE"),
        _pr(11, "invalid", merged_at=frozen_now.isoformat()),
    ]
    queued = watch_store.record_fast_merges(db_path, "example/app", [merged, *rejected], frozen_now)
    duplicate = watch_store.record_fast_merges(db_path, "example/app", [merged], frozen_now)
    assert (queued, duplicate) == (1, 0)
    golden.check("watch-store", "record-fast-merges", {"queued": queued, "duplicate": duplicate})

    watch_store.restrict_repos(db_path, [], frozen_now)
    assert watch_store.heads(db_path)[0]["status"] == "closed"
    golden.check("watch-store", "restrict-fast-merges", None)

    queued = watch_store.requeue_fast_merges(db_path, ["example/app"], frozen_now)
    duplicate = watch_store.requeue_fast_merges(db_path, ["example/app"], frozen_now)
    assert (queued, duplicate) == (1, 0)
    golden.check("watch-store", "requeue-fast-merges", {"queued": queued, "duplicate": duplicate})


def test_restrict_repos(golden, db_path, frozen_now):
    for repo in ("example/app", "acme-org/service"):
        watch_store.record_listing(db_path, repo, [_pr()], frozen_now)
    result = watch_store.restrict_repos(db_path, ["example/app"], frozen_now)
    assert [row["status"] for row in watch_store.heads(db_path)] == ["queued", "closed"]
    golden.check("watch-store", "restrict-repos", result)


def test_claim_trust_policy(golden, db_path, frozen_now, monkeypatch):
    monkeypatch.setenv("ARGUS_WATCH_TRUSTED_ONLY", "0")
    watch_store.record_listing(
        db_path, "example/app", [_pr(7, author_association="NONE"), _pr(8)], frozen_now
    )
    monkeypatch.setenv("ARGUS_WATCH_TRUSTED_ONLY", "1")
    claimed = watch_store.claim(db_path, frozen_now)
    assert claimed["pr"] == 8
    assert watch_store.heads(db_path)[0]["status"] == "skipped"
    assert watch_store.claim(db_path, frozen_now) is None
    golden.check("watch-store", "claim-trusted-and-untrusted", {"claimed": claimed, "next": None})


@pytest.mark.parametrize("case", ["finish-completed", "finish-rate-limited"])
def test_finish(golden, db_path, frozen_now, case):
    watch_store.record_listing(db_path, "example/app", [_pr()], frozen_now)
    claimed = watch_store.claim(db_path, frozen_now)
    review_id = db.create_review(db_path, "example/app", 7, "model-a", head_sha="a" * 40)
    error = None
    if case == "finish-rate-limited":
        reset = frozen_now + timedelta(minutes=5)
        error = GitHubRateLimited(reset.timestamp())
        db.set_error(db_path, review_id, str(error), type(error).__name__)
    else:
        db.save_result(db_path, review_id, ReviewResult(raw_count=0, head_sha="a" * 40))
    finished = watch_store.finish(
        db_path, claimed, review_id, error=error, now=frozen_now + timedelta(seconds=30)
    )
    after = watch_store.heads(db_path)[0]
    assert after["attempts"] == 0
    assert after["status"] == ("queued" if error else "done")
    golden.check("watch-store", case, {"finished": finished, "review_id": review_id})
