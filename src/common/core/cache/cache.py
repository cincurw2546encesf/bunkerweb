"""Simple in-memory cache module with TTL support for BunkerWeb."""

import time
import threading
from typing import Any, Optional


class Cache:
    """Thread-safe in-memory cache with TTL (time-to-live) expiration.

    Provides a simple key-value store where entries automatically expire
    after a configurable TTL. A background cleanup thread removes stale
    entries periodically to prevent unbounded memory growth.
    """

    def __init__(self, default_ttl: float = 300.0, cleanup_interval: float = 60.0):
        """Initialize the cache.

        Args:
            default_ttl: Default time-to-live in seconds for cache entries.
            cleanup_interval: How often (in seconds) the cleanup thread runs.

        Raises:
            ValueError: If default_ttl or cleanup_interval are not positive.
        """
        if default_ttl <= 0:
            raise ValueError("default_ttl must be a positive number")
        if cleanup_interval <= 0:
            raise ValueError("cleanup_interval must be a positive number")

        self._default_ttl = default_ttl
        self._cleanup_interval = cleanup_interval
        # Store: key -> (value, expiry_timestamp)
        self._store: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()

        # Start background cleanup thread
        self._stop_event = threading.Event()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="cache-cleanup"
        )
        self._cleanup_thread.start()

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Store a value in the cache.

        Args:
            key: Cache key.
            value: Value to store.
            ttl: Optional TTL override in seconds. Uses default_ttl if not provided.
        """
        expiry = time.monotonic() + (ttl if ttl is not None else self._default_ttl)
        with self._lock:
            self._store[key] = (value, expiry)

    def get(self, key: str, default: Any = None) -> Any:
        """Retrieve a value from the cache.

        Args:
            key: Cache key to look up.
            default: Value to return if the key is missing or expired.

        Returns:
            The cached value, or *default* if not found / expired.
        """
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return default
            value, expiry = entry
            if time.monotonic() > expiry:
                del self._store[key]
                return default
            return value

    def delete(self, key: str) -> bool:
        """Remove a key from the cache.

        Args:
            key: Cache key to remove.

        Returns:
            True if the key existed and was removed, False otherwise.
        """
        with self._lock:
            return self._store.pop(key, None) is not None

    def clear(self) -> None:
        """Remove all entries from the cache."""
        with self._lock:
            self._store.clear()

    def size(self) -> int:
        """Return the number of (potentially expired) entries currently held."""
        with self._lock:
            return len(self._store)

    def _cleanup(self) -> int:
        """Remove expired entries from the store.

        Returns:
            Number of entries removed.
        """
        now = time.monotonic()
        with self._lock:
            expired_keys = [
                k for k, (_, expiry) in self._store.items() if now > expiry
            ]
            for k in expired_keys:
                del self._store[k]
        return len(expired_keys)

    def _cleanup_loop(self) -> None:
        """Background loop that periodically purges expired entries."""
        while not self._stop_event.wait(self._cleanup_interval):
            self._cleanup()

    def shutdown(self) -> None:
        """Stop the background cleanup thread gracefully."""
        self._stop_event.set()
        self._cleanup_thread.join(timeout=5)
