import copy
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from argus import github
from argus.gitlab import pacing
from argus.gitlab.pacing import NotePacer, pacing_for
from test_gitlab_client import configured as configured, make_client as make_client


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


def test_initial_burst_and_refill(clock):
    pacer = NotePacer(30, clock=clock, sleep=clock.sleep)
    for _ in range(5):
        pacer.acquire(7)
    assert clock.waits == []
    pacer.acquire(7)
    assert clock.waits == [2.0]


def test_sliding_window_limit(clock):
    pacer = NotePacer(30, clock=clock, sleep=clock.sleep)
    times = []
    while clock.now <= 200:
        pacer.acquire(7)
        times.append(clock.now)
    assert max(sum(start <= t <= start + 60 for t in times) for start in times) == 35


def test_configured_rate_and_refill_capacity(clock, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_NOTES_PER_MINUTE", "60")
    pacer = NotePacer(capacity=2, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        pacer.acquire(7)
    assert clock.waits == [1.0]
    clock.now += 100
    for _ in range(3):
        pacer.acquire(7)
    assert clock.waits == [1.0, 1.0]


def test_client_sharing_and_user_isolation(make_client, clock):
    client, _ = make_client(lambda _: httpx.Response(200))
    other, _ = make_client(lambda _: httpx.Response(200))
    assert client.pacing is None
    with ThreadPoolExecutor(max_workers=8) as pool:
        pacers = list(pool.map(lambda _: pacing_for(client), range(20)))
    assert all(pacer is pacers[0] for pacer in pacers)
    assert pacing_for(other) is not pacers[0]
    client.pacing = NotePacer(30, clock=clock, sleep=clock.sleep)
    for _ in range(5):
        pacing_for(client).acquire(7)
    pacing_for(client).acquire(8)
    assert clock.now == 0
    pacing_for(client).acquire(7)
    assert clock.now == 2


def test_rate_limit_is_local_and_waits_retry_after(clock, monkeypatch):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as gh_client:
        monkeypatch.setattr(github, "_http", gh_client)
        module_before = dict(vars(github))
        quota_before = copy.deepcopy(github._quota)
        cache_before = copy.deepcopy(github._cache)
        client_before = dict(vars(gh_client))
        pacing_before = dict(vars(pacing))
        pacer = NotePacer(30, clock=clock, sleep=clock.sleep)
        pacer.on_rate_limited(7, 60)
        pacer.acquire(8)
        assert clock.now == 0
        pacer.acquire(7)
        assert clock.waits == [60.0]
        pacer.acquire(7)
        assert clock.waits == [60.0, 2.0]
        assert vars(github) == module_before
        assert github._quota == quota_before and github._cache == cache_before
        assert vars(gh_client) == client_before
        assert vars(pacing) == pacing_before
        assert not any(isinstance(value, (dict, list)) for name, value in vars(pacing).items()
                       if not name.startswith("__"))


@pytest.mark.parametrize("capacity, callers, release_at", [(1, 4, 2.0), (5, 36, 80.0)])
def test_concurrent_acquisitions_recheck_tokens_after_waking(clock, capacity, callers, release_at):
    condition = threading.Condition()
    sleepers = {}
    times = []
    stopped = False

    def sleep(seconds):
        with condition:
            deadline = clock.now + seconds
            thread_id = threading.get_ident()
            sleepers[thread_id] = deadline
            condition.notify_all()
            assert condition.wait_for(lambda: stopped or clock.now >= deadline, timeout=3)
            del sleepers[thread_id]
            if stopped:
                raise RuntimeError("Test stopped")

    def acquire():
        pacer.acquire(7)
        with condition:
            times.append(clock.now)
            condition.notify_all()

    def settled():
        return (len(times) + len(sleepers) == capacity + callers
                and all(deadline > clock.now for deadline in sleepers.values()))

    pacer = NotePacer(30, capacity=capacity, clock=clock, sleep=sleep)
    for _ in range(capacity):
        acquire()
    with ThreadPoolExecutor(max_workers=callers) as pool:
        futures = [pool.submit(acquire) for _ in range(callers)]
        try:
            with condition:
                assert condition.wait_for(settled, timeout=3)
                assert len(sleepers) == callers
                clock.now = release_at
                condition.notify_all()
                # Every caller must finish or sleep again before time can advance.
                assert condition.wait_for(settled, timeout=3)
                assert times.count(release_at) == capacity
                assert len(sleepers) == callers - capacity
                while sleepers:
                    clock.now = min(sleepers.values())
                    condition.notify_all()
                    assert condition.wait_for(settled, timeout=3)
            for future in futures:
                future.result(timeout=3)
        finally:
            with condition:
                stopped = True
                condition.notify_all()

    assert len(times) == capacity + callers
    if capacity == 1:
        assert times == [0.0, 2.0, 4.0, 6.0, 8.0]
    assert max(sum(start <= t <= start + 60 for t in times) for start in times) <= 35


@pytest.mark.parametrize("delay", [0.0, 1.0, 2.0, 60.0])
def test_first_token_is_available_at_retry_deadline(clock, delay):
    pacer = NotePacer(30, clock=clock, sleep=clock.sleep)
    pacer.on_rate_limited(7, delay)
    pacer.acquire(7)
    assert clock.now == delay


def test_new_pause_invalidates_waiting_reservations(clock):
    def sleep(seconds):
        if not clock.waits:
            pacer.on_rate_limited(7, 60)
        clock.sleep(seconds)

    pacer = NotePacer(30, capacity=1, clock=clock, sleep=sleep)
    pacer.acquire(7)
    pacer.acquire(7)
    assert clock.now == 60
    assert clock.waits == [2.0, 58.0]


@pytest.mark.parametrize("pauses", [[0], [1], [60, 0], [1, 0, 0]])
def test_pause_does_not_replenish_an_exhausted_bucket(clock, pauses):
    pacer = NotePacer(30, clock=clock, sleep=clock.sleep)
    times = []
    for _ in range(5):
        pacer.acquire(7)
        times.append(clock.now)
    for pause in pauses:
        pacer.on_rate_limited(7, pause)
    while clock.now <= 200:
        pacer.acquire(7)
        times.append(clock.now)
    assert times[5] >= max(2.0, *pauses)
    assert max(sum(start <= t <= start + 60 for t in times) for start in times) <= 35


def test_same_mr_lock_serializes_and_is_reentrant():
    pacer = NotePacer()
    entered, attempted, release = (threading.Event() for _ in range(3))
    order = []

    def first():
        with pacer.write_lock(7, 42, 3):
            with pacer.write_lock(7, 42, 3):
                order.append("first entered")
                entered.set()
                assert release.wait(3)
            order.append("first exited")

    def second():
        assert entered.wait(3)
        attempted.set()
        with pacer.write_lock(7, 42, 3):
            order.append("second entered")
        order.append("second exited")

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(first), pool.submit(second)
        try:
            assert attempted.wait(3)
            assert order == ["first entered"]
        finally:
            release.set()
        a.result(timeout=3)
        b.result(timeout=3)
    assert order == ["first entered", "first exited", "second entered", "second exited"]
    assert pacer._locks == {}


@pytest.mark.parametrize("other", [(7, 42, 4), (8, 42, 3), (7, 43, 3)])
def test_independent_locks_run_in_parallel(other):
    pacer = NotePacer()
    barrier = threading.Barrier(2, timeout=3)

    def hold(key):
        with pacer.write_lock(*key):
            barrier.wait()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(hold, [(7, 42, 3), other]))
    assert pacer._locks == {}


def test_lock_registry_cleans_up_after_exception():
    pacer = NotePacer()
    with pytest.raises(ValueError), pacer.write_lock(7, 42, 3):
        raise ValueError("failed write")
    assert pacer._locks == {}
