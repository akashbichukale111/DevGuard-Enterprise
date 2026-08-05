"""
In-memory scan state must not grow without bound.

`router.py` keeps three process-local dicts, all keyed by `scan_id`:

    _scan_results      scan_id -> the full frontend-shaped result
    _pending_approvals scan_id -> {request, result, trace_id, created_at}
    _ws_pending_events scan_id -> buffered WebSocket events

THE DEFECT THESE TESTS EXIST FOR
--------------------------------
None of the three had an eviction path that always runs.

* `_scan_results` was **never** popped anywhere in the file — `grep -c
  '_scan_results.pop'` returned 0. Every scan added an entry and nothing removed
  it. Each entry holds `original_code` AND `fixed_code`, and `ScanRequest.code`
  is capped at 50,000 characters, so a single scan can retain ~100 KB
  indefinitely. A backend serving scans for a day leaks in proportion to traffic
  and eventually gets OOM-killed — with the audit log, the durable record, being
  the only thing that actually needed to persist.

* `_pending_approvals` is popped on `/approve` and `/reject`, but a gated scan
  that is never decided stays forever, holding the submitted source. Since
  critical/high severity is exactly what gets gated, the entries that linger are
  the ones holding the most sensitive code. The dict already stored a
  `created_at` timestamp that **nothing ever read** — an expiry was clearly
  intended and never built.

* `_ws_pending_events` is popped only when a WebSocket subscriber connects. A
  scan driven by `curl`, or a user who closes the tab before the socket opens,
  leaves its buffered events resident forever.

The fix is a bounded, TTL-based sweep rather than an unbounded dict. These tests
pin both halves: entries must survive long enough to be useful, and must not
survive forever.
"""

from __future__ import annotations

import time

import pytest

from backend.api import router as api_router


@pytest.fixture(autouse=True)
def clean_state():
    """Every test starts from empty process state and leaves it empty."""
    api_router._scan_results.clear()
    api_router._pending_approvals.clear()
    api_router._ws_pending_events.clear()
    yield
    api_router._scan_results.clear()
    api_router._pending_approvals.clear()
    api_router._ws_pending_events.clear()


def _store(scan_id: str, *, age_s: float = 0.0) -> None:
    """Put a result in the cache as if it had been stored `age_s` ago."""
    api_router._remember_scan_result(
        scan_id,
        {"original_code": "x" * 1000, "fixed_code": "y" * 1000, "status": "complete"},
        now=time.monotonic() - age_s,
    )


# --------------------------------------------------------------------------- #
# _scan_results — the dict with no eviction path at all
# --------------------------------------------------------------------------- #

def test_a_stored_result_is_retrievable():
    """The cache has to work before bounding it is interesting."""
    _store("scan-1")
    assert api_router._scan_results["scan-1"]["status"] == "complete"


def test_results_are_capped_by_count():
    """Past the cap, the oldest go. Unbounded growth is the defect."""
    cap = api_router.SCAN_RESULT_MAX_ENTRIES
    for i in range(cap + 25):
        _store(f"scan-{i}")

    assert len(api_router._scan_results) <= cap, (
        f"{len(api_router._scan_results)} results retained with a cap of {cap}"
    )
    # The newest survive; the very first are gone.
    assert f"scan-{cap + 24}" in api_router._scan_results
    assert "scan-0" not in api_router._scan_results


def test_eviction_is_oldest_first():
    for i in range(5):
        _store(f"scan-{i}")
    api_router._evict_scan_state(max_entries=3)

    remaining = set(api_router._scan_results)
    assert remaining == {"scan-2", "scan-3", "scan-4"}, remaining


def test_results_expire_by_age():
    """A quiet server must still shed old entries, not just a busy one.

    Count-based eviction alone leaves a low-traffic deployment holding submitted
    source code for as long as the process lives.
    """
    _store("ancient", age_s=api_router.SCAN_RESULT_TTL_SECONDS + 60)
    _store("recent", age_s=1.0)
    api_router._evict_scan_state()

    assert "ancient" not in api_router._scan_results
    assert "recent" in api_router._scan_results


def test_a_result_within_its_ttl_is_never_dropped():
    """The inverse property: eviction must not break the Result Dashboard.

    The frontend fetches `GET /scan/{id}` right after `POST /scan`, so an
    over-eager sweep turns every scan into a 404.
    """
    _store("fresh", age_s=api_router.SCAN_RESULT_TTL_SECONDS / 2)
    api_router._evict_scan_state()
    assert "fresh" in api_router._scan_results


def test_re_storing_the_same_scan_id_does_not_grow_the_cache():
    _store("same")
    _store("same")
    _store("same")
    assert len(api_router._scan_results) == 1


def test_updating_a_result_refreshes_nothing_it_should_not():
    """`/approve` rewrites the stored audit_entry in place; that must keep working
    after the retention layer was added."""
    _store("scan-x")
    api_router._scan_results["scan-x"]["status"] = "complete"
    api_router._scan_results["scan-x"]["audit_entry"] = {"chain_state": "written"}
    api_router._evict_scan_state()
    assert api_router._scan_results["scan-x"]["audit_entry"]["chain_state"] == "written"


# --------------------------------------------------------------------------- #
# _pending_approvals — created_at was written and never read
# --------------------------------------------------------------------------- #

def test_an_undecided_approval_expires():
    """A gated scan nobody decides must not hold submitted source forever.

    Critical/high severity is what gets gated, so these are the entries holding
    the most sensitive code.
    """
    api_router._pending_approvals["stale"] = {
        "request": None,
        "result": None,
        "trace_id": "t",
        "created_at": time.time() - api_router.PENDING_APPROVAL_TTL_SECONDS - 60,
    }
    api_router._evict_scan_state()
    assert "stale" not in api_router._pending_approvals


def test_a_recent_approval_is_not_expired():
    """Expiring a live approval gate would silently discard a human decision
    that is still in flight — worse than the leak."""
    api_router._pending_approvals["live"] = {
        "request": None,
        "result": None,
        "trace_id": "t",
        "created_at": time.time() - 5,
    }
    api_router._evict_scan_state()
    assert "live" in api_router._pending_approvals


def test_an_approval_without_created_at_is_not_dropped():
    """Fail safe on a malformed entry: never discard a pending human decision
    because a timestamp was missing."""
    api_router._pending_approvals["no-ts"] = {"request": None, "result": None}
    api_router._evict_scan_state()
    assert "no-ts" in api_router._pending_approvals


# --------------------------------------------------------------------------- #
# _ws_pending_events — only ever drained by a subscriber that may never arrive
# --------------------------------------------------------------------------- #

def test_buffered_events_for_a_scan_nobody_subscribed_to_expire():
    api_router._ws_pending_events["orphan"] = [{"type": "done"}]
    api_router._scan_results.clear()
    api_router._evict_scan_state(
        now=time.monotonic() + api_router.SCAN_RESULT_TTL_SECONDS + 60
    )
    assert "orphan" not in api_router._ws_pending_events


def test_buffered_events_for_a_live_scan_are_kept():
    """The buffer exists because POST /scan usually finishes before the frontend
    opens its socket. Dropping it early loses the 'done' event the UI waits on."""
    _store("live-scan")
    api_router._ws_pending_events["live-scan"] = [{"type": "done"}]
    api_router._evict_scan_state()
    assert "live-scan" in api_router._ws_pending_events


def test_the_event_buffer_for_one_scan_is_length_capped():
    """A pathological scan must not buffer without limit either."""
    for i in range(api_router.WS_BUFFER_MAX_EVENTS + 50):
        api_router._buffer_ws_event("noisy", {"seq": i})

    buffered = api_router._ws_pending_events["noisy"]
    assert len(buffered) <= api_router.WS_BUFFER_MAX_EVENTS
    # The most recent are what a late subscriber actually needs — in particular
    # the terminal "done" event, which is always last.
    assert buffered[-1]["seq"] == api_router.WS_BUFFER_MAX_EVENTS + 49


# --------------------------------------------------------------------------- #
# Retention is bounded in bytes as well as entries
# --------------------------------------------------------------------------- #

def test_the_cap_bounds_worst_case_retention():
    """Sanity-check the cap against the worst case it is meant to bound.

    `ScanRequest.code` allows 50,000 characters, and a result holds both the
    original and the patched copy, so one entry can approach 100 KB. This asserts
    the configured cap keeps the worst case to something a small container
    survives, rather than leaving it to grow with traffic.
    """
    from backend.core.schemas import ScanRequest

    max_code = ScanRequest.model_fields["code"].metadata
    limit = next(
        (getattr(m, "max_length", None) for m in max_code if hasattr(m, "max_length")),
        None,
    )
    assert limit == 50_000, "the code-length cap moved; re-derive the budget below"

    worst_case_bytes = api_router.SCAN_RESULT_MAX_ENTRIES * 2 * limit
    assert worst_case_bytes <= 256 * 1024 * 1024, (
        f"worst-case retention is {worst_case_bytes / 1024 / 1024:.0f} MiB — too "
        "much for a small container; lower SCAN_RESULT_MAX_ENTRIES"
    )
