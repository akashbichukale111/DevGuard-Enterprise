"""
The scan cache must be able to read back what it wrote.

`POST /scan` writes with `cache.set_cached(scan_req, result)` where `result` is
whatever `run_pipeline_resilient` returned — a **PipelineResult**. It reads with
`cache.get_cached(scan_req)`, which does:

    return ScanResult(**json.loads(raw))

THE DEFECTS THESE TESTS EXIST FOR
---------------------------------
1. **The cache could never hit.** A `PipelineResult` dump has no top-level
   `model_used` — that field lives at `scan.model_used` — and `model_used` is
   *required* on `ScanResult`. So every read raised `ValidationError`, was caught
   by the broad `except Exception`, and was logged as
   *"Corrupt cache entry ...; re-running."* Feature #9 was 100% miss, and the log
   line blamed data corruption rather than a type mismatch between the writer and
   the reader — so the failure looked like a Redis problem forever.

2. **If it ever HAD hit, the result would have been a false negative.** The
   router feeds the cached object straight into `_build_frontend_result`, which
   reads `getattr(pipeline, "scan", None)`. A `ScanResult` has no `.scan`, so
   that yields `None` and the response reports **zero vulnerabilities** — a
   vulnerable file rendered as clean, with a green UI. For a security scanner
   that is the worst possible failure direction, and it was one successful
   deserialization away.

The fix is symmetry: cache the same type the pipeline produces and the router
consumes, and validate on read so a genuinely corrupt entry is still a miss.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.core import cache
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


class FakeRedis:
    """Enough of redis.asyncio.Redis for the round trip, with no server."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def ping(self):
        return True


@pytest.fixture
def fake_redis(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(cache, "_client", client)
    return client


def _pipeline() -> PipelineResult:
    scan = ScanResult(
        vulnerabilities=[
            Vulnerability(
                cwe_id="CWE-89",
                severity=Severity.CRITICAL,
                line_number=2,
                explanation="Concatenated user input reaches a SQL execute call.",
                confidence_score=0.94,
            )
        ],
        model_used="llama-3.3-70b-versatile",
        retrieved_cwe_ids=["CWE-89"],
        tokens_used=1412,
    )
    return PipelineResult(
        original_code=VULN_CODE,
        scan=scan,
        final_fix=FixResult(
            patched_code="db.execute(q, (uid,))",
            diff_summary="Parameterised the query.",
            addressed_cwe_ids=["CWE-89"],
            model_used="llama-3.3-70b-versatile",
            tokens_used=880,
        ),
        final_validation=ValidationResult(
            eval_score=92,
            verdict=Verdict.PASS,
            reasoning="The patch parameterises the query and resolves the finding.",
            tokens_used=340,
        ),
        converged=True,
        routing_decisions={"scanner": "llama-3.3-70b-versatile"},
    )


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# The round trip
# --------------------------------------------------------------------------- #

def test_what_the_pipeline_writes_can_be_read_back(fake_redis):
    """The defect, directly: write a PipelineResult, read a PipelineResult.

    This is exactly the sequence POST /scan performs.
    """
    req = ScanRequest(code=VULN_CODE)
    original = _pipeline()

    run(cache.set_cached(req, original))
    got = run(cache.get_cached(req))

    assert got is not None, (
        "the cache could not read back what it just wrote — every scan is a miss "
        "and the log blames 'corrupt cache entry'"
    )
    assert isinstance(got, PipelineResult)


def test_a_cache_hit_preserves_the_vulnerabilities(fake_redis):
    """The security consequence, stated as a property.

    The router feeds a cache hit straight into `_build_frontend_result`, which
    reads `pipeline.scan`. If the cached object has no `.scan`, the response
    reports zero vulnerabilities — a vulnerable file rendered clean.
    """
    req = ScanRequest(code=VULN_CODE)
    run(cache.set_cached(req, _pipeline()))
    got = run(cache.get_cached(req))

    assert got.scan is not None
    assert len(got.scan.vulnerabilities) == 1
    v = got.scan.vulnerabilities[0]
    assert v.cwe_id == "CWE-89"
    assert v.severity is Severity.CRITICAL


def test_a_cache_hit_renders_the_same_response_as_a_fresh_scan(fake_redis):
    """End-to-end on the consequence: the response builder must not be able to
    tell a cache hit from a fresh result."""
    from backend.api import router as api_router

    req = ScanRequest(code=VULN_CODE)
    fresh = _pipeline()
    run(cache.set_cached(req, fresh))
    cached = run(cache.get_cached(req))

    a = api_router._build_frontend_result("s1", req, fresh, "a" * 32, 1.0, 0.0)
    b = api_router._build_frontend_result("s1", req, cached, "a" * 32, 1.0, 0.0)
    assert a["vulnerabilities"] == b["vulnerabilities"]
    assert a["severity_before"] == b["severity_before"] == "critical"
    assert a["eval_score"] == b["eval_score"] == 92
    assert a["tokens_used"] == b["tokens_used"] == 2632


def test_the_fix_and_validation_survive_the_round_trip(fake_redis):
    req = ScanRequest(code=VULN_CODE)
    run(cache.set_cached(req, _pipeline()))
    got = run(cache.get_cached(req))

    assert got.final_fix.patched_code == "db.execute(q, (uid,))"
    assert got.final_validation.eval_score == 92
    assert got.final_validation.verdict is Verdict.PASS
    assert got.converged is True
    assert got.routing_decisions == {"scanner": "llama-3.3-70b-versatile"}


def test_a_clean_scan_round_trips_too(fake_redis):
    """The short-circuit path stores final_validation=None; that must survive."""
    from backend.core import ai_agent  # noqa: F401  (documents where this comes from)

    clean = PipelineResult(
        original_code="print('ok')\n",
        scan=ScanResult(vulnerabilities=[], model_used="llama-3.3-70b-versatile"),
        final_fix=FixResult(
            patched_code="print('ok')\n",
            diff_summary="No vulnerabilities reported by the Scanner.",
            addressed_cwe_ids=[],
            model_used="none (no vulnerabilities reported)",
        ),
        final_validation=None,
        converged=True,
    )
    req = ScanRequest(code="print('ok')\n")
    run(cache.set_cached(req, clean))
    got = run(cache.get_cached(req))

    assert got.final_validation is None, (
        "a synthesised ValidationResult on read would invent an eval_score for a "
        "scan no Validator ever graded"
    )
    assert got.scan.vulnerabilities == []


# --------------------------------------------------------------------------- #
# A genuinely corrupt entry must still be a miss
# --------------------------------------------------------------------------- #

def test_unparseable_json_is_a_miss(fake_redis):
    req = ScanRequest(code=VULN_CODE)
    fake_redis.store[cache.cache_key(req)] = "{ this is not json"
    assert run(cache.get_cached(req)) is None


def test_json_that_is_not_a_pipeline_result_is_a_miss(fake_redis):
    """Validation on read is what keeps a wrong-shaped entry from becoming a
    silently-empty scan result. This is the exact shape the old writer produced
    for the old reader, so it doubles as a regression guard."""
    req = ScanRequest(code=VULN_CODE)
    fake_redis.store[cache.cache_key(req)] = json.dumps(
        {"vulnerabilities": [], "model_used": "llama-3.3-70b-versatile"}
    )
    assert run(cache.get_cached(req)) is None, (
        "a bare ScanResult payload was accepted; fed to the response builder it "
        "reports zero vulnerabilities for code that may be vulnerable"
    )


def test_an_unreadable_entry_counts_a_miss_and_not_a_hit(fake_redis, monkeypatch):
    """A second defect, found while fixing the first.

    `_record_hit(span)` fired BEFORE deserialization. Since deserialization
    always failed, every request incremented the hit counter AND the miss
    counter — so `devguard.cache.hit_total` reported a healthy hit rate for a
    cache that was returning None every time. The metric said the opposite of
    the truth, on the dashboard built to monitor it.
    """
    hits, misses = [], []

    class Counter:
        def __init__(self, sink):
            self.sink = sink

        def add(self, n, attrs=None):
            self.sink.append((n, attrs))

    monkeypatch.setattr(cache.telemetry, "CACHE_HIT_TOTAL", Counter(hits))
    monkeypatch.setattr(cache.telemetry, "CACHE_MISS_TOTAL", Counter(misses))

    req = ScanRequest(code=VULN_CODE)
    fake_redis.store[cache.cache_key(req)] = "{ not json"
    assert run(cache.get_cached(req)) is None

    assert hits == [], "an entry that could not be read was counted as a cache hit"
    assert len(misses) == 1
    assert misses[0][1] == {"reason": "corrupt"}


def test_a_real_hit_counts_a_hit_and_not_a_miss(fake_redis, monkeypatch):
    hits, misses = [], []

    class Counter:
        def __init__(self, sink):
            self.sink = sink

        def add(self, n, attrs=None):
            self.sink.append((n, attrs))

    monkeypatch.setattr(cache.telemetry, "CACHE_HIT_TOTAL", Counter(hits))
    monkeypatch.setattr(cache.telemetry, "CACHE_MISS_TOTAL", Counter(misses))

    req = ScanRequest(code=VULN_CODE)
    run(cache.set_cached(req, _pipeline()))
    assert run(cache.get_cached(req)) is not None

    assert len(hits) == 1
    assert misses == []


def test_a_missing_key_is_a_miss(fake_redis):
    assert run(cache.get_cached(ScanRequest(code="never scanned\n"))) is None


def test_no_client_is_a_miss_not_an_error(monkeypatch):
    """Fail-open: a cache outage degrades to slower, never to broken."""
    monkeypatch.setattr(cache, "_client", None)
    assert run(cache.get_cached(ScanRequest(code=VULN_CODE))) is None


def test_a_redis_error_on_read_is_a_miss_not_an_error(monkeypatch):
    class Exploding:
        async def get(self, key):
            raise ConnectionError("redis is down")

    monkeypatch.setattr(cache, "_client", Exploding())
    assert run(cache.get_cached(ScanRequest(code=VULN_CODE))) is None


def test_a_redis_error_on_write_never_propagates(monkeypatch):
    class Exploding:
        async def set(self, key, value, ex=None):
            raise ConnectionError("redis is down")

    monkeypatch.setattr(cache, "_client", Exploding())
    run(cache.set_cached(ScanRequest(code=VULN_CODE), _pipeline()))  # must not raise


# --------------------------------------------------------------------------- #
# Key semantics — the compliance-critical part of the design notes
# --------------------------------------------------------------------------- #

def test_a_policy_bump_busts_the_cache(fake_redis, monkeypatch):
    """cache.py: "we must never serve a verdict computed under an outdated
    ruleset." """
    req = ScanRequest(code=VULN_CODE)
    run(cache.set_cached(req, _pipeline()))
    assert run(cache.get_cached(req)) is not None

    monkeypatch.setattr(cache, "SCAN_POLICY_VERSION", "v2")
    assert run(cache.get_cached(req)) is None


def test_cosmetically_identical_code_shares_an_entry(fake_redis):
    a = ScanRequest(code="x = 1\ny = 2\n")
    b = ScanRequest(code="x = 1   \r\ny = 2\n\n")
    run(cache.set_cached(a, _pipeline()))
    assert run(cache.get_cached(b)) is not None


def test_different_code_does_not_share_an_entry(fake_redis):
    run(cache.set_cached(ScanRequest(code="x = 1\n"), _pipeline()))
    assert run(cache.get_cached(ScanRequest(code="x = 2\n"))) is None
