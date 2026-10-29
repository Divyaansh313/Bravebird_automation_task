"""Bounded retries with exponential backoff and jitter.

Only retryable failures are retried:
  - our RetryableError (element not found yet, timeouts we detect, ResolveError)
  - Playwright errors (navigation timeouts, detached elements, closed targets)
FinalError subclasses (bad credentials, 2FA rejected twice, bad input) are
raised straight away. When attempts run out the last error is re-raised
wrapped in RetriesExhausted, which counts as final.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from playwright.async_api import Error as PlaywrightError

from .errors import FinalError, RetryableError

log = logging.getLogger(__name__)


class RetriesExhausted(FinalError):
    def __init__(self, stage: str, attempts: int, last: BaseException):
        self.stage = stage
        self.last = last
        super().__init__(f"{stage}: gave up after {attempts} attempts: {last}")


@dataclass
class RetryPolicy:
    attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 8.0
    jitter: float = 0.3

    def delay(self, attempt: int) -> float:
        raw = min(self.max_delay, self.base_delay * 2 ** (attempt - 1))
        return raw * (1 + random.uniform(-self.jitter, self.jitter))


@dataclass
class RetryLog:
    events: list[dict] = field(default_factory=list)


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, FinalError):
        return False
    return isinstance(exc, RetryableError | PlaywrightError | asyncio.TimeoutError)


async def with_retry[T](stage: str, fn: Callable[[int], Awaitable[T]], *,
                     policy: RetryPolicy | None = None, history: RetryLog | None = None) -> T:
    """Run `fn(attempt)` until it succeeds, a final error occurs, or attempts run out."""
    policy = policy or RetryPolicy()
    for attempt in range(1, policy.attempts + 1):
        try:
            return await fn(attempt)
        except BaseException as exc:
            retryable = is_retryable(exc)
            if history is not None:
                history.events.append({"stage": stage, "attempt": attempt,
                                       "error": type(exc).__name__, "retryable": retryable,
                                       "message": str(exc).splitlines()[0][:300] if str(exc) else ""})
            if not retryable:
                raise
            if attempt == policy.attempts:
                raise RetriesExhausted(stage, attempt, exc) from exc
            wait = policy.delay(attempt)
            log.warning("%s: attempt %d/%d failed (%s); retrying in %.1fs",
                        stage, attempt, policy.attempts, type(exc).__name__, wait)
            await asyncio.sleep(wait)
    raise AssertionError("unreachable")
