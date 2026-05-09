#!/usr/bin/env python3

from logging import getLogger
from time import time
from typing import Any

logger = getLogger("bunkerweb.healthcheck")


class HealthCheck:
    """Provides health check status for BunkerWeb services."""

    def __init__(self, version: str = "unknown"):
        self._version = version
        self._start_time = time()
        self._checks: dict[str, bool] = {}

    def register_check(self, name: str, status: bool = True) -> None:
        """Register or update a named health check component."""
        self._checks[name] = status
        logger.debug(f"Health check '{name}' registered with status: {status}")

    def update_check(self, name: str, status: bool) -> None:
        """Update the status of an existing health check."""
        if name not in self._checks:
            logger.warning(f"Health check '{name}' not found, registering it.")
        self._checks[name] = status

    def is_healthy(self) -> bool:
        """Return True only if all registered checks pass."""
        return all(self._checks.values()) if self._checks else True

    def get_status(self) -> dict[str, Any]:
        """Return a structured status report."""
        uptime = round(time() - self._start_time, 2)
        return {
            "healthy": self.is_healthy(),
            "version": self._version,
            "uptime_seconds": uptime,
            "checks": dict(self._checks),
        }

    def reset(self) -> None:
        """Clear all registered checks."""
        self._checks.clear()
        logger.debug("All health checks cleared.")
