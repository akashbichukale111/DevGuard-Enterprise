"""
Per-client rate limiting for the endpoints that cost money.

Why this exists
---------------
`POST /scan`, `/scan/zip` and `/scan/repository` each spend real money: a scan
is several model calls, and a project scan is that multiplied by up to 25
files. Exposed publicly with no limit, one script — or one enthusiastic
reviewer holding down a button — is an unbounded bill and a denial of service
against the provider quota every other user shares.

The read endpoints are deliberately NOT limited. `/slo-status` is a health
check that a platform polls on a schedule, and rate-limiting a liveness probe
is how a service gets marked unhealthy for being popular.

Why a fixed window and not a token bucket
-----------------------------------------
A fixed window is a few lines, has no background task, and its failure mode is
understood: a client can send up to 2x the limit across a window boundary. For
protecting a wallet that is fine — the cap that matters is the hourly one, and
being briefly generous at a boundary costs one extra scan. A token bucket
would be more precise and would need eviction, refill scheduling and more
tests for no benefit at this scale.

State is per-process. Behind multiple instances each replica enforces its own
share, so the effective limit is `limit x replicas`. That is documented rather
than solved: solving it needs Redis, and the cache layer here is already
optional-and-fails-open, so making the limiter depend on it would trade a
wallet risk for an availability risk.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass

from fastapi import HTTPException, Request

logger = logging.getLogger("devguard.ratelimit")


def _int_env(name: str, default: int) -> int:
    """Read an int from the environment, falling back on anything unparseable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default


#: Requests per window, per client, across the scan endpoints. 0 disables the
#: limiter entirely — for local development and for the test suite, which
#: drives these endpoints faster than any human would.
SCAN_LIMIT = _int_env("DEVGUARD_SCAN_RATE_LIMIT", 20)

#: Window length in seconds.
SCAN_WINDOW_S = _int_env("DEVGUARD_SCAN_RATE_WINDOW_S", 60)

#: Hard ceiling on tracked clients, so the limiter cannot itself become the
#: memory leak. Beyond this the oldest windows are dropped; a dropped window
#: means a client gets a fresh allowance, which is the safe direction to fail.
MAX_TRACKED_CLIENTS = 10_000


@dataclass
class _Window:
    started_at: float
    count: int


class FixedWindowLimiter:
    """Thread-safe fixed-window counter keyed by client identity."""

    def __init__(self, limit: int, window_s: int) -> None:
        self.limit = limit
        self.window_s = window_s
        self._windows: dict[str, _Window] = defaultdict(lambda: _Window(0.0, 0))
        self._lock = threading.Lock()

    def check(self, key: str, now: float | None = None) -> tuple[bool, int]:
        """
        Return `(allowed, retry_after_seconds)`.

        `retry_after` is 0 when allowed, and otherwise the whole seconds left
        in the current window — what belongs in a Retry-After header.
        """
        if self.limit <= 0:
            return True, 0

        mono = time.monotonic() if now is None else now
        with self._lock:
            if len(self._windows) > MAX_TRACKED_CLIENTS:
                self._evict(mono)

            w = self._windows[key]
            if mono - w.started_at >= self.window_s:
                w.started_at = mono
                w.count = 1
                return True, 0

            if w.count >= self.limit:
                return False, max(1, int(self.window_s - (mono - w.started_at)))

            w.count += 1
            return True, 0

    def _evict(self, mono: float) -> None:
        """Drop windows that have already expired. Caller holds the lock."""
        expired = [k for k, w in self._windows.items() if mono - w.started_at >= self.window_s]
        for k in expired:
            del self._windows[k]
        # If everything is still live we are genuinely over the ceiling, which
        # means an attack or a very large deployment. Clearing is the honest
        # response: allowances reset rather than the process growing forever.
        if len(self._windows) > MAX_TRACKED_CLIENTS:
            logger.warning("rate limiter tracking %d clients; resetting", len(self._windows))
            self._windows.clear()

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()


#: The limiter guarding the scan endpoints.
scan_limiter = FixedWindowLimiter(SCAN_LIMIT, SCAN_WINDOW_S)


def client_key(request: Request) -> str:
    """
    Identify the caller.

    Behind Render, Vercel or any reverse proxy, `request.client.host` is the
    proxy, so every caller would share one bucket. `X-Forwarded-For`'s first
    entry is the original client. It is spoofable by a direct caller, which is
    why this is a cost guard rather than a security control — anyone willing
    to forge headers can still get through, and the thing standing between
    them and the model is authentication, which this is not.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def enforce_scan_rate_limit(request: Request) -> None:
    """FastAPI dependency for the endpoints that spend money."""
    allowed, retry_after = scan_limiter.check(client_key(request))
    if allowed:
        return

    logger.warning("rate limit hit: client=%s path=%s", client_key(request), request.url.path)
    raise HTTPException(
        status_code=429,
        detail={
            "error": "Rate limit exceeded.",
            "limit": scan_limiter.limit,
            "window_seconds": scan_limiter.window_s,
            "retry_after_seconds": retry_after,
        },
        headers={"Retry-After": str(retry_after)},
    )
