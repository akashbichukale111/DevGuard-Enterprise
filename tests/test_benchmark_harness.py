"""
Benchmark harness tests.

`benchmark.py` is the only thing in the repo that could ever produce a
publishable accuracy figure, and the measured-numbers rule says every published number comes from
the machine. That makes the harness's own correctness load-bearing: a wrong
denominator or a silently-swallowed failure would publish a wrong number with
full authority.

THE DEFECT THESE TESTS EXIST FOR
--------------------------------
A snippet whose scan raised `AgentExecutionError` was recorded as
`found = set()` — identical to a scan where the Scanner correctly reported
nothing. Those are different facts: the second is a detection miss, the first is
an infrastructure failure. Collapsed together, a run during an API outage
published itself as poor recall, with nothing in the report to indicate the
metrics were computed over a degraded run.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core import benchmark
from backend.core.schemas import AgentExecutionError, ScanResult, Severity, Vulnerability


def _scan_with(cwe_ids, confidence: float = 0.9) -> ScanResult:
    return ScanResult(
        vulnerabilities=[
            Vulnerability(
                cwe_id=c,
                severity=Severity.HIGH,
                line_number=1,
                explanation="Detected by the scanner during this benchmark run.",
                confidence_score=confidence,
            )
            for c in cwe_ids
        ],
        model_used="llama-3.3-70b-versatile",
        retrieved_cwe_ids=list(cwe_ids),
    )


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Errored scans are distinguished from clean scans
# --------------------------------------------------------------------------- #

def test_a_perfect_run_reports_no_errors(monkeypatch):
    """Every snippet detected exactly its expected CWEs."""
    async def fake_scanner(code, k_context=4):
        item = next(i for i in benchmark.BENCHMARK_SET if i["code"] == code)
        return _scan_with(item["expected_cwes"])

    monkeypatch.setattr(benchmark, "run_scanner", fake_scanner)
    report = run(benchmark.run_benchmark(concurrency=2))

    assert report.errored_snippets == 0
    assert report.false_positives == 0
    assert report.false_negatives == 0
    assert report.recall == 1.0


def test_every_scan_failing_is_reported_as_errored_not_as_missed(monkeypatch):
    """The defect, directly.

    If every call fails, recall is 0.0 — but the report must say WHY, otherwise
    an outage is indistinguishable from a scanner that detects nothing.
    """
    async def always_fails(code, k_context=4):
        raise AgentExecutionError("scanner", "upstream 503")

    monkeypatch.setattr(benchmark, "run_scanner", always_fails)
    report = run(benchmark.run_benchmark(concurrency=2))

    assert report.errored_snippets == report.total_snippets, (
        "a run where every scan raised must report every snippet as errored"
    )
    assert report.recall == 0.0


def test_a_partial_outage_is_visible_in_the_report(monkeypatch):
    """Half failing must show up as errored_snippets, not just depressed recall."""
    seen: list[str] = []

    async def flaky(code, k_context=4):
        item = next(i for i in benchmark.BENCHMARK_SET if i["code"] == code)
        seen.append(item["id"])
        if len(seen) % 2 == 0:
            raise AgentExecutionError("scanner", "rate limited")
        return _scan_with(item["expected_cwes"])

    monkeypatch.setattr(benchmark, "run_scanner", flaky)
    report = run(benchmark.run_benchmark(concurrency=1))

    assert 0 < report.errored_snippets < report.total_snippets
    # And the per-snippet detail says which ones.
    errored = [s["id"] for s in report.per_snippet if s["errored"]]
    assert len(errored) == report.errored_snippets


def test_a_clean_negative_control_is_not_counted_as_errored(monkeypatch):
    """The distinction that matters: correctly finding nothing is not an error.

    The corpus includes `clean_parameterized` with expected_cwes=set(). A scan
    returning no findings for it is a SUCCESS, and must not inflate
    errored_snippets.
    """
    async def finds_nothing(code, k_context=4):
        return _scan_with([])

    monkeypatch.setattr(benchmark, "run_scanner", finds_nothing)
    report = run(benchmark.run_benchmark(concurrency=2))

    assert report.errored_snippets == 0, (
        "finding nothing is a detection outcome, not an infrastructure failure"
    )
    assert report.false_positives == 0


# --------------------------------------------------------------------------- #
# Metric arithmetic
# --------------------------------------------------------------------------- #

def test_false_positives_are_counted(monkeypatch):
    """Report a CWE nothing expected, on every snippet."""
    async def cries_wolf(code, k_context=4):
        return _scan_with(["CWE-1004"])  # in no snippet's expected set

    monkeypatch.setattr(benchmark, "run_scanner", cries_wolf)
    report = run(benchmark.run_benchmark(concurrency=2))

    assert report.false_positives == report.total_snippets
    assert report.true_positives == 0
    assert report.precision == 0.0
    assert report.false_positive_rate > 0.0


def test_the_negative_control_exists_in_the_corpus():
    """FP rate is meaningless without at least one snippet expecting nothing.

    benchmark.py's own comment says so; this asserts the corpus still honours it.
    """
    clean = [i for i in benchmark.BENCHMARK_SET if not i["expected_cwes"]]
    assert clean, "the benchmark corpus has lost its negative control"


def test_metrics_stay_within_their_declared_bounds(monkeypatch):
    """All four rates are Field(ge=0, le=1); construction must never violate it."""
    async def partial(code, k_context=4):
        item = next(i for i in benchmark.BENCHMARK_SET if i["code"] == code)
        expected = sorted(item["expected_cwes"])
        return _scan_with(expected[:1] + ["CWE-1004"])

    monkeypatch.setattr(benchmark, "run_scanner", partial)
    report = run(benchmark.run_benchmark(concurrency=2))

    for name in ("precision", "recall", "accuracy", "false_positive_rate"):
        value = getattr(report, name)
        assert 0.0 <= value <= 1.0, f"{name} out of bounds: {value}"


def test_per_snippet_detail_covers_the_whole_corpus(monkeypatch):
    async def fake_scanner(code, k_context=4):
        return _scan_with([])

    monkeypatch.setattr(benchmark, "run_scanner", fake_scanner)
    report = run(benchmark.run_benchmark(concurrency=3))

    assert len(report.per_snippet) == len(benchmark.BENCHMARK_SET)
    ids = {s["id"] for s in report.per_snippet}
    assert ids == {i["id"] for i in benchmark.BENCHMARK_SET}


# --------------------------------------------------------------------------- #
# The artifact — the only sanctioned route from a run to a published number
# --------------------------------------------------------------------------- #

def test_the_artifact_carries_every_metric_the_api_requires():
    """`_load_benchmark_artifact` drops a payload missing any required key, so a
    mismatch between these two shapes silently publishes nothing at all."""
    from backend.api.router import _load_benchmark_artifact  # noqa: F401

    report = run(_perfect_run())
    payload = benchmark.artifact_payload(report)
    for key in ("accuracy", "precision", "recall", "false_positive_rate", "sample_size"):
        assert payload.get(key) is not None, f"artifact is missing {key}"


def test_the_artifact_round_trips_through_the_api_reader(tmp_path, monkeypatch):
    """End-to-end on the publication path, with no live key: a real (mocked) run
    writes an artifact, and the API reader publishes exactly those numbers."""
    import json as _json

    from backend.api import router as api_router

    report = run(_perfect_run())
    artifact = tmp_path / "benchmark_report.json"
    artifact.write_text(_json.dumps(benchmark.artifact_payload(report)))
    monkeypatch.setattr(api_router, "BENCHMARK_ARTIFACT_PATH", str(artifact))

    published = api_router._load_benchmark_artifact()
    assert published is not None
    assert published["accuracy"] == report.accuracy
    assert published["recall"] == report.recall
    assert published["sample_size"] == report.total_snippets
    assert published["generated_at"] is not None


def test_the_artifact_records_how_degraded_the_run_was(monkeypatch):
    """A future reader must be able to tell whether these rates came from a
    clean run. Without errored_snippets in the artifact, an outage's numbers are
    indistinguishable from real accuracy once written to disk."""
    async def flaky(code, k_context=4):
        item = next(i for i in benchmark.BENCHMARK_SET if i["code"] == code)
        if item["id"] == "ssrf":
            raise AgentExecutionError("scanner", "rate limited")
        return _scan_with(item["expected_cwes"])

    monkeypatch.setattr(benchmark, "run_scanner", flaky)
    report = run(benchmark.run_benchmark(concurrency=1))
    assert benchmark.artifact_payload(report)["errored_snippets"] == report.errored_snippets == 1


def test_the_printed_report_surfaces_errored_scans(monkeypatch, capsys):
    """The CLI omitted errored_snippets entirely, so a run during an outage
    printed depressed recall with nothing on screen explaining why."""
    async def always_fails(code, k_context=4):
        raise AgentExecutionError("scanner", "upstream 503")

    monkeypatch.setattr(benchmark, "run_scanner", always_fails)
    report = run(benchmark.run_benchmark(concurrency=2))
    benchmark.print_report(report)

    out = capsys.readouterr().out
    assert "ERRORED" in out
    assert str(report.errored_snippets) in out


async def _perfect_run():
    """A full run where the scanner detects exactly the ground truth."""
    import backend.core.benchmark as b

    original = b.run_scanner

    async def fake_scanner(code, k_context=4):
        item = next(i for i in b.BENCHMARK_SET if i["code"] == code)
        return _scan_with(item["expected_cwes"])

    b.run_scanner = fake_scanner
    try:
        return await b.run_benchmark(concurrency=2)
    finally:
        b.run_scanner = original
