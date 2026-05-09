from flask import Blueprint, request, jsonify, current_app
from functools import wraps
from typing import Callable

ratelimit_bp = Blueprint("ratelimit", __name__)


def get_client_ip() -> str:
    """Extract client IP, respecting X-Forwarded-For if present."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.remote_addr or "unknown"


def rate_limited(f: Callable) -> Callable:
    """Decorator to apply rate limiting to a Flask route."""

    @wraps(f)
    def decorated(*args, **kwargs):
        limiter = current_app.extensions.get("ratelimiter")
        if limiter is None:
            return f(*args, **kwargs)

        client_ip = get_client_ip()
        if not limiter.is_allowed(client_ip):
            return (
                jsonify({
                    "error": "Too Many Requests",
                    "message": "Rate limit exceeded. Please try again later.",
                }),
                429,
            )
        return f(*args, **kwargs)

    return decorated


def init_ratelimit(app, limiter) -> None:
    """Register the rate limiter instance with the Flask app."""
    app.extensions["ratelimiter"] = limiter
    app.register_blueprint(ratelimit_bp)


@ratelimit_bp.route("/ratelimit/status", methods=["GET"])
def ratelimit_status():
    """Return rate limit status for the current client IP."""
    limiter = current_app.extensions.get("ratelimiter")
    if limiter is None:
        return jsonify({"error": "Rate limiter not configured"}), 503

    client_ip = get_client_ip()
    remaining = limiter.remaining(client_ip)
    return jsonify({
        "ip": client_ip,
        "max_requests": limiter.max_requests,
        "window_seconds": limiter.window_seconds,
        "remaining": remaining,
    })
