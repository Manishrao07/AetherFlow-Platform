"""Client-side request pacing for LLM providers (a sliding one-minute window).

What: ``RateLimiter(per_minute)`` makes callers wait until sending another request would keep
them within N requests in any 60-second window.
Why: free and entry-level API keys allow only a handful of requests per minute. A deep-research
run fires several calls at once; without pacing the provider answers 429 and a short retry
cannot help because the limit is per minute. Waiting here is cheaper than failing a run.
Connects to: ``client.py`` (every provider attempt, retries included, passes through it).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable

WINDOW_SECONDS = 60.0


class RateLimiter:
    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if per_minute < 1:
            raise ValueError("per_minute must be at least 1")
        self._limit = per_minute
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque()
        # WHY a lock: parallel steps call acquire() at once; they must queue, not all see
        # the same free slot.
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Return when one more request may be sent."""
        async with self._lock:
            while True:
                now = self._clock()
                while self._sent and now - self._sent[0] >= WINDOW_SECONDS:
                    self._sent.popleft()
                if len(self._sent) < self._limit:
                    self._sent.append(now)
                    return
                await self._sleep(WINDOW_SECONDS - (now - self._sent[0]))
