"""The degradation envelope. Pure logic, no infrastructure.

These cover the rule the whole API rests on: a dependency being down produces a
well-formed 200 envelope, never an exception, and stale data is always labelled
as stale.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest

from app.adapters.cache import LastKnownGoodCache
from app.api.envelope import resolve_live
from app.api.errors import DependencyUnavailableError
from app.schemas.common import Envelope, LiveStatus, Page

CID = "11111111-1111-1111-1111-111111111111"


def cache(ttl_s: float = 5.0, stale_after_s: float = 120.0) -> LastKnownGoodCache:
    return LastKnownGoodCache(ttl_s=ttl_s, stale_after_s=stale_after_s)


def down(code: str = "MILVUS_UNREACHABLE") -> DependencyUnavailableError:
    return DependencyUnavailableError("connection refused", dependency="milvus", code=code)


# --- the happy path -------------------------------------------------------
async def test_success_is_ok_and_populates_the_cache() -> None:
    store = cache()
    outcome = await resolve_live(
        cluster_id=CID, resource="metrics", fetch=lambda: _value({"n": 1}), cache=store
    )
    assert outcome.live_status is LiveStatus.OK
    assert outcome.stale is False
    assert outcome.degraded_reason is None
    assert store.get_fresh(CID, "metrics") is not None, "a success must seed the fallback"


# --- failure with a cached value -----------------------------------------
async def test_failure_falls_back_to_cache_and_marks_it_stale() -> None:
    store = cache()
    await resolve_live(
        cluster_id=CID, resource="metrics", fetch=lambda: _value({"n": 1}), cache=store
    )
    outcome = await resolve_live(
        cluster_id=CID, resource="metrics", fetch=lambda: _raise(down()), cache=store
    )
    assert outcome.live == {"n": 1}
    assert outcome.live_status is LiveStatus.STALE
    assert outcome.stale is True
    assert outcome.degraded_reason is not None
    assert outcome.degraded_reason.code == "MILVUS_UNREACHABLE"


async def test_cached_fallback_is_stale_even_when_still_inside_the_ttl() -> None:
    """The value may be one second old, but it was not verifiable just now.

    Reporting `ok` here would show a number the dependency never confirmed,
    which is the failure mode the whole envelope exists to prevent.
    """
    store = cache(ttl_s=3600.0, stale_after_s=7200.0)
    await resolve_live(
        cluster_id=CID, resource="metrics", fetch=lambda: _value({"n": 1}), cache=store
    )
    outcome = await resolve_live(
        cluster_id=CID, resource="metrics", fetch=lambda: _raise(down()), cache=store
    )
    assert outcome.stale is True
    assert outcome.live_status is LiveStatus.STALE


async def test_stale_response_keeps_the_original_observed_at() -> None:
    store = cache()
    before = dt.datetime.now(dt.UTC)
    await resolve_live(
        cluster_id=CID, resource="metrics", fetch=lambda: _value({"n": 1}), cache=store
    )
    after_write = dt.datetime.now(dt.UTC)

    outcome = await resolve_live(
        cluster_id=CID, resource="metrics", fetch=lambda: _raise(down()), cache=store
    )
    assert before <= outcome.observed_at <= after_write, (
        "observed_at must be when the data was true, not when it was served"
    )


# --- failure with nothing cached -----------------------------------------
async def test_failure_without_cache_is_unavailable_with_a_reason() -> None:
    outcome = await resolve_live(
        cluster_id=CID,
        resource="metrics",
        fetch=lambda: _raise(down("DOCKER_UNAVAILABLE")),
        cache=cache(),
    )
    assert outcome.live is None
    assert outcome.live_status is LiveStatus.UNAVAILABLE
    assert outcome.stale is False
    assert outcome.degraded_reason is not None
    assert outcome.degraded_reason.code == "DOCKER_UNAVAILABLE"


async def test_expired_cache_is_not_served() -> None:
    """Past the stale window, nothing is better than something."""
    store = cache(ttl_s=0.01, stale_after_s=0.05)
    await resolve_live(
        cluster_id=CID, resource="metrics", fetch=lambda: _value({"n": 1}), cache=store
    )
    await asyncio.sleep(0.1)
    outcome = await resolve_live(
        cluster_id=CID, resource="metrics", fetch=lambda: _raise(down()), cache=store
    )
    assert outcome.live is None
    assert outcome.live_status is LiveStatus.UNAVAILABLE


# --- uncacheable resources -----------------------------------------------
async def test_uncacheable_resource_never_returns_stale_data() -> None:
    """Logs and health verdicts must not be replayed.

    A stale log tail is indistinguishable from a live one and would send
    someone debugging the wrong minute.
    """
    store = cache()
    await resolve_live(
        cluster_id=CID,
        resource="logs",
        fetch=lambda: _value(["line"]),
        cache=store,
        cacheable=False,
    )
    outcome = await resolve_live(
        cluster_id=CID, resource="logs", fetch=lambda: _raise(down()), cache=store, cacheable=False
    )
    assert outcome.live is None
    assert outcome.live_status is LiveStatus.UNAVAILABLE


# --- timeouts -------------------------------------------------------------
async def test_branch_timeout_is_reported_not_raised() -> None:
    async def slow() -> str:
        await asyncio.sleep(5)
        return "never"

    outcome = await resolve_live(
        cluster_id=CID, resource="slow", fetch=slow, cache=cache(), timeout_s=0.05
    )
    assert outcome.live_status is LiveStatus.UNAVAILABLE
    assert outcome.degraded_reason is not None
    assert outcome.degraded_reason.code == "UPSTREAM_TIMEOUT"


# --- bugs are not dependency failures ------------------------------------
async def test_programming_errors_still_propagate() -> None:
    """Deliberate: a bug must surface as a 500, not as a quiet null forever."""

    async def buggy() -> None:
        raise KeyError("typo")

    with pytest.raises(KeyError):
        await resolve_live(cluster_id=CID, resource="x", fetch=buggy, cache=cache())


# --- envelope shape -------------------------------------------------------
def test_envelope_kwargs_populate_the_response_model() -> None:
    outcome_fields = {
        "live": {"n": 1},
        "live_status": LiveStatus.OK,
        "observed_at": dt.datetime.now(dt.UTC),
        "stale": False,
        "degraded_reason": None,
    }
    envelope = Envelope[dict](**outcome_fields)
    body = envelope.model_dump()
    assert set(body) >= {
        "cluster",
        "live",
        "live_status",
        "observed_at",
        "stale",
        "degraded_reason",
    }


def test_page_reports_more_than_it_returns() -> None:
    page = Page[int](items=[1, 2], total=10, limit=2, offset=0)
    assert page.has_more is True
    assert Page[int](items=[1], total=1, limit=50, offset=0).has_more is False


# --- helpers --------------------------------------------------------------
async def _value[T](v: T) -> T:
    return v


async def _raise(exc: BaseException) -> Any:
    """Returns Any, not None: with `-> None` mypy binds the generic to None and
    then reads every assertion about a cached value as unreachable."""
    raise exc
