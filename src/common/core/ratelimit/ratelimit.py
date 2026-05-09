from time import time
from collections import defaultdict
from threading import Lock
from typing import Optional


class RateLimit:
    """
    Token bucket rate limiter implementation.
    Tracks request counts per key (e.g., IP address) within a sliding window.
    """

    def __init__(self, max_requests: int = 100, window_seconds: int = 60):
        if max_requests <= 0:
            raise ValueError("max_requests must be a positive integer")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be a positive integer")

        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._buckets: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def _cleanup(self, key: str, now: float) -> None:
        """Remove timestamps outside the current window."""
        cutoff = now - self.window_seconds
        self._buckets[key] = [
            ts for ts in self._buckets[key] if ts > cutoff
        ]

    def is_allowed(self, key: str) -> bool:
        """Check if a request from the given key is allowed."""
        now = time()
        with self._lock:
            self._cleanup(key, now)
            if len(self._buckets[key]) < self.max_requests:
                self._buckets[key].append(now)
                return True
            return False

    def remaining(self, key: str) -> int:
        """Return the number of remaining allowed requests for the key."""
        now = time()
        with self._lock:
            self._cleanup(key, now)
            return max(0, self.max_requests - len(self._buckets[key]))

    def reset(self, key: Optional[str] = None) -> None:
        """Reset the bucket for a specific key or all keys."""
        with self._lock:
            if key is not None:
                self._buckets.pop(key, None)
            else:
                self._buckets.clear()
