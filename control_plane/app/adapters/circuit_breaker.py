"""Minimal three-state circuit breaker. No third-party dependency.

States: ``closed`` -> ``open`` -> ``half_open`` -> ``closed``.

  * **closed** -- calls pass. ``fail_max`` consecutive failures open it.
  * **open** -- calls short-circuit immediately, so a dead dependency costs a
    dict lookup instead of a connect timeout. This is what keeps the
    ``/overview`` fan-out inside its budget while Milvus is down.
  * **half_open** -- reached after ``reset_timeout_s``. Exactly one trial call
    is admitted: success closes the breaker, failure re-opens it and restarts
    the timer.

**Who drives recovery.** The scheduled health job calls with ``force=True`` and
therefore always probes, never short-circuiting. It is the thing that discovers
recovery, so detection latency stays one health interval instead of up to
``reset_timeout_s``. Request-path callers honour the breaker normally. A forced
call still records its outcome, so a forced success closes the breaker for
everyone.

A short-circuited call is not a probe: callers must record it with a distinct
code and must NOT treat it as a health transition, or the events table fills
with transitions that never happened.
"""

from __future__ import annotations

import enum
import time
from collections.abc import Callable
from typing import Any

from app.logging_conf import get_logger

log = get_logger("breaker")

# Distinct from the MILVUS_* classification codes: this says "we did not call",
# not "the call failed".
BREAKER_OPEN_CODE = "BREAKER_OPEN"


class BreakerState(enum.StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenError(Exception):
    """Raised instead of calling through an open breaker."""

    def __init__(self, name: str, retry_after_s: float) -> None:
        super().__init__(f"circuit breaker '{name}' is open")
        self.name = name
        self.retry_after_s = retry_after_s
        self.code = BREAKER_OPEN_CODE


class CircuitBreaker:
    """One breaker per dependency.

    Callbacks fire on transitions only -- never per call -- so wiring them to
    the events table (WP-09) cannot produce a row on every poll.
    """

    def __init__(
        self,
        name: str,
        fail_max: int = 3,
        reset_timeout_s: float = 30.0,
        *,
        on_open: Callable[[str, dict[str, Any]], None] | None = None,
        on_close: Callable[[str, dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if fail_max < 1:
            raise ValueError("fail_max must be >= 1")
        self.name = name
        self.fail_max = fail_max
        self.reset_timeout_s = float(reset_timeout_s)
        self._on_open = on_open
        self._on_close = on_close
        self._clock = clock

        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._trial_in_flight = False
        self._last_error_code: str | None = None

    # --- state ------------------------------------------------------------
    @property
    def state(self) -> BreakerState:
        """Current state, promoting open -> half_open once the timer expires.

        Computed on read rather than by a timer task: no background work is
        needed to notice that the reset window has elapsed.
        """
        if (
            self._state is BreakerState.OPEN
            and self._opened_at is not None
            and self._clock() - self._opened_at >= self.reset_timeout_s
        ):
            self._state = BreakerState.HALF_OPEN
            self._trial_in_flight = False
            log.info("breaker_half_open", breaker=self.name)
        return self._state

    @property
    def is_closed(self) -> bool:
        return self.state is BreakerState.CLOSED

    def allow(self) -> bool:
        """Whether a call may proceed. Reserves the half-open trial slot."""
        state = self.state
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.OPEN:
            return False
        if self._trial_in_flight:
            return False
        self._trial_in_flight = True
        return True

    def retry_after_s(self) -> float:
        if self._state is not BreakerState.OPEN or self._opened_at is None:
            return 0.0
        return max(0.0, self.reset_timeout_s - (self._clock() - self._opened_at))

    # --- outcomes ---------------------------------------------------------
    def record_success(self) -> None:
        was = self._state
        self._consecutive_failures = 0
        self._last_error_code = None
        self._trial_in_flight = False
        self._state = BreakerState.CLOSED
        if was is not BreakerState.CLOSED:
            log.info("breaker_closed", breaker=self.name, previous=str(was))
            self._emit(self._on_close, {"previous_state": str(was)})

    def record_failure(self, error_code: str | None = None) -> None:
        self._last_error_code = error_code
        state = self.state
        self._trial_in_flight = False

        if state is BreakerState.HALF_OPEN:
            # The trial failed: straight back to open, timer restarted.
            self._open(error_code, reason="half_open_trial_failed")
            return

        self._consecutive_failures += 1
        if state is BreakerState.CLOSED and self._consecutive_failures >= self.fail_max:
            self._open(error_code, reason="fail_max_reached")

    def _open(self, error_code: str | None, reason: str) -> None:
        already_open = self._state is BreakerState.OPEN
        self._state = BreakerState.OPEN
        self._opened_at = self._clock()
        if already_open:
            return
        log.warning(
            "breaker_opened",
            breaker=self.name,
            reason=reason,
            failures=self._consecutive_failures,
            error_code=error_code,
        )
        self._emit(
            self._on_open,
            {
                "reason": reason,
                "consecutive_failures": self._consecutive_failures,
                "error_code": error_code,
                "reset_timeout_s": self.reset_timeout_s,
            },
        )

    def _emit(
        self, cb: Callable[[str, dict[str, Any]], None] | None, detail: dict[str, Any]
    ) -> None:
        """Invoke a transition callback, never letting it break the caller."""
        if cb is None:
            return
        try:
            cb(self.name, detail)
        except Exception:
            log.exception("breaker_callback_failed", breaker=self.name)

    # --- convenience ------------------------------------------------------
    async def call(self, fn: Callable[[], Any], *, force: bool = False) -> Any:
        """Run ``fn`` under the breaker.

        ``force=True`` bypasses the gate but still records the outcome; that is
        how the scheduled health job drives recovery.
        """
        if not force and not self.allow():
            raise CircuitBreakerOpenError(self.name, self.retry_after_s())
        try:
            result = await fn()
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": str(self.state),
            "consecutive_failures": self._consecutive_failures,
            "fail_max": self.fail_max,
            "retry_after_s": round(self.retry_after_s(), 2),
            "last_error_code": self._last_error_code,
        }

    def reset(self) -> None:
        """Force back to closed. For tests and manual intervention."""
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = None
        self._trial_in_flight = False


_registry: dict[str, CircuitBreaker] = {}


def get_breaker(name: str, **kwargs: Any) -> CircuitBreaker:
    """Process-wide breaker per dependency name."""
    if name not in _registry:
        _registry[name] = CircuitBreaker(name, **kwargs)
    return _registry[name]


def all_breakers() -> dict[str, CircuitBreaker]:
    return dict(_registry)


def reset_all() -> None:
    _registry.clear()
