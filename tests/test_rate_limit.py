"""
The endpoints that spend money must be rate limited; the ones that do not,
must not be.

A scan is several model calls, and a project scan is that multiplied by up to
25 files. Exposed publicly with no limit, one script is an unbounded bill and
a denial of service against the provider quota every other user shares.

The read endpoints are deliberately excluded. `/slo-status` is a liveness
probe that a platform polls on a schedule, and rate-limiting a health check is
how a service gets marked unhealthy for being popular.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend.core.ratelimit import FixedWindowLimiter, scan_limiter


# --------------------------------------------------------------------------- #
# The limiter itself
# --------------------------------------------------------------------------- #

def test_requests_under_the_limit_are_allowed():
    lim = FixedWindowLimiter(limit=3, window_s=60)
    for _ in range(3):
        allowed, retry = lim.check("client-a")
        assert allowed and retry == 0


def test_the_request_over_the_limit_is_refused():
    lim = FixedWindowLimiter(limit=3, window_s=60)
    for _ in range(3):
        lim.check("client-a")
    allowed, retry = lim.check("client-a")
    assert not allowed
    assert retry > 0, "a refusal must say how long to wait"


def test_clients_do_not_share_an_allowance():
    lim = FixedWindowLimiter(limit=2, window_s=60)
    lim.check("a")
    lim.check("a")
    assert lim.check("a")[0] is False
    # A different client is unaffected — the whole point of keying per client.
    assert lim.check("b")[0] is True


def test_the_window_rolls_over():
    lim = FixedWindowLimiter(limit=2, window_s=60)
    lim.check("a", now=1000.0)
    lim.check("a", now=1001.0)
    assert lim.check("a", now=1002.0)[0] is False
    # Past the window, the allowance resets.
    assert lim.check("a", now=1061.0)[0] is True


def test_a_zero_limit_disables_the_limiter():
    """0 is the documented off switch for local development and the suite."""
    lim = FixedWindowLimiter(limit=0, window_s=60)
    for _ in range(500):
        assert lim.check("a")[0] is True


def test_retry_after_never_advertises_zero_seconds():
    """A Retry-After of 0 invites an immediate retry that will also fail."""
    lim = FixedWindowLimiter(limit=1, window_s=60)
    lim.check("a", now=1000.0)
    _, retry = lim.check("a", now=1059.9)
    assert retry >= 1


def test_tracking_is_bounded():
    """The limiter must not become the memory leak it exists to prevent."""
    from backend.core import ratelimit

    lim = FixedWindowLimiter(limit=5, window_s=60)
    for i in range(ratelimit.MAX_TRACKED_CLIENTS + 100):
        lim.check(f"client-{i}")
    assert len(lim._windows) <= ratelimit.MAX_TRACKED_CLIENTS + 1


# --------------------------------------------------------------------------- #
# Client identity
# --------------------------------------------------------------------------- #

def test_the_forwarded_client_is_preferred_over_the_proxy():
    """
    Behind a reverse proxy every caller shares one source address, so without
    this every user would share one bucket.
    """
    from backend.core.ratelimit import client_key

    class _Req:
        headers = {"x-forwarded-for": "203.0.113.7, 10.0.0.1"}
        client = type("C", (), {"host": "10.0.0.1"})()

    assert client_key(_Req()) == "203.0.113.7"


def test_direct_callers_fall_back_to_the_socket_address():
    from backend.core.ratelimit import client_key

    class _Req:
        headers: dict[str, str] = {}
        client = type("C", (), {"host": "198.51.100.4"})()

    assert client_key(_Req()) == "198.51.100.4"


# --------------------------------------------------------------------------- #
# The HTTP surface
# --------------------------------------------------------------------------- #

@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    from fastapi.testclient import TestClient
    from backend.main import app

    scan_limiter.reset()
    with TestClient(app) as c:
        yield c
    scan_limiter.reset()


def test_scan_returns_429_with_retry_after(client, monkeypatch):
    monkeypatch.setattr(scan_limiter, "limit", 2)
    scan_limiter.reset()

    body = {"code": "import os\n", "language": "python"}
    for _ in range(2):
        # 503 is expected without a model; what matters is that it is not 429.
        assert client.post("/scan", json=body).status_code != 429

    r = client.post("/scan", json=body)
    assert r.status_code == 429
    assert r.headers["Retry-After"]
    detail = r.json()["detail"]
    assert detail["limit"] == 2
    assert detail["retry_after_seconds"] >= 1


def test_health_and_read_endpoints_are_never_limited(client, monkeypatch):
    """A liveness probe must not be throttled for being polled."""
    monkeypatch.setattr(scan_limiter, "limit", 1)
    scan_limiter.reset()

    for _ in range(25):
        assert client.get("/slo-status").status_code == 200
        assert client.get("/languages").status_code == 200


def test_nexus_modules_are_not_limited(client, monkeypatch):
    """
    The god-mode endpoints do not call a model unless given code, and the UI
    fires all five at once. Limiting them would break the concurrent run that
    is the module's entire point.
    """
    monkeypatch.setattr(scan_limiter, "limit", 1)
    scan_limiter.reset()

    for _ in range(6):
        assert client.post("/god-mode/simulate/cost-spike", json={}).status_code == 200
