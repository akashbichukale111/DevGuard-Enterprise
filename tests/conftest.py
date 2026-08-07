"""
Shared fixtures.

Kept deliberately small: a conftest that grows becomes hidden global state
that every test inherits whether it wants it or not.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_scan_rate_limiter():
    """
    Give every test a fresh rate-limit allowance.

    The limiter is process-global and keyed by client, and the whole suite
    presents as one client. Without this, tests that legitimately drive the
    scan endpoints repeatedly — the SSRF allowlist cases post to
    /scan/repository once per rejected URL — exhaust the window and later
    tests start seeing 429 instead of the status they assert on. The failure
    surfaces as an unrelated test breaking, which is the worst kind.

    Resetting rather than disabling keeps the limiter genuinely active, so
    tests/test_rate_limit.py still exercises the real object rather than a
    no-op.
    """
    from backend.core.ratelimit import scan_limiter

    scan_limiter.reset()
    yield
    scan_limiter.reset()
