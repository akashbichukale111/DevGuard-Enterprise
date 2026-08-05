"""
Tests for the fault-injection evaluation's fault library, classifier, and published results.

The classifier is the part worth guarding. It scored 7/7 on the committed suite,
and the two ways that number could quietly become dishonest are:

  * it starts guessing a plausible label when it cannot actually tell
    (accuracy stays high, an on-call engineer gets sent to the wrong table);
  * it stops distinguishing "nothing is wrong" from "something is wrong and I
    cannot name it" (the control passes for the wrong reason).

Both are pinned below. The classifier fixtures are real error text from the
committed proof pack, not invented strings.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from backend.v2.faults import (
    FAULTS, NO_FAULT_EXPECTED, EvalSummary, FaultResult, RootCauseLabel, classify,
)

RESULTS = Path("examples/eval/results.json")
README = Path("examples/eval/README.md")

# Real fragments from evidence/proof-pack/eval/.
REAL_OUTPUT = {
    RootCauseLabel.COLUMN_RENAMED:
        'Database Error in model stg_users\n  column "user_id" does not exist',
    RootCauseLabel.UPSTREAM_TABLE_MISSING:
        'Database Error in model stg_orders\n  relation "raw_eval.orders" does not exist',
    RootCauseLabel.PERMISSION_DENIED:
        "Database Error in model stg_orders\n  permission denied for table orders",
    RootCauseLabel.TYPE_CHANGED:
        "Database Error in model user_order_features\n"
        "  function sum(text) does not exist",
    RootCauseLabel.NULL_CONSTRAINT_VIOLATED:
        "12 of 14 FAIL 6000 not_null_stg_orders_amount_cents ... [FAIL 6000 in 0.1s]",
}


class TestFaultLibrary:
    def test_covers_every_category_the_contract_names(self):
        """The fault-injection evaluation lists six fault types plus a control."""
        categories = {f.category for f in FAULTS}
        assert {"schema", "data-quality", "access", "control"} <= categories

    def test_size_is_within_the_contract_range(self):
        """The fault-injection evaluation: '5–8 scripted faults'."""
        assert 5 <= len(FAULTS) <= 8

    def test_there_is_exactly_one_control(self):
        controls = [f for f in FAULTS if f.category == "control"]
        assert len(controls) == 1
        assert controls[0].expected is RootCauseLabel.INSUFFICIENT_EVIDENCE

    def test_every_fault_can_revert_itself(self):
        for fault in FAULTS:
            assert fault.revert_sql, f"{fault.name} has no revert"

    def test_faults_target_the_clone_never_the_real_raw_schema(self):
        """The reference-incident substrate must be untouchable by the eval."""
        for fault in FAULTS:
            for statement in fault.inject_sql + fault.revert_sql:
                assert " raw.users" not in statement, fault.name
                assert " raw.orders" not in statement, fault.name
                assert "raw_eval" in statement or "devguard_eval" in statement, \
                    f"{fault.name}: {statement}"

    def test_silent_faults_expect_a_refusal(self):
        for fault in FAULTS:
            if fault.silent:
                assert fault.expected is RootCauseLabel.INSUFFICIENT_EVIDENCE

    def test_no_fault_expected_set_matches_the_library(self):
        assert NO_FAULT_EXPECTED == {"silent_value_drift", "control_no_fault"}


class TestClassifierRefuses:
    """Rule 1: no failure signal means no diagnosis."""

    def test_clean_run_is_insufficient_evidence(self):
        result = classify(exit_code=0, output="Done. PASS=3 WARN=0 ERROR=0 SKIP=0")
        assert result.label is RootCauseLabel.INSUFFICIENT_EVIDENCE
        assert result.failure_observed is False

    def test_clean_run_is_not_swayed_by_scary_text(self):
        """Error-shaped words in a passing run must not manufacture a diagnosis."""
        result = classify(
            exit_code=0,
            output='-- note: column "user_id" does not exist in the legacy schema\n'
                   "Done. PASS=3 WARN=0 ERROR=0 SKIP=0")
        assert result.label is RootCauseLabel.INSUFFICIENT_EVIDENCE

    def test_unrecognised_failure_is_unknown_not_a_guess(self):
        """Rule 2: never a plausible label for a message that supports none."""
        result = classify(exit_code=1, output="Compilation Error: something odd",
                          errored_models=1)
        assert result.label is RootCauseLabel.UNKNOWN_FAILURE
        assert result.label is not RootCauseLabel.INSUFFICIENT_EVIDENCE

    def test_unknown_failure_is_distinct_from_insufficient_evidence(self):
        """'Nothing is wrong' and 'I cannot name it' must never collapse."""
        assert RootCauseLabel.UNKNOWN_FAILURE is not RootCauseLabel.INSUFFICIENT_EVIDENCE


class TestClassifierLabels:
    @pytest.mark.parametrize("label", list(REAL_OUTPUT))
    def test_real_error_text_maps_to_the_right_label(self, label):
        failed = 1 if label is RootCauseLabel.NULL_CONSTRAINT_VIOLATED else 0
        errored = 0 if failed else 1
        result = classify(exit_code=1, output=REAL_OUTPUT[label],
                          errored_models=errored, failed_tests=failed)
        assert result.label is label

    def test_permission_wins_over_missing_relation(self):
        """Both messages can appear; the access failure is the actionable one."""
        combined = (REAL_OUTPUT[RootCauseLabel.PERMISSION_DENIED] + "\n"
                    + REAL_OUTPUT[RootCauseLabel.UPSTREAM_TABLE_MISSING])
        assert classify(exit_code=1, output=combined,
                        errored_models=1).label is RootCauseLabel.PERMISSION_DENIED

    def test_a_failing_test_alone_is_a_failure_signal(self):
        """Exit code can be 0-ish shapes; a FAIL line still means something broke."""
        result = classify(exit_code=1,
                          output=REAL_OUTPUT[RootCauseLabel.NULL_CONSTRAINT_VIOLATED],
                          errored_models=0, failed_tests=1)
        assert result.failure_observed and result.label is \
            RootCauseLabel.NULL_CONSTRAINT_VIOLATED

    def test_excerpt_is_captured_for_evidence(self):
        result = classify(exit_code=1, output=REAL_OUTPUT[RootCauseLabel.COLUMN_RENAMED],
                          errored_models=1)
        assert "user_id" in result.evidence_excerpt


class TestScoring:
    def make(self, expected, actual) -> FaultResult:
        return FaultResult(
            fault="f", category="c", expected=expected, actual=actual,
            correct=expected is actual, failure_observed=True, exit_code=1,
            errored_models=1, failed_tests=0, duration_s=1.0)

    def test_false_positive_is_a_claim_where_none_exists(self):
        r = self.make(RootCauseLabel.INSUFFICIENT_EVIDENCE, RootCauseLabel.COLUMN_RENAMED)
        assert r.false_positive and not r.false_negative

    def test_false_negative_is_a_refusal_on_a_real_fault(self):
        r = self.make(RootCauseLabel.COLUMN_RENAMED, RootCauseLabel.INSUFFICIENT_EVIDENCE)
        assert r.false_negative and not r.false_positive

    def test_correct_refusal_is_neither(self):
        r = self.make(RootCauseLabel.INSUFFICIENT_EVIDENCE,
                      RootCauseLabel.INSUFFICIENT_EVIDENCE)
        assert not r.false_positive and not r.false_negative

    def test_fp_rate_denominator_is_the_controls_not_everything(self):
        results = [
            self.make(RootCauseLabel.COLUMN_RENAMED, RootCauseLabel.COLUMN_RENAMED),
            self.make(RootCauseLabel.INSUFFICIENT_EVIDENCE, RootCauseLabel.TYPE_CHANGED),
            self.make(RootCauseLabel.INSUFFICIENT_EVIDENCE,
                      RootCauseLabel.INSUFFICIENT_EVIDENCE),
        ]
        summary = EvalSummary.of(results)
        assert summary.control_total == 2
        assert summary.false_positives == 1
        assert summary.false_positive_rate == 0.5


@pytest.mark.skipif(not RESULTS.exists(), reason="eval not run in this checkout")
class TestPublishedResults:
    @pytest.fixture(scope="class")
    def data(self) -> dict:
        return json.loads(RESULTS.read_text())

    def test_every_fault_in_the_library_was_run(self, data):
        assert {r["fault"] for r in data["results"]} == {f.name for f in FAULTS}

    def test_no_false_positives(self, data):
        """The metric that matters most. A regression here is the loud one."""
        assert data["false_positives"] == 0

    def test_the_control_refused(self, data):
        control = next(r for r in data["results"] if r["fault"] == "control_no_fault")
        assert control["actual"] == "INSUFFICIENT_EVIDENCE"
        assert control["failure_observed"] is False

    def test_the_silent_fault_was_genuinely_invisible(self, data):
        """If this ever produces a signal, the published limitation is wrong."""
        drift = next(r for r in data["results"] if r["fault"] == "silent_value_drift")
        assert drift["failure_observed"] is False
        assert drift["actual"] == "INSUFFICIENT_EVIDENCE"

    def test_not_every_fault_is_detectable(self, data):
        """An eval where everything is visible measures nothing about refusal."""
        assert data["detected_total"] < data["total"]

    def test_results_state_what_they_measure(self, data):
        assert "NOT a measurement of LLM diagnosis" in data["measures"]

    def test_every_result_has_raw_output(self, data):
        for r in data["results"]:
            assert r["raw_ref"], f"{r['fault']} has no captured output"


@pytest.mark.skipif(not RESULTS.exists(), reason="eval not run in this checkout")
class TestRenderedReadmeMatchesSource:
    """The generated-numbers rule's one-line diff check."""

    def test_readme_is_up_to_date(self):
        result = subprocess.run(
            [sys.executable, "scripts/render_eval_readme.py", "--check"],
            capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_readme_is_marked_generated(self):
        assert README.read_text().startswith("<!-- GENERATED FILE — DO NOT EDIT.")

    def test_readme_carries_the_weak_evidence_caveat(self):
        text = " ".join(README.read_text().split())
        assert "is not evidence that DevGuard diagnoses arbitrary incidents" in text
        assert "weak evidence by construction" in text
