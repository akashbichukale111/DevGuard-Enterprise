"""
The audit trail must record the verdict it is auditing.

SECURITY.md, "What is implemented today":

    "Tamper-evident audit trail. Every scan appends a hash-chained record
     (backend/core/audit.py); GET /audit-log/verify re-verifies the chain."

All of that is true. The chain is real, the tamper detection works, and 37 tests
in `test_audit_chain.py` demonstrate it. But the record's one substantive field —
what the pipeline actually decided — was never written.

THE DEFECT THESE TESTS EXIST FOR
--------------------------------
`_finalize` wrote the audit entry with:

    verdict=str(getattr(result, "verdict", "unknown"))

`result` is a **PipelineResult**, which has no `verdict` field — the verdict
lives at `final_validation.verdict`. So the `getattr` default won every time and
every entry recorded the literal string `"unknown"`.

Measured against the log committed in this repository:

    entries: 35
      verdict='unknown': 35

35 out of 35. A cryptographically sound, tamper-evident chain of records that
say nothing. The same `getattr(result, "verdict", "unknown")` also fed the
`verdict` field of every `POST /scan` response.

This is the fourth instance in one session of the same root cause: a function
annotated `result: ScanResult` being handed a `PipelineResult`, reading a field
that does not exist, and silently taking the `getattr` default.

NOTE ON THE EXISTING LOG: the 35 committed entries are NOT rewritten. They
honestly record what the system knew when it wrote them, and editing a
hash-chained audit log to look better is precisely the act the chain exists to
detect.
"""

from __future__ import annotations

import json

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


def _pipeline(verdict=Verdict.PASS, score=92, clean=False) -> PipelineResult:
    vulns = (
        []
        if clean
        else [
            Vulnerability(
                cwe_id="CWE-89",
                severity=Severity.CRITICAL,
                line_number=1,
                explanation="Concatenated user input reaches a SQL execute call.",
                confidence_score=0.94,
            )
        ]
    )
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
            None
            if clean
            else ValidationResult(
                eval_score=score,
                verdict=verdict,
                reasoning="The patch parameterises the query and resolves the finding.",
            )
        ),
        converged=verdict is Verdict.PASS,
    )


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit_log.jsonl"))
    return tmp_path / "audit_log.jsonl"


def _entries(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def run(coro):
    import asyncio

    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# The verdict actually reaches the record
# --------------------------------------------------------------------------- #

def test_a_passing_scan_is_audited_as_pass(log):
    """The defect, directly."""
    run(api_router._finalize("s1", ScanRequest(code=CODE), _pipeline(Verdict.PASS)))
    entry = _entries(log)[0]
    assert entry["verdict"] != "unknown", (
        "the audit trail recorded 'unknown' for a scan the Validator passed — "
        "the compliance record's one substantive field is empty"
    )
    assert entry["verdict"] == "pass"


def test_a_failing_scan_is_audited_as_fail(log):
    """The distinction that makes the record worth keeping."""
    run(api_router._finalize("s2", ScanRequest(code=CODE), _pipeline(Verdict.FAIL, 30)))
    assert _entries(log)[0]["verdict"] == "fail"


def test_the_recorded_verdict_is_the_enum_value_not_its_repr(log):
    """`str(Verdict.PASS)` on this str-subclassing enum yields 'Verdict.PASS'.

    A record reading "Verdict.PASS" is machine-hostile and betrays that the
    value was stringified rather than read. The stored value must be the wire
    value the schema defines.
    """
    run(api_router._finalize("s3", ScanRequest(code=CODE), _pipeline(Verdict.PASS)))
    v = _entries(log)[0]["verdict"]
    assert v == "pass"
    assert "Verdict." not in v


def test_a_clean_scan_is_audited_as_no_findings(log):
    """The short-circuit path runs no Validator, so there is no verdict.

    Recording "pass" would claim a Validator judgement that never happened
    (the no-fabrication rule); recording "unknown" is what the defect did for every scan and is
    indistinguishable from a broken read. It gets its own honest value.
    """
    run(api_router._finalize("s4", ScanRequest(code=CODE), _pipeline(clean=True)))
    assert _entries(log)[0]["verdict"] == "no_findings"


def test_a_result_with_no_validation_and_findings_is_audited_as_unvalidated(log):
    """Retries exhausted with no final validation is a real, distinct state."""
    p = _pipeline(Verdict.FAIL, 20)
    p.final_validation = None
    run(api_router._finalize("s5", ScanRequest(code=CODE), p))
    assert _entries(log)[0]["verdict"] == "unvalidated"


# --------------------------------------------------------------------------- #
# The same value in the API response
# --------------------------------------------------------------------------- #

def test_the_scan_response_reports_the_real_verdict(log):
    body = run(
        api_router._finalize_or_gate(
            "s6", ScanRequest(code=CODE, severity="low"), _pipeline(Verdict.PASS),
            "a" * 32,
        )
    )
    assert body["verdict"] == "pass"


def test_a_gated_scan_reports_the_real_verdict_too(log):
    body = run(
        api_router._finalize_or_gate(
            "s7", ScanRequest(code=CODE, severity="critical"),
            _pipeline(Verdict.FAIL, 40), "a" * 32,
        )
    )
    assert body["status"] == "pending_approval"
    assert body["verdict"] == "fail"


# --------------------------------------------------------------------------- #
# The chain still verifies with real verdicts in it
# --------------------------------------------------------------------------- #

def test_the_chain_verifies_across_mixed_verdicts(log):
    for i, v in enumerate([Verdict.PASS, Verdict.FAIL, Verdict.PASS]):
        run(api_router._finalize(f"s{i}", ScanRequest(code=CODE), _pipeline(v)))

    report = audit.verify_chain()
    assert report["valid"] is True
    assert report["entries_checked"] == 3
    assert [e["verdict"] for e in _entries(log)] == ["pass", "fail", "pass"]


def test_tampering_with_a_recorded_verdict_is_detected(log):
    """The verdict is now the field most worth tampering with — flipping a
    'fail' to a 'pass' is the attack the chain exists to catch. Now that the
    field carries real data, prove the detection covers it."""
    run(api_router._finalize("s1", ScanRequest(code=CODE), _pipeline(Verdict.FAIL, 20)))
    run(api_router._finalize("s2", ScanRequest(code=CODE), _pipeline(Verdict.PASS)))

    entries = _entries(log)
    assert entries[0]["verdict"] == "fail"
    entries[0]["verdict"] = "pass"
    log.write_text("".join(json.dumps(e) + "\n" for e in entries))

    report = audit.verify_chain()
    assert report["valid"] is False
    assert report["broken_at"] == 0
    assert "entry_hash mismatch" in report["reason"]


def test_a_rejected_fix_is_audited_as_rejected(log):
    """/reject writes its own entry; that path already passed a literal and must
    keep doing so."""
    run(audit.append_entry("s9", "a" * 64, "rejected"))
    assert _entries(log)[0]["verdict"] == "rejected"
