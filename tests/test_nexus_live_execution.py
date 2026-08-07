"""
The Nexus modules that can run the real pipeline must actually be given the
chance to.

`execute_omni_heal` and `execute_llm_judge` both branch on a `code` argument:
with it they call the real Scanner pipeline and report `data_source: "live"`;
without it they return a synthetic payload badged Simulated. The frontend used
to post `{}` to every endpoint, so both took the synthetic branch on every run
regardless of whether a model was reachable — the capability existed and was
never exercised.

These tests pin the distinction at the API boundary: a request carrying code
reaches the pipeline, a request without it does not. The model itself is
stubbed, because the point under test is the wiring, not the model.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

VULNERABLE = (
    "import sqlite3\n"
    "uid = request.args.get('id')\n"
    "row = conn.execute('SELECT name FROM users WHERE id = ' + uid).fetchone()\n"
)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    from backend.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def pipeline_spy(monkeypatch):
    """
    Record whether the real pipeline was entered, then fail the way an
    unreachable model fails. The orchestrator's own fallback takes over, so
    the endpoint still answers 200 with a synthetic payload — which is the
    honest degradation, not an error path.
    """
    import backend.core.ai_agent as ai
    import backend.core.god_mode_orchestrator as gmo

    entered: list[str] = []

    async def spy(code, override_model=None, language=None):
        entered.append(code)
        raise ai.AgentExecutionError("scanner", "no model reachable")

    monkeypatch.setattr(gmo, "run_pipeline", spy)
    return entered


@pytest.fixture()
def scanner_spy(monkeypatch):
    import backend.core.ai_agent as ai
    import backend.core.god_mode_orchestrator as gmo

    entered: list[str] = []

    async def spy(code, k_context=4, override_model=None, language=None):
        entered.append(code)
        raise ai.AgentExecutionError("scanner", "no model reachable")

    monkeypatch.setattr(gmo, "run_scanner", spy)
    return entered


# --------------------------------------------------------------------------- #
# MOD-01 Omni-Heal
# --------------------------------------------------------------------------- #

def test_omni_heal_with_code_enters_the_real_pipeline(client, pipeline_spy):
    r = client.post("/god-mode/simulate/error", json={"code": VULNERABLE})

    assert r.status_code == 200
    assert pipeline_spy, "run_pipeline was never called despite code being supplied"
    assert VULNERABLE in pipeline_spy[0]


def test_omni_heal_without_code_never_attempts_the_pipeline(client, pipeline_spy):
    """The regression: an empty body means the live branch is unreachable."""
    r = client.post("/god-mode/simulate/error", json={})

    assert r.status_code == 200
    assert pipeline_spy == []
    assert r.json()["data_source"] == "synthetic"


def test_omni_heal_falls_back_honestly_when_the_model_is_unreachable(client, pipeline_spy):
    """
    Supplying code must not turn a model outage into a 500, and must not let a
    synthetic payload claim to be live.
    """
    r = client.post("/god-mode/simulate/error", json={"code": VULNERABLE})

    assert r.status_code == 200
    assert r.json()["data_source"] == "synthetic"


# --------------------------------------------------------------------------- #
# MOD-04 Truth Serum
# --------------------------------------------------------------------------- #

def test_truth_serum_with_code_enters_the_real_scanner(client, scanner_spy):
    r = client.post(
        "/god-mode/simulate/hallucination",
        json={"code": VULNERABLE, "cwe_id": "CWE-89"},
    )

    assert r.status_code == 200
    assert scanner_spy, "run_scanner was never called despite code being supplied"


def test_truth_serum_without_code_never_attempts_the_scanner(client, scanner_spy):
    r = client.post("/god-mode/simulate/hallucination", json={})

    assert r.status_code == 200
    assert scanner_spy == []
    assert r.json()["data_source"] == "synthetic"


# --------------------------------------------------------------------------- #
# The three modules that genuinely take no input
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "endpoint",
    ["/god-mode/simulate/cost-spike", "/god-mode/simulate/memory-leak", "/god-mode/simulate/god-mode"],
)
def test_parameterless_modules_still_answer(client, endpoint):
    """
    These three read process and telemetry state rather than analysing input.
    Sending them a body would imply a configurability they do not have, so the
    frontend deliberately sends none — they must keep working that way.
    """
    r = client.post(endpoint, json={})
    assert r.status_code == 200
    assert r.json()["data_source"] in {"live", "local_shadow", "partial", "synthetic"}
