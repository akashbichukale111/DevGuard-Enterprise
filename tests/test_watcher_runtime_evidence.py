"""
Tests for the Watcher — the deterministic detection agent.

The Watcher is the only source of `RUNTIME` evidence, and the evidence rule will not let a root
cause form without it. So the properties that matter are:

  * it reports what happened, including "nothing happened";
  * it cannot be told what to find;
  * its parsing survives real dbt output, including the SKIP line, which is the
    signal that distinguishes this incident (drift) from an outage.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

from backend.v2.agents.watcher import (
    RuntimeEvidenceProvider, RuntimeFailure, Watcher, _readable_command,
)
from backend.v2.evidence import EvidenceBuilder, EvidenceSource, EvidenceTrust
from backend.v2.handoff import AgentDecision
from backend.v2.proofpack import ProofPack

#: Real dbt 1.12.0 output from evidence/d3/break/03-AFTER-dbt-failure.txt.
REAL_DBT_FAILURE = """\
02:16:58  Running with dbt=1.12.0
02:16:59  1 of 3 START sql view model analytics_staging.stg_orders ....................... [RUN]
02:16:59  2 of 3 ERROR creating sql view model analytics_staging.stg_users ............... [ERROR in 0.16s]
02:16:59  3 of 3 SKIP relation analytics_marts.user_order_features ....................... [SKIP]
02:16:59  Completed with 1 error, 0 partial successes, and 0 warnings:
02:16:59  [ERROR]: in model stg_users (models/staging/stg_users.sql)
02:16:59    Database Error in model stg_users (models/staging/stg_users.sql)
  column "user_id" does not exist
  LINE 13:     user_id,
               ^
02:16:59  Done. PASS=1 WARN=0 ERROR=1 SKIP=1 NO-OP=0 REUSED=0 TOTAL=3
"""

REAL_DBT_SUCCESS = """\
02:16:40  Completed successfully
02:16:40  Done. PASS=3 WARN=0 ERROR=0 SKIP=0 NO-OP=0 REUSED=0 TOTAL=3
"""


def observe_text(tmp_path, text: str, rc: int) -> RuntimeFailure:
    """Run a real subprocess that reproduces the given output and exit code.

    Deliberately a real process rather than a mock: the provider's contract is
    "run something and report what happened", and a mocked subprocess would not
    exercise the part most likely to break.
    """
    script = tmp_path / "fake.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.write({text!r})\n"
        f"sys.exit({rc})\n"
    )
    return RuntimeEvidenceProvider(cwd=tmp_path).observe([sys.executable, str(script)])


class TestParsing:
    def test_extracts_everything_from_real_dbt_failure(self, tmp_path):
        f = observe_text(tmp_path, REAL_DBT_FAILURE, 1)
        assert f.exit_code == 1
        assert f.failing_model == "stg_users"
        assert f.failing_model_path == "models/staging/stg_users.sql"
        assert f.missing_column == "user_id"
        assert (f.passed, f.errored, f.skipped) == (1, 1, 1)
        assert f.is_failure

    def test_skip_count_is_captured(self, tmp_path):
        """The SKIP is why this is drift, not an outage — a stale table survives."""
        assert observe_text(tmp_path, REAL_DBT_FAILURE, 1).skipped == 1

    def test_success_is_reported_as_success(self, tmp_path):
        f = observe_text(tmp_path, REAL_DBT_SUCCESS, 0)
        assert not f.is_failure
        assert f.failing_model is None and f.missing_column is None
        assert (f.passed, f.errored, f.skipped) == (3, 0, 0)

    def test_ansi_colour_codes_do_not_defeat_parsing(self, tmp_path):
        coloured = REAL_DBT_FAILURE.replace(
            "[ERROR]", "\x1b[31m[ERROR]\x1b[0m").replace(
            "Done.", "\x1b[32mDone.\x1b[0m")
        f = observe_text(tmp_path, coloured, 1)
        assert f.failing_model == "stg_users"
        assert f.errored == 1

    def test_nonzero_exit_with_unparseable_output_is_still_a_failure(self, tmp_path):
        f = observe_text(tmp_path, "segmentation fault\n", 139)
        assert f.is_failure and f.exit_code == 139
        assert f.failing_model is None


class TestCannotBeToldWhatToFind:
    def test_provider_has_no_expected_failure_parameter(self):
        import inspect
        params = set(inspect.signature(RuntimeEvidenceProvider.observe).parameters)
        assert params == {"self", "command", "env"}

    def test_watcher_refuses_to_open_an_incident_on_a_passing_command(self, tmp_path):
        f = observe_text(tmp_path, REAL_DBT_SUCCESS, 0)
        builder = EvidenceBuilder("T", clock=lambda: datetime.now(timezone.utc))
        _, handoff = Watcher(builder, ProofPack(tmp_path, "r")).run(f)
        assert handoff.decision is AgentDecision.INSUFFICIENT_EVIDENCE
        assert "the no-fabrication rule" in handoff.rationale


class TestEvidence:
    def test_emits_runtime_trusted_observed(self, tmp_path):
        f = observe_text(tmp_path, REAL_DBT_FAILURE, 1)
        builder = EvidenceBuilder("T", clock=lambda: datetime.now(timezone.utc))
        items, _ = Watcher(builder, ProofPack(tmp_path, "r")).run(f)
        assert items
        for e in items:
            assert e.source is EvidenceSource.RUNTIME
            assert e.trust is EvidenceTrust.TRUSTED_SYSTEM

    def test_one_claim_per_distinct_fact(self, tmp_path):
        f = observe_text(tmp_path, REAL_DBT_FAILURE, 1)
        builder = EvidenceBuilder("T", clock=lambda: datetime.now(timezone.utc))
        items, _ = Watcher(builder, ProofPack(tmp_path, "r")).run(f)
        claims = " | ".join(e.claim for e in items)
        assert "exited 1" in claims
        assert "stg_users" in claims
        assert "user_id" in claims
        assert "SKIPped" in claims

    def test_every_item_points_at_captured_output(self, tmp_path):
        f = observe_text(tmp_path, REAL_DBT_FAILURE, 1)
        builder = EvidenceBuilder("T", clock=lambda: datetime.now(timezone.utc))
        items, _ = Watcher(builder, ProofPack(tmp_path, "r")).run(f)
        for e in items:
            assert open(e.raw_ref).read().strip()

    def test_deterministic_agents_report_no_tokens_or_model(self, tmp_path):
        f = observe_text(tmp_path, REAL_DBT_FAILURE, 1)
        builder = EvidenceBuilder("T", clock=lambda: datetime.now(timezone.utc))
        _, handoff = Watcher(builder, ProofPack(tmp_path, "r")).run(f)
        assert handoff.tokens == 0 and handoff.model is None


class TestReproducibleCommandRendering:
    @pytest.mark.parametrize("argv,expected", [
        (["/tmp/x/venv/bin/dbt", "run"], "dbt run"),
        (["dbt", "run"], "dbt run"),
        (["/usr/bin/python3", "train.py"], "python3 train.py"),
    ])
    def test_interpreter_path_is_stripped(self, argv, expected):
        """Evidence must read the same on a judge's machine as on ours."""
        assert _readable_command(argv) == expected

    def test_only_the_interpreter_is_shortened(self):
        assert _readable_command(["/x/dbt", "run", "--select", "path/to/model.sql"]) \
            == "dbt run --select path/to/model.sql"
