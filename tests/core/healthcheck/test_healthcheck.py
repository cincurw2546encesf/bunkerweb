#!/usr/bin/env python3

import pytest
from unittest.mock import patch
from src.common.core.healthcheck.healthcheck import HealthCheck
from src.common.core.healthcheck.healthcheck_route import (
    init_healthcheck,
    get_healthcheck,
    healthcheck_bp,
)
from flask import Flask


@pytest.fixture
def hc():
    return HealthCheck(version="1.2.3")


@pytest.fixture
def app():
    flask_app = Flask(__name__)
    flask_app.register_blueprint(healthcheck_bp)
    flask_app.config["TESTING"] = True
    init_healthcheck(version="1.2.3")
    return flask_app


def test_initial_healthy(hc):
    assert hc.is_healthy() is True


def test_register_check_pass(hc):
    hc.register_check("database", True)
    assert hc.is_healthy() is True


def test_register_check_fail(hc):
    hc.register_check("database", False)
    assert hc.is_healthy() is False


def test_update_check(hc):
    hc.register_check("cache", True)
    hc.update_check("cache", False)
    assert hc.is_healthy() is False


def test_reset_clears_checks(hc):
    hc.register_check("service", False)
    hc.reset()
    assert hc.is_healthy() is True
    assert hc.get_status()["checks"] == {}


def test_get_status_structure(hc):
    hc.register_check("nginx", True)
    status = hc.get_status()
    assert "healthy" in status
    assert "version" in status
    assert status["version"] == "1.2.3"
    assert "uptime_seconds" in status
    assert "checks" in status


def test_healthz_endpoint_ok(app):
    with app.test_client() as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["healthy"] is True


def test_healthz_endpoint_unhealthy(app):
    hc = get_healthcheck()
    hc.register_check("critical", False)
    with app.test_client() as client:
        resp = client.get("/healthz")
        assert resp.status_code == 503
    hc.reset()


def test_readyz_endpoint(app):
    with app.test_client() as client:
        resp = client.get("/readyz")
        assert resp.status_code == 200
        assert resp.get_json()["ready"] is True
