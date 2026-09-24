"""Shared retry-with-backoff helper used by every provider adapter.

Retries only a call classified as retryable (429 rate-limit or 5xx server
error) -- never a 4xx that isn't 429 (that's a bad request; retrying it
just repeats the same failure), and never a *completed* turn. A turn is
either fully retried at the transport level before it produces a
TurnResult, or it isn't retried at all.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def call_with_backoff(
    fn: Callable[[], T],
    is_retryable: Callable[[Exception], bool],
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[T, int]:
    """Call fn(), retrying with exponential backoff + jitter on a retryable
    error only.

    Returns (result, retry_count). Raises the last exception once
    max_retries is exhausted, or immediately if is_retryable(exc) is False.
    """
    attempt = 0
    while True:
        try:
            return fn(), attempt
        except Exception as exc:
            if attempt >= max_retries or not is_retryable(exc):
                raise
            delay = min(base_delay * (2**attempt), max_delay)
            delay += random.uniform(0, delay * 0.1)
            sleep(delay)
            attempt += 1
