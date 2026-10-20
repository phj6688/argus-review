"""Outcome work stays outside discovery ticks and drains during shutdown."""

import asyncio
import threading
import time

import pytest

from argus import acceptance, github
from argus.api import db, watcher


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "argus.db")
    db.init_db(path)
    monkeypatch.setenv("ARGUS_OUTCOME_INTERVAL_TICKS", "1")
    monkeypatch.setattr(watcher, "_ticks", {})
    monkeypatch.setattr(watcher, "_outcome_tasks", {}, raising=False)
    monkeypatch.setattr(watcher, "quota_paused", lambda: False)
    monkeypatch.setattr(watcher, "discover_repos", lambda path: None)
    monkeypatch.setattr(watcher, "_watched_repos", lambda path: [])
    return path


def test_tick_returns_and_skips_overlapping_collection(database, monkeypatch):
    release = threading.Event()
    calls = []
    async def scenario():
        started = asyncio.Event()
        loop = asyncio.get_running_loop()
        def collect(path):
            calls.append(path)
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
        monkeypatch.setattr(acceptance, "collect", collect)
        try:
            assert await asyncio.wait_for(watcher.poll_once(database), 1) == 0
            await asyncio.wait_for(started.wait(), 1)
            assert watcher._last_tick[database]["last_tick_duration_s"] < 1
            assert await asyncio.wait_for(watcher.poll_once(database), 1) == 0
            assert calls == [database]
            assert not watcher._outcome_tasks[database].done()
        finally:
            release.set()
            if database in watcher._outcome_tasks:
                await watcher._outcome_tasks[database]
    asyncio.run(scenario())


@pytest.mark.parametrize("limited", [False, True])
def test_collection_logs_its_own_failure(database, monkeypatch, caplog, limited):
    caplog.set_level("INFO", logger="argus.watcher")
    def collect(path):
        if limited:
            raise github.GitHubRateLimited(time.time() + 60)
        raise RuntimeError("failed collection")
    monkeypatch.setattr(acceptance, "collect", collect)
    async def scenario():
        await watcher.poll_once(database)
        await watcher._outcome_tasks[database]
    asyncio.run(scenario())
    records = [r for r in caplog.records if "outcome collection" in r.message]
    assert len(records) == 1
    assert records[0].levelname == ("INFO" if limited else "WARNING")
    assert bool(records[0].exc_info) is not limited


def test_shutdown_cancels_and_awaits_collection(database, monkeypatch):
    release = threading.Event()
    finished = threading.Event()
    async def scenario():
        started = asyncio.Event()
        loop = asyncio.get_running_loop()
        def collect(path):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
            finished.set()
        monkeypatch.setattr(acceptance, "collect", collect)
        runner = asyncio.create_task(watcher.run_forever(database))
        try:
            await asyncio.wait_for(started.wait(), 1)
            runner.cancel()
            await asyncio.sleep(0.02)
            assert not runner.done()
            task = watcher._outcome_tasks[database]
            assert task.cancelling()
        finally:
            release.set()
            if not runner.cancelling():
                runner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(runner, 2)
        assert finished.is_set()
        assert task.done()
        assert database not in watcher._outcome_tasks
    asyncio.run(scenario())
