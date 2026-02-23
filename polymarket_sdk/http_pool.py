"""
HTTP Connection Pool Manager

Provides connection pooling for HTTP requests to reduce latency by reusing
TCP+TLS connections instead of creating new ones for each request.

Savings: ~150-300ms per request after warm-up.
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class HTTPPoolManager:
    """
    Singleton HTTP connection pool manager.

    Reuses TCP connections across requests to avoid:
    - TCP handshake (~50-100ms)
    - TLS negotiation (~100-200ms)
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_pool()
        return cls._instance

    def _init_pool(self):
        """Initialize the connection pool with optimal settings."""
        self.session = requests.Session()

        # Retry strategy for transient failures
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.3,
            status_forcelist=[429, 500, 502, 503, 504],
        )

        # Connection pool adapter
        adapter = HTTPAdapter(
            pool_connections=10,   # Number of host connection pools
            pool_maxsize=20,       # Max connections per host
            max_retries=retry_strategy,
            pool_block=False,      # Don't block on exhaustion, fail fast
        )

        self.session.mount('https://', adapter)
        self.session.mount('http://', adapter)

        # Default headers for keep-alive
        self.session.headers.update({
            'Connection': 'keep-alive',
            'Accept-Encoding': 'gzip, deflate',
        })

        # Pre-warm connections to critical endpoints
        self._prewarm()

    def _prewarm(self):
        """Pre-establish connections to reduce first-request latency."""
        endpoints = [
            'https://gamma-api.polymarket.com',
            'https://clob.polymarket.com',
            'https://data-api.polymarket.com',
        ]
        for url in endpoints:
            try:
                self.session.head(url, timeout=5)
            except Exception:
                pass  # Prewarm failure is non-fatal

    def get(self, url, **kwargs):
        """Pooled GET request."""
        return self.session.get(url, **kwargs)

    def post(self, url, **kwargs):
        """Pooled POST request."""
        return self.session.post(url, **kwargs)

    def request(self, method, url, **kwargs):
        """Pooled request with arbitrary method."""
        return self.session.request(method, url, **kwargs)

    def close(self):
        """Clean shutdown of connection pool."""
        self.session.close()


# Global singleton
_pool = None


def get_pool() -> HTTPPoolManager:
    """Get the global HTTP pool manager singleton."""
    global _pool
    if _pool is None:
        _pool = HTTPPoolManager()
    return _pool


def pooled_get(url, **kwargs):
    """Drop-in replacement for requests.get() with connection pooling."""
    return get_pool().get(url, **kwargs)


def pooled_post(url, **kwargs):
    """Drop-in replacement for requests.post() with connection pooling."""
    return get_pool().post(url, **kwargs)


def patch_py_clob_client():
    """
    Monkey-patch py_clob_client to use urllib3 connection pooling.

    Uses urllib3.PoolManager directly (NOT requests.Session) to avoid
    header mutation that breaks HMAC signatures.

    Call this ONCE at application startup, BEFORE importing ClobClient.
    The patch is safe - wrapped in try/except with graceful fallback.

    Returns:
        bool: True if patch succeeded, False otherwise
    """
    try:
        import json
        import urllib3
        from py_clob_client.http_helpers import helpers
        from py_clob_client.exceptions import PolyApiException

        # Create urllib3 pool manager - NO header mutation like requests.Session
        pool_manager = urllib3.PoolManager(
            num_pools=10,
            maxsize=20,
            retries=urllib3.Retry(total=3, backoff_factor=0.3),
        )

        # Pre-warm connections
        for url in ['https://clob.polymarket.com', 'https://gamma-api.polymarket.com']:
            try:
                pool_manager.request('HEAD', url, timeout=5)
            except Exception:
                pass

        # Store original for potential restoration
        _original_request = helpers.request

        def patched_request(endpoint: str, method: str, headers=None, data=None):
            """Patched request function using urllib3 (preserves HMAC headers)."""
            try:
                # Get signed headers from py_clob_client
                headers = helpers.overloadHeaders(method, headers)

                # Encode JSON body manually (urllib3 doesn't have json= param)
                body = None
                if data:
                    body = json.dumps(data).encode('utf-8')
                    if headers is None:
                        headers = {}
                    headers['Content-Type'] = 'application/json'

                # urllib3 request - does NOT mutate headers
                resp = pool_manager.request(
                    method=method,
                    url=endpoint,
                    headers=headers,
                    body=body
                )

                if resp.status != 200:
                    raise PolyApiException(error_msg=f"HTTP {resp.status}: {resp.data.decode('utf-8', errors='replace')}")

                try:
                    return json.loads(resp.data.decode('utf-8'))
                except json.JSONDecodeError:
                    return resp.data.decode('utf-8')

            except urllib3.exceptions.HTTPError as e:
                raise PolyApiException(error_msg=f"Request exception: {e}")

        # Apply the patch
        helpers.request = patched_request
        print("[HTTP_POOL] Patched py_clob_client with urllib3 pooling (HMAC-safe)")
        return True

    except ImportError as e:
        print(f"[HTTP_POOL] py_clob_client not installed: {e}")
        return False
    except Exception as e:
        print(f"[HTTP_POOL] Patch failed (using default): {e}")
        return False
