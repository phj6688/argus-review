"""Keep note budgets and write serialization local to each client."""

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from argus.gitlab import config


@dataclass
class _Bucket:
    tokens: float
    updated: float
    not_before: float = 0.0


class NotePacer:
    def __init__(self, rate_per_minute: int | None = None, capacity: int = 5, *,
                 clock=time.monotonic, sleep=time.sleep):
        rate = config.notes_per_minute() if rate_per_minute is None else rate_per_minute
        if rate <= 0 or capacity < 1:
            raise ValueError("Note rate and capacity must be positive")
        self._rate, self._capacity = rate / 60.0, capacity
        self._clock, self._sleep = clock, sleep
        self._mutex = threading.Lock()
        self._buckets = {}
        self._locks = {}

    def acquire(self, bot_user_id: int) -> None:
        while True:
            with self._mutex:
                now = self._clock()
                bucket = self._buckets.setdefault(bot_user_id, _Bucket(self._capacity, now))
                bucket.tokens = min(self._capacity, bucket.tokens + max(0, now - bucket.updated) * self._rate)
                bucket.updated = max(now, bucket.updated)
                if bucket.tokens >= 1 and now >= bucket.not_before:
                    bucket.tokens -= 1
                    return
                wait = max(bucket.not_before - now,
                           max(0, bucket.updated - now) + max(0, (1 - bucket.tokens) / self._rate))
            # Delayed sleepers must compete for tokens available when they wake.
            self._sleep(wait)

    def on_rate_limited(self, bot_user_id: int, retry_after: float) -> None:
        with self._mutex:
            now = self._clock()
            bucket = self._buckets.setdefault(bot_user_id, _Bucket(self._capacity, now))
            available = bucket.tokens + max(0, now - bucket.updated) * self._rate
            next_token = max(now, bucket.updated) + max(0, (1 - available) / self._rate)
            bucket.tokens = 0
            # The first token is ready at the deadline, with later tokens spaced out.
            bucket.not_before = max(bucket.not_before, now + max(0, retry_after), next_token)
            bucket.updated = bucket.not_before - 1 / self._rate

    @contextmanager
    def write_lock(self, bot_user_id: int, project_id: int, iid: int):
        key = (bot_user_id, project_id, iid)
        with self._mutex:
            lock, users = self._locks.get(key, (threading.RLock(), 0))
            self._locks[key] = (lock, users + 1)
        try:
            with lock:
                yield
        finally:
            with self._mutex:
                _, users = self._locks[key]
                if users == 1:
                    del self._locks[key]
                else:
                    self._locks[key] = (lock, users - 1)


def pacing_for(client) -> NotePacer:
    with client._lock:
        if client.pacing is None:
            client.pacing = NotePacer()
        return client.pacing
