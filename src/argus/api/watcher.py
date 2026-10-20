"""Fixed-cadence discovery with a durable queue and bounded review workers."""

import asyncio
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .. import github
from ..client import DEFAULT_MODEL, LLM_MAX_CONCURRENCY, llm_in_flight
from ..repo_policy import env_int, hard_deny_owners, validate_repo, watch_owners
from ..review import DEFAULT_BUG_CAP
from . import db, watch_config, watch_store

log = logging.getLogger("argus.watcher")
ENV_ENABLED = "ARGUS_WATCH_ENABLED"
ENV_INTERVAL = "ARGUS_WATCH_INTERVAL"
ENV_AUTODISCOVER = "ARGUS_AUTODISCOVER"
DEFAULT_INTERVAL_SECONDS = 60
_ticks = {}
_last_tick = {}
_repo_pushed_at = {}
_previous_watched_repos = {}
_watched_repos_lock = threading.Lock()
_outcome_tasks = {}
_pause_logged = 0
_active_lock = threading.Lock()
_active_heads = set()


def is_enabled():
    return watch_store.truthy(ENV_ENABLED)


def poll_interval():
    return env_int(ENV_INTERVAL, DEFAULT_INTERVAL_SECONDS, 30)


def worker_count():
    return env_int("ARGUS_REVIEW_WORKERS", 2, 1, 8)


def autodiscover_enabled():
    return watch_store.truthy(ENV_AUTODISCOVER)


def discover_repos(path):
    stored = db.get_config(path) or {}
    has_scope = watch_owners() or any(r.get("watched") is True for r in stored.get("repos", []))
    if is_enabled() and not has_scope:
        log.warning("watcher: ARGUS_WATCH_OWNERS is empty and no explicit repos are configured; watching nothing")
    records = github.owned_repo_records() if has_scope else []
    with watch_store.connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        config = db.get_config(path) or {
            "repos": [],
            "model": DEFAULT_MODEL,
            "bug_cap": DEFAULT_BUG_CAP,
            "min_confidence": 0.55,
            "watch_mode": "auto",
            "deny": [],
        }
        explicit = {r["name"].casefold() for r in config["repos"] if r.get("watched") is True}
        records = [
            r
            for r in records
            if watch_config.allowed(r.get("full_name", ""), config.get("deny", []),
                                    explicit=r.get("full_name", "").casefold() in explicit)
        ]
        known = {r["name"].casefold() for r in config["repos"]}
        new = [r["full_name"] for r in records if r["full_name"].casefold() not in known]
        mode = config.get("watch_mode", "auto")
        add = mode == "auto" or autodiscover_enabled()
        repos = config["repos"] + (
            [{"name": n, "watched": None if mode == "auto" else False} for n in new] if add else []
        )
        conn.execute(
            """INSERT INTO config
            (id,repo_allowlist,model,bug_cap,min_confidence,watch_mode,deny,owned_repos)
            VALUES(1,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            repo_allowlist=excluded.repo_allowlist, owned_repos=excluded.owned_repos""",
            (
                json.dumps(repos),
                config["model"],
                config["bug_cap"],
                config["min_confidence"],
                mode,
                json.dumps(config.get("deny", [])),
                json.dumps(records),
            ),
        )
    if new and add:
        log.info("watcher: catalogued %d repos", len(new))
    return len(new) if add else 0


def _watched_repos(path):
    return watch_config.effective(db.get_config(path))


def _open_prs(repo):
    validate_repo(repo)
    return github.list_cached(f"/repos/{repo}/pulls", {"state": "open", "per_page": 100})


def _recently_closed_prs(repo):
    validate_repo(repo)
    prs, _ = github.get_json_cached(
        f"/repos/{repo}/pulls",
        {"state": "closed", "sort": "updated", "direction": "desc", "per_page": 20, "page": 1},
    )
    return prs


def _advanced_repos(path, repos):
    key = str(Path(path).resolve())
    config = db.get_config(path) or {}
    watched = {repo.casefold() for repo in repos}
    current = {}
    for record in config.get("owned_repos", []):
        if not record.get("pushed_at"):
            continue
        try:
            pushed = db._parse_created_at(record["pushed_at"])
        except (ValueError, TypeError, AttributeError):
            continue
        current[record["full_name"].casefold()] = pushed
    previous = _repo_pushed_at.get(key)
    if previous is None:
        _repo_pushed_at[key] = current
        return {}
    advanced = {
        repo: pushed for repo, pushed in current.items()
        if repo in watched and repo in previous and pushed > previous[repo]
    }
    # A watched advance stays pending until its closed listing is stored.
    _repo_pushed_at[key] = {
        repo: previous[repo] if repo in advanced else pushed for repo, pushed in current.items()
    }
    return advanced


def trigger_review(path, repo, pr, head_sha, *, head=None):
    from . import main

    validate_repo(repo, (db.get_config(path) or {}).get("deny", []))
    stored = db.get_config(path)
    model = stored["model"] if stored else DEFAULT_MODEL
    bug_cap = stored["bug_cap"] if stored else DEFAULT_BUG_CAP
    review_id = db.create_review(path, repo, pr, model, head_sha=head_sha)
    if head:
        with watch_store.connection(path) as conn:
            linked = conn.execute(
                "UPDATE pr_heads SET review_id=? WHERE id=? AND status='running' AND claimed_at=?",
                (review_id, head["id"], head["claimed_at"]),
            ).rowcount
        if not linked:
            db.set_error(path, review_id, "claim expired", "InterruptedError")
            return review_id, InterruptedError("claim expired")
    fb_runner, fb_model = main._fallback_for(
        model, lambda m: lambda: main.run_review(repo, pr, m, bug_cap, expected_head_sha=head_sha)
    )
    error = main.run_one_review(
        path,
        review_id,
        repo,
        pr,
        lambda: main.run_review(repo, pr, model, bug_cap, expected_head_sha=head_sha),
        fallback_runner=fb_runner,
        primary_model=model,
        fallback_model=fb_model,
    )
    return review_id, error


def quota_paused():
    global _pause_logged
    reset = github.pause_until()
    if reset > time.time():
        if reset != _pause_logged:
            remaining = github.quota_snapshot().get("core", {}).get("remaining", "?")
            log.info("watcher: quota pause remaining=%s reset=%s", remaining, reset)
            _pause_logged = reset
        return True
    _pause_logged = 0
    return False


async def _collect_outcomes(path):
    from .. import acceptance

    def collect():
        try:
            acceptance.collect(path)
        except github.GitHubRateLimited as error:
            log.info("watcher: outcome collection paused until %s", error.reset_at)
        except Exception:
            log.warning("watcher: outcome collection failed", exc_info=True)

    work = asyncio.create_task(asyncio.to_thread(collect))
    try:
        await asyncio.shield(work)
    except asyncio.CancelledError:
        # Cancelling an await cannot stop its thread; retain ownership until it exits.
        await work
        raise


async def poll_once(path=None):
    from . import watch_metrics

    path = path or db.db_path()
    started = time.monotonic()
    cached = github.not_modified_count()
    polled = seen = queued = 0
    try:
        for name, step in (("reclaim", watch_store.reclaim), ("daily report", watch_metrics.daily_report)):
            try:
                await asyncio.to_thread(step, path)
            except Exception:
                log.warning("watcher: %s failed", name, exc_info=True)
        if quota_paused():
            return 0
        tick = _ticks.get(path, 0)
        discovered = False
        if tick % env_int("ARGUS_WATCH_DISCOVERY_TICKS", 1) == 0:
            try:
                await asyncio.to_thread(discover_repos, path)
                discovered = True
            except Exception:
                log.warning("watcher: discovery failed; using cached repos", exc_info=True)
        _ticks[path] = tick + 1
        repos = await asyncio.to_thread(_watched_repos, path)
        await asyncio.to_thread(watch_store.restrict_repos, path, repos)
        key = str(Path(path).resolve())
        with _watched_repos_lock:
            previous = _previous_watched_repos.get(key)
            # Startup must not restore heads closed by an earlier process.
            reentered = (
                [repo for repo in repos if repo.casefold() not in previous]
                if previous is not None else []
            )
            # Publish before awaiting so worker invalidations cannot be overwritten.
            _previous_watched_repos[key] = {repo.casefold() for repo in repos}
        fast_merges = os.getenv("ARGUS_WATCH_FAST_MERGES", "1") != "0"
        if fast_merges and reentered:
            try:
                queued += await asyncio.to_thread(watch_store.requeue_fast_merges, path, reentered)
            except Exception:
                log.warning("watcher: fast merge requeue failed", exc_info=True)
                with _watched_repos_lock:
                    _previous_watched_repos[key].difference_update(
                        repo.casefold() for repo in reentered
                    )
        advanced = {}
        if fast_merges and (discovered or key in _repo_pushed_at):
            try:
                advanced = await asyncio.to_thread(_advanced_repos, path, repos)
            except Exception:
                log.warning("watcher: fast merge detection failed", exc_info=True)
        for repo in repos:
            if quota_paused():
                break
            try:
                prs = await asyncio.to_thread(_open_prs, repo)
                queued += await asyncio.to_thread(watch_store.record_listing, path, repo, prs)
                seen += len(prs)
                polled += 1
            except Exception:
                log.warning("watcher: tick failed for %s", repo, exc_info=True)
            if repo.casefold() in advanced:
                try:
                    prs = await asyncio.to_thread(_recently_closed_prs, repo)
                    queued += await asyncio.to_thread(watch_store.record_fast_merges, path, repo, prs)
                    _repo_pushed_at[key][repo.casefold()] = advanced[repo.casefold()]
                except Exception:
                    log.warning("watcher: fast merge listing failed for %s", repo, exc_info=True)
        if (tick + 1) % env_int("ARGUS_OUTCOME_INTERVAL_TICKS", 10, 1) == 0:
            key = str(Path(path).resolve())
            task = _outcome_tasks.get(key)
            if task is None or task.done():
                _outcome_tasks[key] = asyncio.create_task(_collect_outcomes(path))
        return queued
    except Exception:
        log.error("watcher: tick failed", exc_info=True)
        return queued
    finally:
        elapsed = time.monotonic() - started
        _last_tick[path] = {
            "last_tick_at": datetime.now(timezone.utc).isoformat(),
            "last_tick_duration_s": elapsed,
        }
        try:
            depth = (await asyncio.to_thread(watch_store.queue_sizes, path))["queued"]
        except Exception:
            depth = -1
        log.info(
            "watcher: repos=%d 304=%d heads=%d queued=%d queue_depth=%d duration=%.3fs",
            polled,
            github.not_modified_count() - cached,
            seen,
            queued,
            depth,
            elapsed,
        )


def process_one(path):
    if quota_paused():
        return False
    with _active_lock:
        active = [head_id for target, head_id in _active_heads if target == path]
        head = watch_store.claim(path, exclude=active)
        if not head:
            return False
        _active_heads.add((path, head["id"]))
    try:
        if head["repo"].casefold() not in {name.casefold() for name in _watched_repos(path)}:
            with _watched_repos_lock:
                watch_store.close_claim(path, head)
                # An unwatch between ticks must still allow the next tick to restore this head.
                previous = _previous_watched_repos.get(str(Path(path).resolve()))
                if previous is not None:
                    previous.discard(head["repo"].casefold())
            return True
        return _process_claimed(path, head)
    finally:
        with _active_lock:
            _active_heads.discard((path, head["id"]))


def _process_claimed(path, head):
    review_id = None
    try:
        # Manual reviews can complete between listing and claim.
        with watch_store.connection(path) as conn:
            done = conn.execute(
                "SELECT id FROM reviews WHERE repo=? AND pr=? AND head_sha=? AND status='done'",
                (head["repo"], head["pr"], head["head_sha"]),
            ).fetchone()
        if done:
            review_id, error = done["id"], None
        else:
            review_id, error = trigger_review(
                path, head["repo"], head["pr"], head["head_sha"], head=head
            )
    except Exception as exc:
        error = exc
    watch_store.finish(path, head, review_id, error)
    return True


async def _worker(path, pool):
    loop = asyncio.get_running_loop()
    while True:
        try:
            worked = await loop.run_in_executor(pool, process_one, path)
        except Exception:
            log.error("watcher: worker failed", exc_info=True)
            worked = False
        if not worked:
            await asyncio.sleep(5)


async def run_forever(path=None):
    path = path or db.db_path()
    try:
        watch_store.reclaim(path, startup=True)
    except Exception:
        log.warning("watcher: startup reclaim failed", exc_info=True)
    try:
        with watch_store.connection(path) as conn:
            # A previous process's listing cannot establish which PRs are still open.
            conn.execute("UPDATE pr_heads SET is_open=0")
    except Exception:
        log.warning("watcher: startup open-head reset failed", exc_info=True)
    pool = ThreadPoolExecutor(max_workers=worker_count(), thread_name_prefix="argus-review")
    workers = [asyncio.create_task(_worker(path, pool)) for _ in range(worker_count())]
    try:
        deadline = time.monotonic()
        while True:
            try:
                await poll_once(path)
            except Exception:
                log.error("watcher: tick failed", exc_info=True)
            deadline += poll_interval()
            # Missed slots do not cause a burst of catch-up requests.
            while deadline < time.monotonic():
                deadline += poll_interval()
            await asyncio.sleep(max(0, deadline - time.monotonic()))
    finally:
        key = str(Path(path).resolve())
        collection = _outcome_tasks.get(key)
        if collection:
            collection.cancel()
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, *([collection] if collection else []), return_exceptions=True)
        if _outcome_tasks.get(key) is collection:
            _outcome_tasks.pop(key, None)
        pool.shutdown(wait=False, cancel_futures=True)


def stats(path):
    config = db.get_config(path) or {}
    return {
        "enabled": is_enabled(),
        "tick_interval": poll_interval(),
        "workers": worker_count(),
        "llm_cap": LLM_MAX_CONCURRENCY,
        "llm_in_flight": llm_in_flight(),
        "queue_sizes": watch_store.queue_sizes(path),
        "effective_watched_repos": len(watch_config.effective(config)),
        "deny": config.get("deny", []),
        "hard_denied_owners": sorted(hard_deny_owners()),
        "github_quota": github.quota_snapshot(),
        "hard_deny_refusals": github.refusal_count(),
        "last_tick_at": None,
        "last_tick_duration_s": None,
        **_last_tick.get(path, {}),
    }
