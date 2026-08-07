"""
Optional API-key authentication.

Two properties matter and they pull in opposite directions:

  1. Unset `DEVGUARD_API_KEYS` must leave behaviour *exactly* as it was — the
     project's central claim is that a clean clone runs with no configuration,
     and auth that switches itself on would break every quickstart in the
     README.
  2. Set it, and the endpoints that spend money must actually refuse strangers.

Most of the tests below are about the seam between those two, because that is
where a security control silently becomes decorative.
"""

from __future__ import annotations

import pytest

from backend.core import auth


KEY = "test-key-longer-than-minimum"
OTHER = "second-key-also-long-enough"


@pytest.fixture()
def no_auth(monkeypatch):
    monkeypatch.delenv("DEVGUARD_API_KEYS", raising=False)
    auth.reload_keys()
    yield
    auth.reload_keys()


@pytest.fixture()
def with_keys(monkeypatch):
    monkeypatch.setenv("DEVGUARD_API_KEYS", f"{KEY},{OTHER}")
    auth.reload_keys()
    yield
    monkeypatch.delenv("DEVGUARD_API_KEYS", raising=False)
    auth.reload_keys()


# --------------------------------------------------------------------------- #
# Key loading
# --------------------------------------------------------------------------- #

def test_unset_means_disabled(no_auth):
    assert auth.auth_enabled() is False


def test_empty_string_means_disabled(monkeypatch):
    """`DEVGUARD_API_KEYS=` in a compose file must not read as one empty key."""
    monkeypatch.setenv("DEVGUARD_API_KEYS", "")
    auth.reload_keys()
    assert auth.auth_enabled() is False
    auth.reload_keys()


def test_whitespace_and_blanks_are_ignored(monkeypatch):
    """A trailing comma is the most common way to write this list."""
    monkeypatch.setenv("DEVGUARD_API_KEYS", f"  {KEY} , , {OTHER},")
    auth.reload_keys()
    assert auth.auth_enabled() is True
    assert len(auth.key_fingerprints()) == 2
    auth.reload_keys()


def test_short_keys_are_refused(monkeypatch):
    """
    A 4-character key fails to keys but reads as protection on a dashboard.
    Refusing it is the honest outcome.
    """
    monkeypatch.setenv("DEVGUARD_API_KEYS", "abcd")
    auth.reload_keys()
    assert auth.auth_enabled() is False
    auth.reload_keys()


def test_a_short_key_does_not_disable_a_good_one(monkeypatch):
    monkeypatch.setenv("DEVGUARD_API_KEYS", f"abcd,{KEY}")
    auth.reload_keys()
    assert auth.auth_enabled() is True
    assert len(auth.key_fingerprints()) == 1
    auth.reload_keys()


# --------------------------------------------------------------------------- #
# Fingerprints
# --------------------------------------------------------------------------- #

def test_fingerprints_never_contain_the_key(with_keys):
    joined = "".join(auth.key_fingerprints())
    assert KEY not in joined
    assert OTHER not in joined


def test_fingerprints_are_stable_and_distinct(with_keys):
    first = auth.key_fingerprints()
    auth.reload_keys()
    assert auth.key_fingerprints() == first
    assert len(set(first)) == 2


def test_no_fingerprints_when_disabled(no_auth):
    assert auth.key_fingerprints() == []


# --------------------------------------------------------------------------- #
# The HTTP surface
# --------------------------------------------------------------------------- #

@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    from fastapi.testclient import TestClient
    from backend.main import app

    with TestClient(app) as c:
        yield c


BODY = {"code": "import os\n", "language": "python"}
PROTECTED = ["/scan", "/scan/zip", "/scan/repository"]


def test_scan_is_open_when_auth_is_disabled(client, no_auth):
    """
    Without a key configured, /scan must behave as before. 503 is what a
    keyless environment returns from the pipeline; what matters is that it is
    not 401.
    """
    assert client.post("/scan", json=BODY).status_code != 401


@pytest.mark.parametrize("path", PROTECTED)
def test_protected_endpoints_refuse_an_anonymous_caller(client, with_keys, path):
    r = client.post(path, json=BODY)
    assert r.status_code == 401
    # RFC 7235: a 401 has to say how to authenticate.
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_a_bearer_token_is_accepted(client, with_keys):
    r = client.post("/scan", json=BODY, headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code != 401


def test_the_x_api_key_header_is_accepted(client, with_keys):
    r = client.post("/scan", json=BODY, headers={"X-API-Key": KEY})
    assert r.status_code != 401


def test_every_configured_key_works_not_just_the_first(client, with_keys):
    """Rotation depends on two keys being valid at once."""
    r = client.post("/scan", json=BODY, headers={"X-API-Key": OTHER})
    assert r.status_code != 401


def test_a_wrong_key_is_refused(client, with_keys):
    r = client.post("/scan", json=BODY, headers={"X-API-Key": "wrong-but-long-enough"})
    assert r.status_code == 401


def test_a_key_prefix_is_refused(client, with_keys):
    """Guards against a truncating or prefix comparison."""
    r = client.post("/scan", json=BODY, headers={"X-API-Key": KEY[:-1]})
    assert r.status_code == 401


def test_the_scheme_is_required(client, with_keys):
    """`Authorization: <key>` with no scheme must not authenticate."""
    r = client.post("/scan", json=BODY, headers={"Authorization": KEY})
    assert r.status_code == 401


def test_the_bearer_scheme_is_case_insensitive(client, with_keys):
    """RFC 7235 makes the scheme case-insensitive; clients do send 'bearer'."""
    r = client.post("/scan", json=BODY, headers={"Authorization": f"bearer {KEY}"})
    assert r.status_code != 401


def test_the_key_is_never_echoed_in_a_refusal(client, with_keys):
    """A 401 body that quotes the attempt lands the guess in access logs."""
    guess = "some-wrong-key-value-here"
    r = client.post("/scan", json=BODY, headers={"X-API-Key": guess})
    assert guess not in r.text
    assert KEY not in r.text


@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_the_approval_gate_is_protected(client, with_keys, verb):
    """
    The gate is the whole "nothing is autonomous" claim. An operator who turns
    auth on plainly expects it to cover who may approve a fix, not just who may
    request one. 404 (unknown scan_id) is the pass here — it means the request
    got past auth to the handler.
    """
    r = client.post(f"/scan/no-such-scan/{verb}", json={})
    assert r.status_code == 401

    ok = client.post(f"/scan/no-such-scan/{verb}", json={},
                     headers={"X-API-Key": KEY})
    assert ok.status_code != 401


def test_read_endpoints_stay_open(client, with_keys):
    """
    Auth guards spend, not liveness. A health check that 401s is a health check
    the platform marks unhealthy.
    """
    assert client.get("/slo-status").status_code == 200
    assert client.get("/languages").status_code == 200


def test_auth_is_checked_before_the_rate_limiter(client, with_keys, monkeypatch):
    """
    Otherwise anonymous traffic burns the allowance that legitimate callers
    share, turning a failed login into a denial of service.
    """
    from backend.core.ratelimit import scan_limiter

    monkeypatch.setattr(scan_limiter, "limit", 2)
    scan_limiter.reset()

    for _ in range(10):
        assert client.post("/scan", json=BODY).status_code == 401

    # The allowance was never touched, so a real caller still gets through.
    r = client.post("/scan", json=BODY, headers={"X-API-Key": KEY})
    assert r.status_code != 429


# --------------------------------------------------------------------------- #
# Operator visibility
# --------------------------------------------------------------------------- #

def test_slo_status_reports_that_auth_is_disabled(client, no_auth):
    ac = client.get("/slo-status").json()["access_control"]
    assert ac["api_key_auth"] == "disabled"
    assert ac["key_fingerprints"] == []


def test_slo_status_reports_that_auth_is_enabled(client, with_keys):
    """
    A deployment that believes it is protected but is not is worse than one
    that knows it is open, so the mode has to be inspectable.
    """
    ac = client.get("/slo-status").json()["access_control"]
    assert ac["api_key_auth"] == "enabled"
    assert len(ac["key_fingerprints"]) == 2


def test_slo_status_never_leaks_a_key(client, with_keys):
    assert KEY not in client.get("/slo-status").text


def test_slo_status_still_reports_the_rate_limit(client, no_auth):
    ac = client.get("/slo-status").json()["access_control"]
    assert "scan_rate_limit" in ac
    assert "scan_rate_window_seconds" in ac
