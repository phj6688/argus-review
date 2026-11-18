"""Capture one discovery tick followed by one serial review worker."""

import asyncio

from argus.api import db, watch_store, watcher

from .harness import register_review


def test_watcher_poll_once(golden, transport, provider, db_path, frozen_now, monkeypatch):
    monkeypatch.setenv("ARGUS_WATCH_OWNERS", "example")
    monkeypatch.setenv("ARGUS_REVIEW_WORKERS", "1")
    monkeypatch.setenv("ARGUS_OUTCOME_INTERVAL_TICKS", "100")
    db.save_config(db_path, repos=[], model="model-a", bug_cap=5, min_confidence=0.55)
    transport.add("GET", "/user/repos", (200, [{
        "full_name": "example/app", "pushed_at": frozen_now.isoformat(), "archived": False,
    }], {}), query={"affiliation": "owner", "per_page": "100", "sort": "full_name"})
    transport.add("GET", "/repos/example/app/pulls", (200, [
        {
            "number": 7, "head": {"sha": "a" * 40}, "base": {"ref": "main"},
            "user": {"login": "maintainer", "type": "User"},
            "author_association": "MEMBER", "draft": False,
            "updated_at": frozen_now.isoformat(),
        },
        {
            "number": 8, "head": {"sha": "b" * 40}, "base": {"ref": "main"},
            "user": {"login": "contributor", "type": "User"},
            "author_association": "NONE", "draft": False,
            "updated_at": frozen_now.isoformat(),
        },
    ], {}), query={"state": "open", "per_page": "100"})
    register_review(transport, provider)
    with asyncio.Runner() as runner:
        queued = runner.run(watcher.poll_once(db_path))
    assert queued == 1
    assert [head["status"] for head in watch_store.heads(db_path)] == ["queued", "skipped"]
    golden.check("watcher", "poll-listed-heads", {"queued": queued})

    processed = watcher.process_one(db_path)
    idle = watcher.process_one(db_path)
    assert processed is True
    assert idle is False
    assert [head["status"] for head in watch_store.heads(db_path)] == ["done", "skipped"]
    assert db.list_reviews(db_path)[0]["status"] == "done"
    transport.assert_all_used()
    golden.check(
        "watcher", "watcher_poll_once", {"queued": queued, "processed": processed, "idle": idle}
    )
