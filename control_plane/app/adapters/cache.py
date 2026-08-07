"""Last-known-good cache.

Two time windows over the same entry:

  * **fresh** (``CP_CACHE_TTL_S``) -- young enough to serve as current.
  * **stale** (``Settings.stale_after_s``) -- too old to present as current, but
    still better than nothing, and returned with ``stale: true`` plus the
    original ``observed_at`` so the dashboard can dim it.

Past the stale window an entry is dropped entirely. Showing an hour-old row
count as though it were current is worse than showing nothing, so the cache
refuses to serve it at all rather than leaving that judgement to callers.

Ages are measured with a monotonic clock so a host clock adjustment (NTP step,
laptop resuming from sleep) cannot make an entry look newer than it is;
``observed_at`` is kept separately as wall-clock UTC purely for display.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from dataclasses import dataclass
from typing import Any

from cachetools import TTLCache

CacheKey = tuple[str, str]


@dataclass(frozen=True)
class CacheEntry:
    """A cached value plus when it was observed."""

    value: Any
    observed_at: dt.datetime
    monotonic_at: float
    is_stale: bool = False

    def age_s(self, now: float | None = None) -> float:
        return (time.monotonic() if now is None else now) - self.monotonic_at


class LastKnownGoodCache:
    """TTL cache keyed by ``(cluster_id, resource)``."""

    def __init__(self, ttl_s: float, stale_after_s: float, maxsize: int = 2048) -> None:
        if stale_after_s < ttl_s:
            raise ValueError("stale_after_s must be >= ttl_s")
        self._ttl_s = float(ttl_s)
        self._stale_after_s = float(stale_after_s)
        # The TTLCache itself expires at the OUTER (stale) boundary; freshness
        # is decided per read by comparing ages. One store, two windows.
        self._cache: TTLCache[CacheKey, CacheEntry] = TTLCache(
            maxsize=maxsize, ttl=self._stale_after_s
        )
        # cachetools is not thread-safe and adapter calls land here from
        # asyncio.to_thread worker threads as well as the event loop.
        self._lock = threading.Lock()

    @staticmethod
    def _key(cluster_id: Any, resource: str) -> CacheKey:
        return (str(cluster_id), resource)

    def set(self, cluster_id: Any, resource: str, value: Any) -> CacheEntry:
        """Record a successful observation."""
        entry = CacheEntry(
            value=value,
            observed_at=dt.datetime.now(dt.UTC),
            monotonic_at=time.monotonic(),
        )
        with self._lock:
            self._cache[self._key(cluster_id, resource)] = entry
        return entry

    def get_fresh(self, cluster_id: Any, resource: str) -> CacheEntry | None:
        """Entry young enough to serve as current, else None."""
        entry = self._get(cluster_id, resource)
        if entry is None or entry.age_s() > self._ttl_s:
            return None
        return entry

    def get_stale(self, cluster_id: Any, resource: str) -> CacheEntry | None:
        """Any entry still inside the stale window, flagged accordingly.

        Callers must surface ``is_stale`` to the user; that flag is the whole
        contract of this method.
        """
        entry = self._get(cluster_id, resource)
        if entry is None:
            return None
        # Enforce the outer bound here rather than relying solely on TTLCache's
        # own expiry. The "never present hour-old data as current" rule is a
        # correctness property of this class; delegating it entirely to
        # cachetools would leave it untested and silently dependent on that
        # library's timer semantics.
        if entry.age_s() > self._stale_after_s:
            self.invalidate(cluster_id, resource)
            return None
        if entry.age_s() <= self._ttl_s:
            return entry
        return CacheEntry(
            value=entry.value,
            observed_at=entry.observed_at,
            monotonic_at=entry.monotonic_at,
            is_stale=True,
        )

    def _get(self, cluster_id: Any, resource: str) -> CacheEntry | None:
        with self._lock:
            # Reading a TTLCache can trigger expiry, hence the lock on reads too.
            return self._cache.get(self._key(cluster_id, resource))

    def invalidate(self, cluster_id: Any, resource: str) -> None:
        with self._lock:
            self._cache.pop(self._key(cluster_id, resource), None)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)
