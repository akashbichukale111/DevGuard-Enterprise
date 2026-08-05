"""
Contract tests for every Pydantic boundary (the verification programme, "Contract tests for
every Pydantic boundary").

These are the cheapest credibility the repo can buy: the whole architecture
claims "strict typed contracts at every agent boundary, no regex parsing," and
until now nothing proved the constraints were actually enforced. Each test here
asserts a bound that the pipeline's correctness depends on — not that Pydantic
works, but that DevGuard's *specific* invariants hold.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.core.schemas import (
    FixResult,
    ScanRequest,
    ScanResult,
    Severity,
    ValidationResult,
    Verdict,
    Vulnerability,
)


# --------------------------------------------------------------------------- #
# Vulnerability — the scanner's output unit
# --------------------------------------------------------------------------- #

def _vuln(**overrides):
    base = dict(
        cwe_id="CWE-89",
        severity=Severity.CRITICAL,
        line_number=2,
        explanation="Raw string concatenation reaches a SQL execute call.",
        confidence_score=0.94,
    )
    base.update(overrides)
    return base


def test_vulnerability_accepts_a_well_formed_finding():
    v = Vulnerability(**_vuln())
    assert v.cwe_id == "CWE-89"
    assert v.severity is Severity.CRITICAL


@pytest.mark.parametrize("bad", [-0.1, 1.1, 2.0])
def test_confidence_score_is_bounded_to_unit_interval(bad):
    """Confidence drives the Truth Serum hallucination gate. An out-of-range
    value there would silently corrupt that decision."""
    with pytest.raises(ValidationError):
        Vulnerability(**_vuln(confidence_score=bad))


def test_severity_rejects_an_unknown_level():
    """Severity drives model-tier routing and the human-approval gate. An
    unrecognised level must fail loudly rather than route unpredictably."""
    with pytest.raises(ValidationError):
        Vulnerability(**_vuln(severity="apocalyptic"))


def test_severity_is_a_str_enum_so_it_serialises_to_a_plain_string():
    """The frontend and the audit log both read this as a string."""
    v = Vulnerability(**_vuln())
    assert v.model_dump(mode="json")["severity"] == "critical"


# --------------------------------------------------------------------------- #
# ValidationResult — the gate the reflection loop turns on
# --------------------------------------------------------------------------- #

def _validation(**overrides):
    base = dict(
        eval_score=92,
        verdict=Verdict.PASS,
        reasoning="The patch parameterises the query and no other sink remains.",
        unresolved_cwe_ids=[],
        feedback="No further action required.",
    )
    base.update(overrides)
    return base


@pytest.mark.parametrize("bad", [-1, 101, 1000])
def test_eval_score_is_bounded_0_to_100(bad):
    """eval_score is compared against EVAL_PASS_THRESHOLD to decide whether the
    loop converges. Out-of-range values would break that comparison."""
    with pytest.raises(ValidationError):
        ValidationResult(**_validation(eval_score=bad))


@pytest.mark.parametrize("ok", [0, 50, 100])
def test_eval_score_accepts_the_full_legal_range(ok):
    assert ValidationResult(**_validation(eval_score=ok)).eval_score == ok


def test_verdict_rejects_a_value_outside_the_enum():
    with pytest.raises(ValidationError):
        ValidationResult(**_validation(verdict="probably-fine"))


def test_reasoning_has_a_minimum_length():
    """Guards against a model returning an empty or one-word justification —
    the reasoning is surfaced to a human making an approval decision."""
    with pytest.raises(ValidationError):
        ValidationResult(**_validation(reasoning="ok"))


def test_verdict_and_score_are_independent():
    """schemas.py documents this deliberately: a fix can score well and still
    fail, so the two must not be collapsed into one field."""
    v = ValidationResult(**_validation(eval_score=95, verdict=Verdict.FAIL))
    assert v.eval_score == 95
    assert v.verdict is Verdict.FAIL


# --------------------------------------------------------------------------- #
# Cross-boundary: no raw dicts may cross an agent boundary
# --------------------------------------------------------------------------- #

def test_scan_result_rejects_untyped_vulnerability_dicts():
    """A raw dict that does not satisfy Vulnerability must not slip through."""
    with pytest.raises(ValidationError):
        ScanResult(
            vulnerabilities=[{"cwe_id": "CWE-89"}],  # missing required fields
            model_used="llama-3.3-70b-versatile",
            retrieved_cwe_ids=[],
        )


def test_scan_result_coerces_valid_dicts_into_typed_models():
    r = ScanResult(
        vulnerabilities=[_vuln()],
        model_used="llama-3.3-70b-versatile",
        retrieved_cwe_ids=["CWE-89"],
    )
    assert isinstance(r.vulnerabilities[0], Vulnerability)


def test_fix_result_requires_the_patched_code():
    with pytest.raises(ValidationError):
        FixResult(
            diff_summary="Parameterised the query.",
            addressed_cwe_ids=["CWE-89"],
            model_used="llama-3.3-70b-versatile",
        )


def test_scan_request_rejects_empty_code():
    """The API's front door. Empty input must not reach a paid LLM call."""
    with pytest.raises(ValidationError):
        ScanRequest(code="")


def test_scan_request_accepts_real_code():
    req = ScanRequest(code="def f():\n    return 1\n")
    assert "def f()" in req.code
