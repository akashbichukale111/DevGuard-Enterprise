"""
DataHub configuration is detected and validated, or honestly reported absent.

Three audit findings drove this, and each has tests here:

1. `DATAHUB_TOKEN` was read by nothing. The client reads `DATAHUB_GMS_TOKEN`
   and `DATAHUB_TOKEN_FILE`; `DATAHUB_TOKEN` is the name most people export
   first, and setting it produced a silently unauthenticated client.
2. `make doctor` had no DataHub check at all — the one dependency the flagship
   module is built around.
3. Reachable and authorised are different questions. Conflating them sends an
   operator to debug the wrong layer.

The rule every test below defends: no failure state may read as success.
"""

from __future__ import annotations

import json

import pytest

from backend.v2 import datahub_preflight as pf
from backend.v2.datahub_preflight import Status


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("DATAHUB_GMS_URL", "DATAHUB_GRAPHQL_URL", "DATAHUB_TOKEN",
                "DATAHUB_GMS_TOKEN", "DATAHUB_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------- #
# Token resolution — finding 1
# --------------------------------------------------------------------------- #

def test_datahub_token_is_read(monkeypatch):
    """The finding, directly: this name previously reached nothing."""
    monkeypatch.setenv("DATAHUB_TOKEN", "tok-a")
    assert pf.resolve_token() == ("tok-a", "DATAHUB_TOKEN")


def test_the_legacy_name_still_works(monkeypatch):
    """Dropping a name that used to work is its own outage."""
    monkeypatch.setenv("DATAHUB_GMS_TOKEN", "tok-b")
    assert pf.resolve_token() == ("tok-b", "DATAHUB_GMS_TOKEN")


def test_the_explicit_name_wins_when_both_are_set(monkeypatch):
    monkeypatch.setenv("DATAHUB_TOKEN", "tok-a")
    monkeypatch.setenv("DATAHUB_GMS_TOKEN", "tok-b")
    token, source = pf.resolve_token()
    assert (token, source) == ("tok-a", "DATAHUB_TOKEN")


def test_a_token_file_is_read(monkeypatch, tmp_path):
    p = tmp_path / "tok"
    p.write_text("  tok-file\n")
    monkeypatch.setenv("DATAHUB_TOKEN_FILE", str(p))
    token, source = pf.resolve_token()
    assert token == "tok-file"
    assert str(p) in source


def test_an_unreadable_token_file_is_reported_not_swallowed(monkeypatch, tmp_path):
    monkeypatch.setenv("DATAHUB_TOKEN_FILE", str(tmp_path / "missing"))
    token, source = pf.resolve_token()
    assert token is None
    assert "unreadable" in source


def test_whitespace_only_values_do_not_count_as_a_token(monkeypatch):
    monkeypatch.setenv("DATAHUB_TOKEN", "   ")
    assert pf.resolve_token() == (None, None)


def test_no_token_anywhere(monkeypatch):
    assert pf.resolve_token() == (None, None)


# --------------------------------------------------------------------------- #
# URL resolution
# --------------------------------------------------------------------------- #

def test_graphql_url_is_derived_from_gms(monkeypatch):
    monkeypatch.setenv("DATAHUB_GMS_URL", "http://gms:8080/")
    assert pf.resolve_graphql_url() == "http://gms:8080/api/graphql"


def test_an_explicit_graphql_url_overrides_the_derivation(monkeypatch):
    """A deployment may put GraphQL behind a different host or path."""
    monkeypatch.setenv("DATAHUB_GMS_URL", "http://gms:8080")
    monkeypatch.setenv("DATAHUB_GRAPHQL_URL", "https://edge/graphql")
    assert pf.resolve_graphql_url() == "https://edge/graphql"


def test_no_gms_means_no_graphql_url(monkeypatch):
    assert pf.resolve_graphql_url() is None


# --------------------------------------------------------------------------- #
# The three failure states — none may read as success
# --------------------------------------------------------------------------- #

def test_absent_configuration_is_not_configured(monkeypatch):
    report = pf.preflight()
    assert report.status is Status.NOT_CONFIGURED
    assert not report.usable
    assert "replays committed proof packs without one" in report.detail


def test_an_unreachable_gms_is_reported_as_unreachable(monkeypatch):
    monkeypatch.setenv("DATAHUB_GMS_URL", "http://gms:8080")

    def _boom(url, token, timeout):
        raise ConnectionRefusedError("no route")

    monkeypatch.setattr(pf, "_get", _boom)
    report = pf.preflight()
    assert report.status is Status.UNREACHABLE
    assert not report.usable
    assert "ConnectionRefusedError" in report.detail


def test_a_non_200_config_is_not_a_gms(monkeypatch):
    monkeypatch.setenv("DATAHUB_GMS_URL", "http://gms:8080")
    monkeypatch.setattr(pf, "_get", lambda u, t, x: (404, "nope"))
    report = pf.preflight()
    assert report.status is Status.UNREACHABLE
    assert "does not look like a GMS" in report.detail


def test_a_rejected_token_is_unauthenticated_not_unreachable(monkeypatch):
    """
    The distinction that matters. A reachable GMS with a bad token is a
    credential problem; reporting it as unreachable sends the operator to
    debug networking.
    """
    monkeypatch.setenv("DATAHUB_GMS_URL", "http://gms:8080")
    monkeypatch.setenv("DATAHUB_TOKEN", "bad")
    monkeypatch.setattr(pf, "_get", lambda u, t, x: (200, "{}"))
    monkeypatch.setattr(pf, "_post_graphql", lambda u, t, q, x: (401, "unauthorized"))

    report = pf.preflight()
    assert report.status is Status.UNAUTHENTICATED
    assert not report.usable
    assert "DATAHUB_TOKEN" in report.detail
    assert "different problems" in report.detail


def test_graphql_errors_count_as_unauthenticated(monkeypatch):
    """DataHub answers 200 with an errors array for some auth failures."""
    monkeypatch.setenv("DATAHUB_GMS_URL", "http://gms:8080")
    monkeypatch.setattr(pf, "_get", lambda u, t, x: (200, "{}"))
    monkeypatch.setattr(pf, "_post_graphql",
                        lambda u, t, q, x: (200, '{"errors":[{"message":"denied"}]}'))
    assert pf.preflight().status is Status.UNAUTHENTICATED


def test_a_working_catalog_is_ok(monkeypatch):
    monkeypatch.setenv("DATAHUB_GMS_URL", "http://gms:8080")
    monkeypatch.setenv("DATAHUB_TOKEN", "good")
    monkeypatch.setattr(pf, "_get", lambda u, t, x: (200, "{}"))
    monkeypatch.setattr(pf, "_post_graphql",
                        lambda u, t, q, x: (200, '{"data":{"__typename":"Query"}}'))

    report = pf.preflight()
    assert report.status is Status.OK
    assert report.usable
    assert report.token_source == "DATAHUB_TOKEN"


def test_the_server_version_is_reported_when_present(monkeypatch):
    monkeypatch.setenv("DATAHUB_GMS_URL", "http://gms:8080")
    body = json.dumps({"versions": {"acryldata/datahub": {"version": "v1.6.0"}}})
    monkeypatch.setattr(pf, "_get", lambda u, t, x: (200, body))
    monkeypatch.setattr(pf, "_post_graphql", lambda u, t, q, x: (200, '{"data":{}}'))
    assert pf.preflight().server_version == "v1.6.0"


def test_an_unparseable_config_body_does_not_fail_the_check(monkeypatch):
    """The version is a nicety; it must not gate usability."""
    monkeypatch.setenv("DATAHUB_GMS_URL", "http://gms:8080")
    monkeypatch.setattr(pf, "_get", lambda u, t, x: (200, "not json"))
    monkeypatch.setattr(pf, "_post_graphql", lambda u, t, q, x: (200, '{"data":{}}'))
    report = pf.preflight()
    assert report.status is Status.OK
    assert report.server_version is None


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #

def test_preflight_never_raises(monkeypatch):
    """A preflight that throws is a preflight nobody runs."""
    monkeypatch.setenv("DATAHUB_GMS_URL", "http://gms:8080")

    def _explode(*a, **k):
        raise RuntimeError("catastrophe")

    monkeypatch.setattr(pf, "_get", _explode)
    assert pf.preflight().status is Status.UNREACHABLE


def test_the_probe_query_is_side_effect_free(monkeypatch):
    """
    A preflight must never mutate a catalog. Capture the query actually sent
    and assert it is a read.
    """
    sent: list[str] = []
    monkeypatch.setenv("DATAHUB_GMS_URL", "http://gms:8080")
    monkeypatch.setattr(pf, "_get", lambda u, t, x: (200, "{}"))
    monkeypatch.setattr(pf, "_post_graphql",
                        lambda u, t, q, x: (sent.append(q), (200, '{"data":{}}'))[1])
    pf.preflight()
    assert sent and sent[0].strip().startswith("query")
    assert "mutation" not in sent[0]


def test_every_status_is_distinguishable():
    """Four states, four meanings; collapsing any two loses the diagnosis."""
    assert len({s.value for s in Status}) == 4
    assert Status.OK.value == "OK"
