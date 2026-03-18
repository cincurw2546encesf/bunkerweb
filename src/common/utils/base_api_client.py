from logging import getLogger
from time import time

from requests import Session
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError as RequestsConnectionError, Timeout
from urllib3.util.retry import Retry


class ApiClientError(Exception):
    """API returned a 4xx error."""

    def __init__(self, message, status_code=None):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class ApiUnavailableError(Exception):
    """API unreachable or returned 5xx."""

    def __init__(self, message="API unavailable"):
        self.message = message
        super().__init__(message)


class BaseApiClient:
    """Shared HTTP client base class for BunkerWeb API consumers.

    Provides connection pooling, Bearer token auth, error handling, and TTL-cached
    readonly check. Subclass this to add domain-specific API methods.
    """

    def __init__(self, base_url: str, api_token: str, timeout=30, logger_name: str = "API_CLIENT"):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._logger = getLogger(logger_name)

        self.session = Session()
        self.session.headers["Authorization"] = f"Bearer {api_token}"
        self.session.headers["Content-Type"] = "application/json"

        # Connection pooling with 1 retry on 5xx/connection errors
        retry = Retry(total=1, backoff_factor=0.5, status_forcelist=[502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self._readonly_cache = None
        self._readonly_cache_ttl = 5  # seconds
        self._readonly_cache_time = 0

    # ── Core request methods ─────────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs):
        """Central request method with error handling.

        Returns the parsed JSON response dict on success.
        Raises ApiClientError on 4xx, ApiUnavailableError on 5xx/network errors.
        """
        url = f"{self.base_url}{path}"
        kwargs.setdefault("timeout", self.timeout)

        try:
            resp = self.session.request(method, url, **kwargs)
        except (RequestsConnectionError, Timeout) as e:
            self._logger.error(f"API request failed ({method} {path}): {e}")
            raise ApiUnavailableError(f"Cannot reach API at {self.base_url}: {e}") from e

        if resp.status_code >= 500:
            msg = resp.text[:500] if resp.text else f"HTTP {resp.status_code}"
            self._logger.error(f"API server error ({method} {path}): {resp.status_code} {msg}")
            raise ApiUnavailableError(f"API returned {resp.status_code}")

        if resp.status_code >= 400:
            try:
                body = resp.json()
                msg = body.get("message", body.get("msg", resp.text[:500]))
            except Exception:
                msg = resp.text[:500] if resp.text else f"HTTP {resp.status_code}"
            raise ApiClientError(msg, status_code=resp.status_code)

        # 204 No Content or empty body
        if resp.status_code == 204 or not resp.content:
            return {"status": "success"}

        try:
            return resp.json()
        except Exception:
            return {"status": "success", "data": resp.text}

    def _get(self, path: str, **kwargs):
        return self._request("GET", path, **kwargs)

    def _post(self, path: str, **kwargs):
        return self._request("POST", path, **kwargs)

    def _put(self, path: str, **kwargs):
        return self._request("PUT", path, **kwargs)

    def _patch(self, path: str, **kwargs):
        return self._request("PATCH", path, **kwargs)

    def _delete(self, path: str, **kwargs):
        return self._request("DELETE", path, **kwargs)

    def _raw_request(self, method: str, path: str, **kwargs):
        """Like _request but returns the raw Response object (for binary downloads)."""
        url = f"{self.base_url}{path}"
        kwargs.setdefault("timeout", self.timeout)

        try:
            resp = self.session.request(method, url, **kwargs)
        except (RequestsConnectionError, Timeout) as e:
            raise ApiUnavailableError(f"Cannot reach API at {self.base_url}: {e}") from e

        if resp.status_code >= 500:
            raise ApiUnavailableError(f"API returned {resp.status_code}")
        if resp.status_code >= 400:
            try:
                body = resp.json()
                msg = body.get("message", resp.text[:500])
            except Exception:
                msg = resp.text[:500]
            raise ApiClientError(msg, status_code=resp.status_code)

        return resp

    # ── System ──────────────────────────────────────────────────────────

    @property
    def readonly(self) -> bool:
        """Check if the database is in readonly mode. Cached with short TTL."""
        now = time()
        if self._readonly_cache is not None and (now - self._readonly_cache_time) < self._readonly_cache_ttl:
            return self._readonly_cache

        try:
            data = self._get("/system/readonly")
            self._readonly_cache = data.get("readonly", False)
        except (ApiClientError, ApiUnavailableError):
            self._readonly_cache = True

        self._readonly_cache_time = now
        return self._readonly_cache

    def ping(self) -> dict:
        """Call GET /ping and return the response dict."""
        return self._get("/ping")
