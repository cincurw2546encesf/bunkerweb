#!/usr/bin/env python3

from flask import Blueprint, Response, jsonify
from logging import getLogger
from .healthcheck import HealthCheck

logger = getLogger("bunkerweb.healthcheck.route")

healthcheck_bp = Blueprint("healthcheck", __name__)
_health_instance: HealthCheck | None = None


def init_healthcheck(version: str = "unknown") -> HealthCheck:
    """Initialize the global HealthCheck instance for this blueprint."""
    global _health_instance
    _health_instance = HealthCheck(version=version)
    logger.info(f"HealthCheck initialized for version {version}")
    return _health_instance


def get_healthcheck() -> HealthCheck:
    """Retrieve the current HealthCheck instance, creating one if needed."""
    global _health_instance
    if _health_instance is None:
        _health_instance = HealthCheck()
    return _health_instance


@healthcheck_bp.route("/healthz", methods=["GET"])
def healthz() -> Response:
    """Kubernetes/Docker-compatible health endpoint."""
    hc = get_healthcheck()
    status = hc.get_status()
    http_status = 200 if status["healthy"] else 503
    logger.debug(f"Health check requested — healthy={status['healthy']}")
    return jsonify(status), http_status


@healthcheck_bp.route("/readyz", methods=["GET"])
def readyz() -> Response:
    """Readiness probe endpoint — same logic, separate semantic."""
    hc = get_healthcheck()
    status = hc.get_status()
    http_status = 200 if status["healthy"] else 503
    return jsonify({"ready": status["healthy"]}), http_status
