"""Shared retry-with-backoff helper for the real LLM providers.

Both providers call a transient upstream API (rate limits, 5xx). A provider
call raises :class:`RetryableError` to signal "worth retrying"; anything else
propagates immediately. After the configured number of attempts is exhausted,
the last error propagates so the caller (see ``anthropic_client.py`` /
``openrouter_client.py``) can fall back to the offline templates.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class RetryableError(Exception):
    """Raised by a provider call to signal a transient (429/5xx) failure."""


def call_with_retry(
    fn: Callable[[], T],
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 20.0,
) -> T:
    """Call ``fn`` up to ``attempts`` times, backing off on :class:`RetryableError`.

    Exponential backoff with jitter between attempts. Non-retryable exceptions
    propagate on the first occurrence. If every attempt raises
    :class:`RetryableError`, the last one propagates.
    """
    last_exc: RetryableError | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except RetryableError as exc:
            last_exc = exc
            if attempt == attempts - 1:
                break
            delay = min(base_delay * (2**attempt) + random.uniform(0, 0.5), max_delay)
            time.sleep(delay)
    assert last_exc is not None  # attempts >= 1 guarantees this
    raise last_exc
