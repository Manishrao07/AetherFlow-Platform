"""Circuit breaker: stop calling a dependency that is clearly down.

What: a small state machine - CLOSED (normal) -> OPEN (fail fast) -> HALF_OPEN (probe) -> CLOSED.
Why: if the LLM provider is down, retrying every request for 30s each ties up workers and
makes the outage worse. After N consecutive failures we reject calls instantly for a cool-off
period, then let ONE trial request through; success closes the circuit, failure re-opens it.
Why injectable clock: tests can move time forward without sleeping.
Connects to: ``client.py`` (LLM) and the tool registry (external tools).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TypeVar

from app.core.llm.base import CircuitOpenError

T = TypeVar("T")
logger = logging.getLogger(__name__)


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        recovery_seconds: float = 30.0,
        should_count: Callable[[BaseException], bool] = lambda exc: True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._threshold = failure_threshold
        self._recovery = recovery_seconds
        self._should_count = should_count
        self._clock = clock
        self._failures = 0
        self._opened_at = 0.0
        self._state = BreakerState.CLOSED
        self._probe_in_flight = False
        self._lock = asyncio.Lock()

    @property
    def state(self) -> BreakerState:
        """Current state (OPEN lazily becomes HALF_OPEN once the cool-off has elapsed)."""
        if self._state is BreakerState.OPEN and self._clock() - self._opened_at >= self._recovery:
            return BreakerState.HALF_OPEN
        return self._state

    @property
    def is_open(self) -> bool:
        return self.state is BreakerState.OPEN

    async def call(self, func: Callable[[], Awaitable[T]]) -> T:
        """Run ``func`` through the breaker, or raise ``CircuitOpenError`` immediately."""
        await self._before_call()
        try:
            result = await func()
        except BaseException as exc:
            await self._record_failure(exc)
            raise
        await self._record_success()
        return result

    async def _before_call(self) -> None:
        async with self._lock:
            state = self.state
            if state is BreakerState.OPEN:
                raise CircuitOpenError(f"circuit '{self.name}' is open")
            if state is BreakerState.HALF_OPEN:
                # WHY only one probe: if the dependency is still broken, we must not let a
                # flood of requests through just because the timer elapsed.
                if self._probe_in_flight:
                    raise CircuitOpenError(f"circuit '{self.name}' is half-open (probe running)")
                self._state = BreakerState.HALF_OPEN
                self._probe_in_flight = True

    async def _record_success(self) -> None:
        async with self._lock:
            if self._state is not BreakerState.CLOSED:
                logger.info("circuit closed", extra={"breaker": self.name})
            self._state = BreakerState.CLOSED
            self._failures = 0
            self._probe_in_flight = False

    async def _record_failure(self, exc: BaseException) -> None:
        async with self._lock:
            probing = self._state is BreakerState.HALF_OPEN
            self._probe_in_flight = False
            if not self._should_count(exc):
                # e.g. a 400 "bad request" is OUR bug, not evidence the provider is unhealthy.
                if probing:
                    self._state = BreakerState.CLOSED
                return
            self._failures += 1
            if probing or self._failures >= self._threshold:
                self._state = BreakerState.OPEN
                self._opened_at = self._clock()
                logger.warning(
                    "circuit opened", extra={"breaker": self.name, "failures": self._failures}
                )
