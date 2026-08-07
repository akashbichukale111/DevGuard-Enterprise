"""
A failing /scan must say WHY it failed.

Every one of these failure modes produced the same user-visible message —
"Scan pipeline failed: [scanner] LLM call failed on model <name>" — while the
provider's own error was discarded. `AgentExecutionError` stored the original
on a `cause` attribute that nothing read, and raised without `from`, so the
traceback stopped at the wrapper too. An operator seeing a 503 in production
could not tell a missing key from a rejected key from a decommissioned model
from a rate limit, and each of those has a different fix.

These tests pin the diagnosis: the cause is chained, carried, and redacted.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

import backend.core.ai_agent as ai
from backend.v2.proofpack import redact


def _raise_from_llm(monkeypatch, error: Exception):
    """Point the Groq client at something that raises `error`."""

    async def fake_create(**kwargs):
        raise error

    class _Client:
        class chat:
            class completions:
                create = staticmethod(fake_create)

    monkeypatch.setattr(ai, "groq_client", _Client)


# The five things that actually go wrong in production, each needing a
# different fix, each previously indistinguishable.
FAILURES = [
    ("missing key", RuntimeError("GROQ_API_KEY environment variable is not set.")),
    ("rejected key", Exception("Error code: 401 - {'error': {'message': 'Invalid API Key'}}")),
    ("decommissioned model", Exception("Error code: 404 - model `x` has been decommissioned")),
    ("rate limited", Exception("Error code: 429 - rate limit reached for model")),
    ("unreachable provider", ConnectionError("Connection error.")),
]


@pytest.mark.parametrize("label,error", FAILURES, ids=[f[0] for f in FAILURES])
def test_the_provider_error_survives_as_the_cause(monkeypatch, caplog, label, error):
    _raise_from_llm(monkeypatch, error)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(ai.AgentExecutionError) as excinfo:
            asyncio.run(ai.run_scanner("x = 1", language="python"))

    exc = excinfo.value
    # Chained, so a traceback shows the real error rather than stopping at the
    # wrapper. This is what `raise ... from exc` buys.
    assert exc.__cause__ is error
    # And still carried on the attribute the API layer reads.
    assert exc.cause is error
    # The provider's text reaches the log, not just "LLM call failed".
    assert str(error)[:30] in caplog.text


def test_distinct_failures_produce_distinct_causes(monkeypatch):
    """The regression itself: five failures must not look identical."""
    seen = set()
    for _, error in FAILURES:
        _raise_from_llm(monkeypatch, error)
        with pytest.raises(ai.AgentExecutionError) as excinfo:
            asyncio.run(ai.run_scanner("x = 1", language="python"))
        seen.add(str(excinfo.value.cause))
    assert len(seen) == len(FAILURES)


def test_a_key_in_the_provider_error_is_redacted(monkeypatch):
    """
    Provider errors can echo request material. The cause is surfaced to the
    caller, so it goes through the same redactor the proof packs use.
    """
    leaked = "auth failed for gsk_" + "A" * 40
    _raise_from_llm(monkeypatch, RuntimeError(leaked))

    with pytest.raises(ai.AgentExecutionError) as excinfo:
        asyncio.run(ai.run_scanner("x = 1", language="python"))

    surfaced = redact(str(excinfo.value.cause))
    assert "gsk_" not in surfaced
    assert "[REDACTED-API-KEY]" in surfaced


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


def test_scan_503_names_the_cause(client, monkeypatch):
    _raise_from_llm(
        monkeypatch,
        Exception("Error code: 401 - {'error': {'message': 'Invalid API Key'}}"),
    )
    r = client.post("/scan", json={"code": "import os\n", "language": "python"})

    assert r.status_code == 503
    detail = r.json()["detail"]
    # The original message is preserved for anyone matching on it...
    assert "Scan pipeline failed" in detail["error"]
    assert detail["trace_id"]
    # ...and the discriminator is now present alongside it.
    assert detail["cause_type"] == "Exception"
    assert "Invalid API Key" in detail["cause"]


def test_scan_503_redacts_a_key_in_the_cause(client, monkeypatch):
    _raise_from_llm(monkeypatch, RuntimeError("bad key gsk_" + "B" * 40))
    r = client.post("/scan", json={"code": "import os\n", "language": "python"})

    assert r.status_code == 503
    body = r.text
    assert "gsk_" not in body
    assert "[REDACTED-API-KEY]" in r.json()["detail"]["cause"]
