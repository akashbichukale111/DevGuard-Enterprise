"""
backend/core/cache.py
=====================
Redis caching for repeat scans.

SRE DESIGN NOTES — CACHE KEY STRATEGY
-------------------------------------
Cache key = sha256(normalized_code + language + severity_policy_version).

Rationale, in order of importance:
  1. CONTENT-ADDRESSED, not id-addressed. Two different users pasting the SAME
     vulnerable snippet should hit the same cache entry. The scan result is a
     pure function of the code + policy, so the key must be a function of the
     code + policy — never the scan_id (which is per-request and would give 0%
     hit rate).
  2. NORMALIZE before hashing. We strip trailing whitespace and normalize line
     endings so cosmetically-identical code doesn't cause avoidable misses. We
     do NOT do aggressive semantic normalization — that risks collapsing two
     snippets that a scanner would legitimately treat differently.
  3. POLICY VERSION in the key. If the scanning ruleset changes, every old cache
     entry must be invalidated automatically. Baking a policy version into the
     key means a policy bump is a clean, instantaneous cache bust — no manual
     FLUSHDB, no stale "clean" verdict served against new rules. This is the
     compliance-critical part: we must never serve a verdict computed under an
     outdated ruleset.

TTL = 24h. Long enough to absorb the "same PR scanned repeatedly in CI" pattern
that drives most repeat volume; short enough that a scanner model upgrade drains
naturally within a day even if someone forgets to bump the policy version.

FAIL-OPEN: if Redis is down, we log, count a miss, and run the pipeline. A cache
outage must degrade to "slower," never to "broken." Availability > hit rate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Optional

import redis.asyncio as redis

from backend.core import telemetry
from backend.core.schemas import PipelineResult, ScanRequest, ScanResult  # noqa: F401

logger = logging.getLogger("devguard.cache")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CACHE_TTL_S = int(os.getenv("SCAN_CACHE_TTL_S", str(24 * 3600)))
# Bump this whenever the scanning ruleset / prompt / model policy changes.
SCAN_POLICY_VERSION = os.getenv("SCAN_POLICY_VERSION", "v1")

_client: Optional[redis.Redis] = None


async def init_cache() -> None:
    """Create the Redis connection pool at startup."""
    global _client
    _client = redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    try:
        await _client.ping()
        logger.info("Redis cache connected: %s", REDIS_URL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis unavailable at startup (%s). Running cache-less.", exc)


async def close_cache() -> None:
    if _client is not None:
        await _client.aclose()


def _normalize(code: str) -> str:
    """Cosmetic normalization only — see design notes."""
    return "\n".join(line.rstrip() for line in code.replace("\r\n", "\n").split("\n")).strip()


def cache_key(request: ScanRequest) -> str:
    """Deterministic content-addressed key. Pure + unit-testable.

    NOTE ON THE `severity` COMPONENT: it is **always the empty string**.
    `ScanRequest.model_fields` is exactly `['code', 'language']`, so
    `getattr(request, "severity", "")` never finds anything — verified:
    `cache_key(ScanRequest(code=c)) == cache_key(ScanRequest(code=c,
    severity="critical"))`.

    That is correct, and it has to stay that way. Severity is a *finding*, not an
    input: nothing has scanned the code when the request arrives, so a key
    computed before the scan cannot include it. The component is kept only so the
    key shape stays stable for any entries already written. The module docstring
    above describes the key as `code + language + severity_policy_version`; the
    severity part of that has never contributed a byte.
    """
    payload = "|".join(
        [
            _normalize(request.code),
            getattr(request, "language", "") or "",
            getattr(request, "severity", "") or "",
            SCAN_POLICY_VERSION,
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"devguard:scan:{SCAN_POLICY_VERSION}:{digest}"


async def get_cached(request: ScanRequest) -> Optional[PipelineResult]:
    """
    Return a cached PipelineResult or None. Records hit/miss as BOTH a span
    attribute and a counter metric (Feature #9).

    THE TYPE HERE IS LOAD-BEARING, and it used to be wrong. `POST /scan` writes
    whatever `run_pipeline_resilient` returns — a **PipelineResult** — and this
    function deserialized with `ScanResult(**json.loads(raw))`. A PipelineResult
    dump has no top-level `model_used` (it lives at `scan.model_used`) and that
    field is required on ScanResult, so every single read raised
    ValidationError, was swallowed by the broad `except`, and was logged as
    "Corrupt cache entry ...; re-running."

    Two consequences, both silent:

      * **The cache never hit. Not once.** Feature #9 was 100% miss for its
        entire existence, and the log blamed data corruption rather than a
        writer/reader type mismatch — so it read as a Redis problem forever.

      * **A hit would have been a false negative.** The router hands this object
        straight to `_build_frontend_result`, which reads
        `getattr(pipeline, "scan", None)`. A ScanResult has no `.scan`, so that
        is None and the response reports ZERO vulnerabilities — a vulnerable file
        rendered clean, with a green UI. For a security scanner that is the worst
        failure direction available, and it was one successful deserialization
        away.

    Validation on read is what keeps a wrong-shaped entry a miss instead of an
    empty scan result, so the `except` below is a real safety net rather than a
    permanent state.
    """
    span = telemetry.trace.get_current_span()
    key = cache_key(request)
    span.set_attribute("cache.key", key)

    if _client is None:
        _record_miss(span, reason="cache_unavailable")
        return None

    try:
        raw = await _client.get(key)
    except Exception as exc:  # noqa: BLE001 — FAIL OPEN
        logger.warning("Redis GET failed (%s); treating as miss.", exc)
        _record_miss(span, reason="redis_error")
        return None

    if raw is None:
        _record_miss(span, reason="not_found")
        return None

    try:
        result = PipelineResult(**json.loads(raw))
    except Exception as exc:  # noqa: BLE001
        # Corrupt or wrong-shaped entry — treat as a miss and let the pipeline
        # overwrite it. Counted as a miss, NOT as a hit: an entry we could not
        # read is not a hit, and counting it as one made the hit-rate metric
        # report the exact opposite of the truth.
        logger.warning("Unreadable cache entry for %s (%s); re-running.", key, exc)
        _record_miss(span, reason="corrupt")
        return None

    _record_hit(span)
    return result


async def set_cached(request: ScanRequest, result: PipelineResult) -> None:
    """Persist a result. Best-effort; a write failure never fails the request.

    Writes the same type `get_cached` reads. The asymmetry between this function
    and its reader is what made the cache permanently non-functional.
    """
    if _client is None:
        return
    key = cache_key(request)
    try:
        # model_dump for pydantic v2; fall back to dict() for v1.
        try:
            body = result.model_dump(mode="json")
        except (AttributeError, TypeError):
            body = result.dict()
        await _client.set(key, json.dumps(body, default=str), ex=CACHE_TTL_S)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis SET failed for %s (%s); result not cached.", key, exc)


def _record_hit(span) -> None:
    span.set_attribute("cache.hit", True)
    if telemetry.CACHE_HIT_TOTAL is not None:
        telemetry.CACHE_HIT_TOTAL.add(1)


def _record_miss(span, reason: str) -> None:
    span.set_attribute("cache.hit", False)
    span.set_attribute("cache.miss_reason", reason)
    if telemetry.CACHE_MISS_TOTAL is not None:
        telemetry.CACHE_MISS_TOTAL.add(1, {"reason": reason})
