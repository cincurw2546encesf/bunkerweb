import pytest
from flask import Flask
from src.common.core.ratelimit.ratelimit import RateLimit
from src.common.core.ratelimit.ratelimit_route import init_ratelimit


@pytest.fixture
def limiter():
    return RateLimit(max_requests=3, window_seconds=60)


@pytest.fixture
def app(limiter):
    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    init_ratelimit(flask_app, limiter)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def test_invalid_max_requests():
    with pytest.raises(ValueError, match="max_requests"):
        RateLimit(max_requests=0)


def test_invalid_window_seconds():
    with pytest.raises(ValueError, match="window_seconds"):
        RateLimit(window_seconds=-1)


def test_allows_within_limit(limiter):
    for _ in range(3):
        assert limiter.is_allowed("192.168.1.1") is True


def test_blocks_over_limit(limiter):
    for _ in range(3):
        limiter.is_allowed("10.0.0.1")
    assert limiter.is_allowed("10.0.0.1") is False


def test_remaining_decrements(limiter):
    assert limiter.remaining("172.16.0.1") == 3
    limiter.is_allowed("172.16.0.1")
    assert limiter.remaining("172.16.0.1") == 2


def test_reset_specific_key(limiter):
    limiter.is_allowed("1.2.3.4")
    limiter.is_allowed("1.2.3.4")
    limiter.reset("1.2.3.4")
    assert limiter.remaining("1.2.3.4") == 3


def test_reset_all_keys(limiter):
    limiter.is_allowed("a.b.c.d")
    limiter.is_allowed("e.f.g.h")
    limiter.reset()
    assert limiter.remaining("a.b.c.d") == 3
    assert limiter.remaining("e.f.g.h") == 3


def test_status_endpoint(client):
    response = client.get("/ratelimit/status", environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert response.status_code == 200
    data = response.get_json()
    assert "remaining" in data
    assert data["max_requests"] == 3
    assert data["window_seconds"] == 60


def test_independent_keys(limiter):
    for _ in range(3):
        limiter.is_allowed("host-a")
    assert limiter.is_allowed("host-a") is False
    assert limiter.is_allowed("host-b") is True
