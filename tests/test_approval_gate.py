"""
The human-in-the-loop approval gate (Feature #7).

README, Core Features:

    "**Human-in-the-loop approval gate** for critical/high severity fixes"

`_finalize_or_gate`'s own docstring:

    "Human-in-the-loop gate (Feature #7): critical/high severity fixes PAUSE for
     approval instead of auto-finalizing."

THE DEFECT THESE TESTS EXIST FOR
--------------------------------
The gate never opened. Not once.

    severity = str(getattr(req, "severity", "")).lower()
    gated = severity in ("critical", "high")

`req` is a `ScanRequest`, and `ScanRequest.model_fields` is exactly
`['code', 'language']` — **there is no `severity` field**. Pydantic ignores the
extra key if a caller passes one, so `getattr` returned `""` every single time
and `gated` was permanently `False`. Every scan auto-finalized, including
critical ones. `POST /scan/{id}/approve` and `/reject` were unreachable dead
code, and the "pending_approval" status was never emitted.

The design was wrong twice over. Even if the field existed, the *request* cannot
know the severity — nothing has scanned the code yet when it arrives. Severity is
determined by the Scanner, and the pipeline already computes exactly the value
needed (`_max_severity(scan.vulnerabilities)`) to route the Fixer's model. The
gate must read the finding, not the request.

This is the safety control that stops an unreviewed critical security fix from
being auto-applied. It is also the fifth instance in this session of the same
root cause: reading a field that does not exist and silently taking the `getattr`
default.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.api import router as api_router
from backend.core import audit
from backend.core.schemas import (
    FixResult,
    PipelineResult,
    ScanRequest,
    ScanResult,
    Severity,
    ValidationResult,
    Verdict,
    Vulnerability,
)

CODE = 'q = "SELECT * FROM t WHERE id = " + uid\n'


def _pipeline(*severities: Severity, verdict=Verdict.PASS) -> PipelineResult:
    vulns = [
        Vulnerability(
            cwe_id="CWE-89",
            severity=s,
            line_number=i + 1,
            explanation="Concatenated user input reaches a SQL execute call.",
            confidence_score=0.9,
        )
        for i, s in enumerate(severities)
    ]
    return PipelineResult(
        original_code=CODE,
        scan=ScanResult(vulnerabilities=vulns, model_used="llama-3.3-70b-versatile"),
        final_fix=FixResult(
            patched_code="ok",
            diff_summary="Parameterised the query.",
            addressed_cwe_ids=["CWE-89"],
            model_used="llama-3.3-70b-versatile",
        ),
        final_validation=(
            ValidationResult(
                eval_score=92,
                verdict=verdict,
                reasoning="The patch parameterises the query and resolves the finding.",
            )
            if vulns
            else None
        ),
        converged=True,
    )


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit_log.jsonl"))
    api_router._pending_approvals.clear()
    api_router._scan_results.clear()
    api_router._scan_result_stored_at.clear()
    yield
    api_router._pending_approvals.clear()
    api_router._scan_results.clear()
    api_router._scan_result_stored_at.clear()


def run(coro):
    return asyncio.run(coro)


def _gate(scan_id, pipeline):
    return run(
        api_router._finalize_or_gate(scan_id, ScanRequest(code=CODE), pipeline, "a" * 32)
    )


# --------------------------------------------------------------------------- #
# The gate opens for what it claims to gate
# --------------------------------------------------------------------------- #

def test_a_critical_finding_pauses_for_approval():
    """The defect, directly. This is the safety control the README advertises."""
    body = _gate("s1", _pipeline(Severity.CRITICAL))
    assert body["status"] == "pending_approval", (
        "a CRITICAL finding auto-finalized without human review — the approval "
        "gate never opened"
    )
    assert "s1" in api_router._pending_approvals


def test_a_high_finding_pauses_for_approval():
    assert _gate("s2", _pipeline(Severity.HIGH))["status"] == "pending_approval"


def test_the_worst_finding_decides_not_the_first():
    """A batch with one critical among lows must gate on the critical."""
    body = _gate("s3", _pipeline(Severity.LOW, Severity.CRITICAL, Severity.MEDIUM))
    assert body["status"] == "pending_approval"
    assert body["severity"] == "critical"


def test_the_gate_reports_the_severity_it_gated_on():
    assert _gate("s4", _pipeline(Severity.HIGH))["severity"] == "high"


# --------------------------------------------------------------------------- #
# ...and stays shut for what it does not
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sev", [Severity.LOW, Severity.MEDIUM])
def test_low_and_medium_finalize_immediately(sev):
    """The inverse property. A gate that stops everything is as broken as one
    that stops nothing — it would make the product unusable and push people to
    disable it."""
    body = _gate("s5", _pipeline(sev))
    assert body["status"] == "completed"
    assert "s5" not in api_router._pending_approvals


def test_a_clean_scan_finalizes_immediately():
    """Nothing was found, so there is nothing for a human to approve."""
    body = _gate("s6", _pipeline())
    assert body["status"] == "completed"
    assert body["severity"] is None


# --------------------------------------------------------------------------- #
# What a gated scan does and does not do
# --------------------------------------------------------------------------- #

def test_a_gated_scan_writes_no_audit_entry_yet():
    """The audit entry records a DECISION. Nobody has made one yet."""
    _gate("s7", _pipeline(Severity.CRITICAL))
    assert audit.count_entries() == 0


def test_a_gated_scan_reports_its_chain_state_as_pending():
    _gate("s8", _pipeline(Severity.CRITICAL))
    entry = api_router._scan_results["s8"]["audit_entry"]
    assert entry["chain_state"] == "pending"
    assert entry["chain_verified"] is None


def test_a_non_gated_scan_writes_its_audit_entry_immediately():
    _gate("s9", _pipeline(Severity.LOW))
    assert audit.count_entries() == 1
    assert api_router._scan_results["s9"]["audit_entry"]["chain_state"] == "written"


def test_approving_a_gated_scan_writes_the_audit_entry():
    """End-to-end on the control: gate, then approve, then the record exists."""
    _gate("s10", _pipeline(Severity.CRITICAL))
    assert audit.count_entries() == 0

    body = run(api_router.approve("s10"))
    assert body["status"] == "approved"
    assert body["verdict"] == "pass"
    assert audit.count_entries() == 1

    entry = api_router._scan_results["s10"]["audit_entry"]
    assert entry["chain_state"] == "written"
    assert entry["chain_verified"] is True
    assert "s10" not in api_router._pending_approvals


def test_rejecting_a_gated_scan_audits_the_rejection():
    _gate("s11", _pipeline(Severity.CRITICAL))
    body = run(api_router.reject("s11", reason="patch changes behaviour"))

    assert body["status"] == "rejected"
    assert audit.count_entries() == 1
    assert audit.read_tail(1)[0]["verdict"] == "rejected"
    assert api_router._scan_results["s11"]["status"] == "error"


def test_approving_an_unknown_scan_is_a_404():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        run(api_router.approve("never-existed"))
    assert exc.value.status_code == 404


def test_the_chain_verifies_after_a_gate_and_approve_cycle():
    _gate("s12", _pipeline(Severity.CRITICAL))
    run(api_router.approve("s12"))
    _gate("s13", _pipeline(Severity.LOW))
    _gate("s14", _pipeline(Severity.HIGH))
    run(api_router.reject("s14", reason="no"))

    report = audit.verify_chain()
    assert report["valid"] is True
    assert report["entries_checked"] == 3
    assert [e["verdict"] for e in audit.read_all()] == ["pass", "pass", "rejected"]


# --------------------------------------------------------------------------- #
# The severity source
# --------------------------------------------------------------------------- #

def test_the_gate_does_not_read_the_request():
    """`ScanRequest` has no `severity` field, and could not meaningfully have one
    — nothing has scanned the code when the request arrives. Severity is a
    finding, not an input. This pins that the gate reads the scan."""
    assert "severity" not in ScanRequest.model_fields

    # A caller passing severity="low" must not be able to talk the gate out of
    # opening on a critical finding.
    body = run(
        api_router._finalize_or_gate(
            "s15", ScanRequest(code=CODE, severity="low"),
            _pipeline(Severity.CRITICAL), "a" * 32,
        )
    )
    assert body["status"] == "pending_approval", (
        "a client-supplied severity overrode the Scanner's finding — a caller "
        "could bypass human review by asking nicely"
    )
