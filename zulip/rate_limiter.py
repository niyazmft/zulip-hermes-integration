"""Per-sender sliding-window rate limiter for Zulip messages.

Prevents a single user from flooding the bot with messages,
exhausting CPU, memory, or Zulip API quota.

Uses a sliding window approach: tracks timestamps per sender
and rejects messages that exceed the configured rate.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_MAX_PER_MINUTE = 60
DEFAULT_WINDOW_SECONDS = 60


class RateLimiter:
    """Sliding-window rate limiter keyed by sender identifier.

    Thread-safe for asyncio use (single-threaded event loop).
    """

    def __init__(
        self,
        max_per_minute: int = DEFAULT_MAX_PER_MINUTE,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
    ):
        self.max_per_window = max_per_minute
        self.window_seconds = window_seconds
        # sender_key -> list of timestamps (seconds since epoch)
        self._buckets: dict[str, list[float]] = defaultdict(list)

    def _prune(self, key: str, now: float) -> None:
        """Remove timestamps outside the current window."""
        cutoff = now - self.window_seconds
        bucket = self._buckets.get(key)
        if bucket:
            # Prune expired entries from the front
            while bucket and bucket[0] < cutoff:
                bucket.pop(0)
            if not bucket:
                del self._buckets[key]

    def check(self, key: str) -> bool:
        """Check if a request from this key is allowed.

        Returns True if allowed, False if rate-limited.
        """
        now = time.time()
        self._prune(key, now)
        bucket = self._buckets[key]
        if len(bucket) >= self.max_per_window:
            logger.debug(
                "rate limit hit [key=%s count=%d max=%d window=%ds]",
                key,
                len(bucket),
                self.max_per_window,
                self.window_seconds,
            )
            return False
        bucket.append(now)
        return True

    def remaining(self, key: str) -> int:
        """Return how many more requests are allowed in the current window."""
        now = time.time()
        self._prune(key, now)
        bucket = self._buckets.get(key, [])
        return max(0, self.max_per_window - len(bucket))

    def reset(self, key: Optional[str] = None) -> None:
        """Reset rate limit state for a key, or all keys if None."""
        if key:
            self._buckets.pop(key, None)
        else:
            self._buckets.clear()

    @property
    def config(self) -> dict:
        return {
            "max_per_minute": self.max_per_window,
            "window_seconds": self.window_seconds,
        }
