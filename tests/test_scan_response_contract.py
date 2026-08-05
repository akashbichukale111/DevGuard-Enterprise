"""
Contract tests for the JSON `POST /scan` hands the Result Dashboard.

`_build_frontend_result` is the single place where internal pipeline objects
become the numbers a human reads on screen. Everything it emits is presented by
`frontend/app/result/page.tsx` as measured fact: an "Accuracy benchmark proof
strip", a "Tokens Used" metric card, CVSS before/after scores to one decimal,
and a green "✅ Chain Verified" tamper-evidence badge.

THE DEFECTS THESE TESTS EXIST FOR
---------------------------------
Four values in that response were constants, not measurements:

1. `benchmark_report` was `{accuracy: 0.9, precision: 0.92, recall: 0.88,
   false_positive_rate: 0.05, sample_size: 14}` — hand-typed. The harness in
   `backend/core/benchmark.py` has never been run to an artifact, and the README
   says so in as many words ("no accuracy figures are published anywhere"),
   while the result page published them on every scan.

2. `audit_entry.chain_verified` was the literal `True`, and `prev_hash` the empty
   string. Worse, both are computed BEFORE the audit entry is written — the
   append is fired as a background task after this function returns. So the API
   asserted a verified hash chain for a record that did not exist yet. The
   frontend type even documents the field as "sourced from /audit-log/verify",
   which it was not.

3. `tokens_used` was `0` on every response, rendered as a "Tokens Used" metric.

4. `cvss_before` came from a hard-coded severity->score map (critical -> 9.5)
   and `cvss_after` was the constant `1.2`, both displayed with `.toFixed(1)`
   as though a CVSS vector had been scored.

the no-fabrication rule (never fabricate) and the measured-numbers rule (every published number comes from the
machine) both apply. The fix is not better constants — it is `None`, so the UI
can say "not measured" instead of inventing a measurement.
"""

from __future__ import annotations

import pytest

from backend.api import router as api_router
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

VULN_CODE = 'q = "SELECT * FROM t WHERE id = " + uid\ndb.execute(q)\n'


def _pipeline(severity: Severity = Severity.CRITICAL, clean: bool = False) -> PipelineResult:
    vulns = (
        []
        if clean
        else [
            Vulnerability(
                cwe_id="CWE-89",
                severity=severity,
                line_number=1,
                explanation="Raw string concatenation reaches a SQL execute call.",
                confidence_score=0.93,
            )
        ]
    )
    scan = ScanResult(
        vulnerabilities=vulns,
        model_used="llama-3.3-70b-versatile",
        retrieved_cwe_ids=["CWE-89"],
    )
    return PipelineResult(
        original_code=VULN_CODE,
        scan=scan,
        final_fix=FixResult(
            patched_code="db.execute(q, (uid,))\n",
            diff_summary="Parameterised the query.",
            addressed_cwe_ids=["CWE-89"],
            model_used="llama-3.3-70b-versatile",
        ),
        final_validation=ValidationResult(
            eval_score=91,
            verdict=Verdict.PASS,
            reasoning="The patch parameterises the query and resolves the finding.",
            unresolved_cwe_ids=[],
            feedback="Correct.",
        ),
        converged=True,
        routing_decisions={"scanner": "llama-3.3-70b-versatile"},
    )


def _build(**kw):
    return api_router._build_frontend_result(
        scan_id=kw.pop("scan_id", "scan-abc"),
        req=kw.pop("req", ScanRequest(code=VULN_CODE)),
        pipeline=kw.pop("pipeline", None) or _pipeline(),
        trace_id=kw.pop("trace_id", "0" * 32),
        latency_ms=kw.pop("latency_ms", 1234.5),
        cost=kw.pop("cost", 0.00042),
        **kw,
    )


# --------------------------------------------------------------------------- #
# 1. Benchmark figures
# --------------------------------------------------------------------------- #

def test_no_benchmark_artifact_means_no_published_accuracy(monkeypatch, tmp_path):
    """The defect, directly.

    With no benchmark artifact on disk there is no measured accuracy, so the
    response must not carry one. Anything non-null here is published on the
    result page as an "Accuracy benchmark proof strip".
    """
    monkeypatch.setattr(api_router, "BENCHMARK_ARTIFACT_PATH", str(tmp_path / "nope.json"))
    body = _build()
    assert body["benchmark_report"] is None, (
        "the harness has never run, so any figure here is invented: "
        f"{body['benchmark_report']!r}"
    )


def test_a_real_benchmark_artifact_is_published_verbatim(monkeypatch, tmp_path):
    """When a real run exists, report exactly what it measured — no rounding,
    no substitution, and carry the provenance so the UI can date it."""
    import json

    artifact = tmp_path / "benchmark_report.json"
    artifact.write_text(
        json.dumps(
            {
                "accuracy": 0.7142857142857143,
                "precision": 0.8,
                "recall": 0.6153846153846154,
                "false_positive_rate": 0.0,
                "sample_size": 14,
                "generated_at": 1750000000.0,
            }
        )
    )
    monkeypatch.setattr(api_router, "BENCHMARK_ARTIFACT_PATH", str(artifact))

    got = _build()["benchmark_report"]
    assert got is not None
    assert got["accuracy"] == 0.7142857142857143
    assert got["sample_size"] == 14
    assert got["generated_at"] == 1750000000.0


def test_a_corrupt_benchmark_artifact_publishes_nothing(monkeypatch, tmp_path):
    """A half-written artifact must degrade to "not measured", never to a
    partially-populated strip of plausible-looking numbers."""
    artifact = tmp_path / "benchmark_report.json"
    artifact.write_text("{ this is not json")
    monkeypatch.setattr(api_router, "BENCHMARK_ARTIFACT_PATH", str(artifact))
    assert _build()["benchmark_report"] is None


def test_an_artifact_missing_required_metrics_publishes_nothing(monkeypatch, tmp_path):
    """Missing keys must not become zeros: 0% recall reads as a measurement."""
    import json

    artifact = tmp_path / "benchmark_report.json"
    artifact.write_text(json.dumps({"accuracy": 0.9}))  # no precision/recall/fpr/n
    monkeypatch.setattr(api_router, "BENCHMARK_ARTIFACT_PATH", str(artifact))
    assert _build()["benchmark_report"] is None


def test_the_hardcoded_benchmark_constants_are_gone():
    """Regression guard on the source itself.

    The exact figures the result page used to show. If any reappears as a
    literal in the response builder, this fails — a constant is easy to
    reintroduce "temporarily for the demo", which is precisely how it got
    there.
    """
    import inspect

    src = inspect.getsource(api_router._build_frontend_result)
    for literal in ("0.92", "0.88", "0.05", "0.9,"):
        assert literal not in src, (
            f"hand-typed benchmark figure {literal} is back in the response builder"
        )


# --------------------------------------------------------------------------- #
# 2. The audit-chain badge
# --------------------------------------------------------------------------- #

def test_chain_verified_is_not_asserted_before_the_entry_exists():
    """The security claim, directly.

    `_finalize` appends the audit entry as a task fired AFTER this function
    returns, so at build time there is nothing to verify. Reporting True here
    renders a green "✅ Chain Verified" badge for a record that does not exist.
    """
    entry = _build()["audit_entry"]
    assert entry["chain_verified"] is not True, (
        "response claims a verified hash chain for an entry that has not been "
        "written yet"
    )


def test_an_unwritten_entry_reports_unknown_not_false():
    """"Not yet written" is a third state, distinct from "chain broken".

    Returning False would render "⚠ Chain Broken" and cry tamper on every
    healthy scan, which is the opposite failure.
    """
    entry = _build()["audit_entry"]
    assert entry["chain_verified"] is None
    assert entry["chain_state"] == "pending"


def test_prev_hash_is_null_rather_than_an_empty_string():
    """An empty string renders as a truncated hash of nothing. Absent is absent."""
    assert _build()["audit_entry"]["prev_hash"] is None


def test_a_finalized_scan_reports_the_real_chain_link(monkeypatch, tmp_path):
    """Once the entry IS written, the response must carry the real hashes it
    was written with — not placeholders, and not a recomputation."""
    from backend.core import audit

    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit_log.jsonl"))

    import anyio

    written = anyio.run(
        lambda: audit.append_entry("scan-abc", "a" * 64, "pass")
    )
    body = _build(finalized_entry=written)
    entry = body["audit_entry"]
    assert entry["prev_hash"] == written["prev_hash"]
    assert entry["entry_hash"] == written["entry_hash"]
    assert entry["chain_verified"] is True
    assert entry["chain_state"] == "written"


# --------------------------------------------------------------------------- #
# 3. Token accounting
# --------------------------------------------------------------------------- #

def test_tokens_used_is_null_when_nothing_counted_them():
    """`0` is a measurement — "no tokens were consumed" — and it is false for a
    scan that just made three LLM calls. The metric card must read "n/a"."""
    assert _build()["tokens_used"] is None


def test_tokens_used_is_reported_when_the_pipeline_counted_them():
    pipeline = _pipeline()
    pipeline.scan.tokens_used = 1300
    pipeline.final_fix.tokens_used = 900
    body = _build(pipeline=pipeline)
    assert body["tokens_used"] == 2200


def test_partial_token_counts_still_sum_what_is_known():
    """One agent reporting and another not must not zero the total."""
    pipeline = _pipeline()
    pipeline.scan.tokens_used = 1300
    assert _build(pipeline=pipeline)["tokens_used"] == 1300


# --------------------------------------------------------------------------- #
# 4. CVSS
# --------------------------------------------------------------------------- #

def test_severity_bands_are_not_published_as_cvss_scores():
    """A severity word mapped through a lookup table is not a CVSS score.

    The page renders these with `.toFixed(1)` next to the label "CVSS", which
    claims a scored vector. The band is still useful — it just has to be
    labelled as the ordinal it is.
    """
    body = _build()
    assert "cvss_before" not in body, "severity band published under a CVSS label"
    assert "cvss_after" not in body


def test_the_severity_band_is_reported_as_an_ordinal():
    body = _build(pipeline=_pipeline(Severity.CRITICAL))
    assert body["severity_before"] == "critical"


def test_residual_severity_is_not_invented():
    """`cvss_after = 1.2` asserted a measured residual risk after the patch.

    Nothing measures the patched code — the Validator scores the *fix*, not a
    fresh severity. So there is no "after" value to report.
    """
    body = _build()
    assert body.get("severity_after") is None


def test_clean_code_reports_no_severity_band():
    body = _build(pipeline=_pipeline(clean=True))
    assert body["severity_before"] is None
    assert body["vulnerabilities"] == []


# --------------------------------------------------------------------------- #
# 5. Fields that were already honest must stay that way
# --------------------------------------------------------------------------- #

def test_the_real_measurements_are_still_passed_through():
    body = _build(latency_ms=987.66, cost=0.00042)
    assert body["latency_ms"] == 987.7
    assert body["cost_usd"] == 0.00042
    assert body["eval_score"] == 91
    assert body["trace_id"] == "0" * 32


def test_vulnerabilities_are_reshaped_not_invented():
    body = _build()
    assert len(body["vulnerabilities"]) == 1
    v = body["vulnerabilities"][0]
    assert v["cwe"] == "CWE-89"
    assert v["severity"] == "critical"
    assert v["line"] == 1


@pytest.fixture
def anyio_backend():
    return "asyncio"
