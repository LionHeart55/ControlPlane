"""Circuit-breaker state machine and last-known-good cache. No infrastructure."""

from __future__ import annotations

from typing import Any

import pytest

from app.adapters.cache import LastKnownGoodCache
from app.adapters.circuit_breaker import BreakerState, CircuitBreaker, CircuitBreakerOpenError


class FakeClock:
    """Injectable monotonic clock so tests never sleep."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def state_of(b: CircuitBreaker) -> BreakerState:
    """Read the state through a function boundary.

    mypy narrows a property across successive reads, so `assert b.state is
    CLOSED` followed by `assert b.state is OPEN` looks self-contradictory and
    everything after it is flagged unreachable. Returning it from a function
    re-widens the type without weakening the assertion.
    """
    return b.state


def make_breaker(clock: FakeClock, **kw: Any) -> tuple[CircuitBreaker, list, list]:
    opened: list[tuple[str, dict]] = []
    closed: list[tuple[str, dict]] = []
    b = CircuitBreaker(
        "milvus",
        fail_max=kw.pop("fail_max", 3),
        reset_timeout_s=kw.pop("reset_timeout_s", 30),
        on_open=lambda n, d: opened.append((n, d)),
        on_close=lambda n, d: closed.append((n, d)),
        clock=clock,
        **kw,
    )
    return b, opened, closed


# --- breaker -------------------------------------------------------------
def test_starts_closed_and_allows() -> None:
    b, _, _ = make_breaker(FakeClock())
    assert state_of(b) is BreakerState.CLOSED
    assert b.allow() is True


def test_opens_only_at_fail_max() -> None:
    b, opened, _ = make_breaker(FakeClock(), fail_max=3)
    b.record_failure("MILVUS_UNREACHABLE")
    b.record_failure("MILVUS_UNREACHABLE")
    assert state_of(b) is BreakerState.CLOSED, "must not open early"
    assert opened == []
    b.record_failure("MILVUS_UNREACHABLE")
    assert state_of(b) is BreakerState.OPEN
    assert len(opened) == 1
    assert opened[0][1]["error_code"] == "MILVUS_UNREACHABLE"


def test_success_resets_the_failure_run() -> None:
    b, opened, _ = make_breaker(FakeClock(), fail_max=3)
    b.record_failure()
    b.record_failure()
    b.record_success()
    b.record_failure()
    b.record_failure()
    assert state_of(b) is BreakerState.CLOSED, "counter must be consecutive failures only"
    assert opened == []


def test_open_blocks_calls_until_reset_timeout() -> None:
    clock = FakeClock()
    b, _, _ = make_breaker(clock, fail_max=1, reset_timeout_s=30)
    b.record_failure()
    assert b.allow() is False
    clock.advance(29)
    assert b.allow() is False
    clock.advance(2)
    assert state_of(b) is BreakerState.HALF_OPEN
    assert b.allow() is True


def test_half_open_admits_exactly_one_trial() -> None:
    clock = FakeClock()
    b, _, _ = make_breaker(clock, fail_max=1, reset_timeout_s=10)
    b.record_failure()
    clock.advance(11)
    assert b.allow() is True, "first trial admitted"
    assert b.allow() is False, "second concurrent trial must be refused"


def test_half_open_success_closes_and_emits_once() -> None:
    clock = FakeClock()
    b, opened, closed = make_breaker(clock, fail_max=1, reset_timeout_s=10)
    b.record_failure()
    clock.advance(11)
    b.allow()
    b.record_success()
    assert state_of(b) is BreakerState.CLOSED
    assert len(closed) == 1
    assert len(opened) == 1


def test_half_open_failure_reopens_and_restarts_timer() -> None:
    clock = FakeClock()
    b, _, _ = make_breaker(clock, fail_max=1, reset_timeout_s=10)
    b.record_failure()
    clock.advance(11)
    b.allow()
    b.record_failure()
    assert state_of(b) is BreakerState.OPEN
    clock.advance(5)
    assert b.allow() is False, "timer must restart from the reopen"
    clock.advance(6)
    assert state_of(b) is BreakerState.HALF_OPEN


def test_transition_callbacks_fire_only_on_transition() -> None:
    """The events table must not gain a row per poll."""
    clock = FakeClock()
    b, opened, closed = make_breaker(clock, fail_max=1, reset_timeout_s=60)
    for _ in range(10):
        b.record_failure()
    assert len(opened) == 1, "repeated failures while open must not re-emit"
    assert closed == []


def test_callback_exception_does_not_break_the_breaker() -> None:
    def boom(name: str, detail: dict) -> None:
        raise RuntimeError("event write failed")

    b = CircuitBreaker("milvus", fail_max=1, on_open=boom, clock=FakeClock())
    b.record_failure()
    assert state_of(b) is BreakerState.OPEN


async def test_call_helper_enforces_and_records() -> None:
    clock = FakeClock()
    b, _, _ = make_breaker(clock, fail_max=1, reset_timeout_s=60)

    async def failing() -> None:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await b.call(failing)
    assert state_of(b) is BreakerState.OPEN

    async def ok() -> str:
        return "fine"

    with pytest.raises(CircuitBreakerOpenError):
        await b.call(ok)
    assert await b.call(ok, force=True) == "fine"
    assert state_of(b) is BreakerState.CLOSED


# --- cache ---------------------------------------------------------------
def test_set_then_get_fresh() -> None:
    c = LastKnownGoodCache(ttl_s=5, stale_after_s=60)
    c.set("cid", "collections", ["a", "b"])
    entry = c.get_fresh("cid", "collections")
    assert entry is not None and entry.value == ["a", "b"]
    assert entry.is_stale is False
    assert entry.observed_at.tzinfo is not None


def test_miss_returns_none() -> None:
    c = LastKnownGoodCache(ttl_s=5, stale_after_s=60)
    assert c.get_fresh("cid", "nothing") is None
    assert c.get_stale("cid", "nothing") is None


def test_keys_are_scoped_per_cluster_and_resource() -> None:
    c = LastKnownGoodCache(ttl_s=5, stale_after_s=60)
    c.set("cluster-a", "metrics", 1)
    c.set("cluster-b", "metrics", 2)
    a = c.get_fresh("cluster-a", "metrics")
    b = c.get_fresh("cluster-b", "metrics")
    assert a is not None and a.value == 1
    assert b is not None and b.value == 2
    assert c.get_fresh("cluster-a", "collections") is None


def test_beyond_ttl_is_stale_but_still_served(monkeypatch: pytest.MonkeyPatch) -> None:
    c = LastKnownGoodCache(ttl_s=5, stale_after_s=60)
    c.set("cid", "metrics", {"v": 1})

    entry = c.get_fresh("cid", "metrics")
    assert entry is not None
    # Age the entry by rewriting its monotonic stamp rather than sleeping.
    aged = c._cache[("cid", "metrics")]
    object.__setattr__(aged, "monotonic_at", aged.monotonic_at - 10)

    assert c.get_fresh("cid", "metrics") is None, "past ttl it is no longer fresh"
    stale = c.get_stale("cid", "metrics")
    assert stale is not None
    assert stale.is_stale is True, "callers must be able to dim it"
    assert stale.value == {"v": 1}
    assert stale.observed_at == entry.observed_at, "original observation time is preserved"


def test_beyond_stale_window_is_dropped() -> None:
    """An hour-old number must not be served at all, flagged or otherwise."""
    c = LastKnownGoodCache(ttl_s=1, stale_after_s=2)
    c.set("cid", "metrics", 1)
    aged = c._cache[("cid", "metrics")]
    object.__setattr__(aged, "monotonic_at", aged.monotonic_at - 3600)
    assert c.get_stale("cid", "metrics") is None


def test_rejects_stale_window_smaller_than_ttl() -> None:
    with pytest.raises(ValueError):
        LastKnownGoodCache(ttl_s=60, stale_after_s=5)


def test_invalidate_and_clear() -> None:
    c = LastKnownGoodCache(ttl_s=5, stale_after_s=60)
    c.set("cid", "a", 1)
    c.set("cid", "b", 2)
    c.invalidate("cid", "a")
    assert c.get_stale("cid", "a") is None
    assert len(c) == 1
    c.clear()
    assert len(c) == 0
