"""
backend/core/audit.py
=====================
Immutable, hash-chained audit trail for completed scans (Feature #8).

SRE / COMPLIANCE DESIGN NOTES
-----------------------------
In fintech, "what did the automated system decide, on what code, and when?" is a
regulator-facing question. The audit log is not a debugging convenience — it's a
tamper-evident record. Design choices:

  - HASH CHAIN: each entry stores prev_hash = sha256 of the *canonical
    serialization* of the previous entry. Any retroactive edit to entry N breaks
    the hash of N and every entry after it, so tampering is detectable by a single
    linear verification pass. This is the same primitive a blockchain uses,
    minus the distributed consensus we don't need for a single-writer log.

  - CANONICAL SERIALIZATION: we hash json.dumps(..., sort_keys=True,
    separators=(",",":")). Deterministic key order + no whitespace means the hash
    is reproducible on verification. A non-canonical hash is a hash you can't
    re-verify — worse than useless.

  - APPEND-ONLY JSONL: one JSON object per line. Append-only is the whole point;
    we never rewrite the file. JSONL is grep-able and stream-verifiable without
    loading the entire file into memory (matters once you're millions of scans in).

  - GENESIS: the first entry's prev_hash is a fixed sentinel ("0"*64) so the chain
    has a well-defined, verifiable root.

  - WRITE SERIALIZATION: an asyncio.Lock guards append so two concurrent scans
    can't interleave and compute prev_hash off a stale tail (which would fork the
    chain). Correctness of the chain depends on strictly serialized writes.

  - We store code_hash, NOT code. Same compliance reason as the tracing layer:
    the audit log must be safe to hand to an auditor without leaking source/secrets.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger("devguard.audit")

AUDIT_LOG_PATH = os.getenv("AUDIT_LOG_PATH", "data/audit_log.jsonl")
GENESIS_PREV_HASH = "0" * 64

_write_lock = asyncio.Lock()


def _canonical(entry: dict[str, Any]) -> str:
    """Deterministic serialization used for hashing (excludes the entry's own hash)."""
    return json.dumps(entry, sort_keys=True, separators=(",", ":"))


def _hash_entry(entry: dict[str, Any]) -> str:
    """Hash the canonical form of an entry's payload (without its 'entry_hash')."""
    payload = {k: v for k, v in entry.items() if k != "entry_hash"}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


# How much of the file's tail to read when looking for the last record. One
# entry is ~250 bytes, so 8 KiB comfortably contains the final line while
# staying a single small read.
_TAIL_CHUNK_BYTES = 8192


def _read_last_line(path: str) -> Optional[str]:
    """Return the last non-empty line, reading only the tail of the file.

    PERFORMANCE (this replaced a full-file iteration):
    The previous implementation looped over every line — its docstring said
    "Streaming, not full-load", which was true of memory but not of I/O. Since
    it runs once per append, the total cost of writing N entries was O(N^2).
    Measured on this machine, per-append latency by existing log size:

        0 entries    0.112 ms
        1 000        0.338 ms
        10 000       2.738 ms
        50 000      13.823 ms      (123x slower than empty)

    Seeking from the end makes it O(1) in the log size. It also matters because
    append_entry() holds a lock while doing this synchronously, so the cost was
    paid on the event loop and stalled unrelated requests.
    """
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        file_size = fh.tell()
        if file_size == 0:
            return None

        # Walk backwards in chunks until a newline separates a complete final
        # line, or we have read the whole (small) file.
        offset = 0
        buf = b""
        while True:
            offset = min(offset + _TAIL_CHUNK_BYTES, file_size)
            fh.seek(file_size - offset)
            buf = fh.read(offset)
            if b"\n" in buf.rstrip(b"\n") or offset >= file_size:
                break

    lines = [ln for ln in buf.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    return lines[-1] if lines else None


def _read_last_entry() -> Optional[dict[str, Any]]:
    """Read the tail of the JSONL log to get prev_hash. O(1) in the log size."""
    if not os.path.exists(AUDIT_LOG_PATH):
        return None
    last_line = _read_last_line(AUDIT_LOG_PATH)
    if last_line is None:
        return None
    try:
        return json.loads(last_line)
    except json.JSONDecodeError:
        logger.error("Audit log tail is corrupt — chain integrity at risk.")
        return None


async def append_entry(scan_id: str, code_hash: str, verdict: str) -> dict[str, Any]:
    """
    Append a completed-scan record to the chain. Returns the written entry.

    Fields: {scan_id, timestamp, code_hash, verdict, prev_hash, entry_hash}
    entry_hash is stored too so verification can confirm each link without
    recomputing under ambiguity about what was hashed.
    """
    os.makedirs(os.path.dirname(AUDIT_LOG_PATH) or ".", exist_ok=True)

    def _read_and_write() -> dict[str, Any]:
        """The blocking part: tail read + append. Runs on a worker thread.

        Kept as one function so the read and the write stay inside the same
        critical section — splitting them would let two writers interleave and
        fork the chain, which is exactly what _write_lock exists to prevent.
        """
        last = _read_last_entry()
        prev_hash = last["entry_hash"] if last else GENESIS_PREV_HASH

        entry = {
            "scan_id": scan_id,
            "timestamp": time.time(),
            "code_hash": code_hash,
            "verdict": verdict,
            "prev_hash": prev_hash,
        }
        entry["entry_hash"] = _hash_entry(entry)

        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        return entry

    async with _write_lock:  # serialize writers -> no chain forks
        # to_thread keeps the file I/O off the event loop. Previously this ran
        # synchronously inside the lock, so a large audit log stalled every
        # other in-flight request, not just audit writes.
        entry = await asyncio.to_thread(_read_and_write)

    logger.info("Audit entry appended: scan_id=%s verdict=%s", scan_id, verdict)
    return entry


def count_entries() -> int:
    """Number of entries, without parsing any JSON.

    The /audit-log endpoint reports a total alongside a page of results.
    Counting newlines over binary chunks is roughly two orders of magnitude
    cheaper than json.loads on every record just to call len() on the result.
    """
    if not os.path.exists(AUDIT_LOG_PATH):
        return 0
    count = 0
    with open(AUDIT_LOG_PATH, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            count += chunk.count(b"\n")
    return count


def read_tail(limit: int) -> list[dict[str, Any]]:
    """Return at most `limit` most-recent entries, oldest-first.

    PERFORMANCE: /audit-log?limit=N used to call read_all() and slice the last N
    off the result, so it json.loads()'d the ENTIRE log to return one page.
    Measured cost of returning 100 entries, by log size:

        100 entries      0.463 ms
        1 000            3.665 ms
        10 000          43.385 ms
        50 000         311.091 ms

    311 ms of synchronous work on the event loop, to produce 100 records. This
    reads backwards from the end instead, parsing only the page it returns.
    """
    if limit <= 0:
        return []
    if not os.path.exists(AUDIT_LOG_PATH):
        return []

    with open(AUDIT_LOG_PATH, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        file_size = fh.tell()
        if file_size == 0:
            return []

        # Grow the window from the end until it holds enough line breaks to
        # cover `limit` records, or until it covers the whole file.
        window = _TAIL_CHUNK_BYTES
        while True:
            window = min(window, file_size)
            fh.seek(file_size - window)
            buf = fh.read(window)
            # limit + 1 breaks guarantees `limit` COMPLETE lines, since the
            # first line in the window is probably truncated.
            if buf.count(b"\n") > limit or window >= file_size:
                break
            window *= 4

    lines = [ln for ln in buf.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    # Drop a leading partial line unless the window covered the whole file.
    if window < file_size and lines:
        lines = lines[1:]

    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            logger.error("Audit log contains an unparseable record; skipping it.")
    return out


def read_all() -> list[dict[str, Any]]:
    """Return ALL audit entries.

    Used by verify_chain(), which genuinely needs every record to walk the
    chain. Prefer read_tail() for anything paginated.
    """
    if not os.path.exists(AUDIT_LOG_PATH):
        return []
    entries = []
    with open(AUDIT_LOG_PATH, "r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                entries.append(json.loads(line))
    return entries


def verify_chain() -> dict[str, Any]:
    """
    Walk the chain and verify integrity (for /audit-log/verify).

    Returns a report: {valid, entries_checked, broken_at, reason}. We check both:
      (a) each entry's stored entry_hash matches a fresh recompute (no in-place edit)
      (b) each entry's prev_hash equals the previous entry's entry_hash (no reorder/insert/delete)
    """
    entries = read_all()
    expected_prev = GENESIS_PREV_HASH

    for idx, entry in enumerate(entries):
        # (a) recompute hash — detects field tampering
        recomputed = _hash_entry(entry)
        if recomputed != entry.get("entry_hash"):
            return {
                "valid": False,
                "entries_checked": idx + 1,
                "broken_at": idx,
                "scan_id": entry.get("scan_id"),
                "reason": "entry_hash mismatch (record was modified)",
            }
        # (b) link check — detects reordering/insertion/deletion
        if entry.get("prev_hash") != expected_prev:
            return {
                "valid": False,
                "entries_checked": idx + 1,
                "broken_at": idx,
                "scan_id": entry.get("scan_id"),
                "reason": "prev_hash mismatch (chain link broken)",
            }
        expected_prev = entry["entry_hash"]

    return {
        "valid": True,
        "entries_checked": len(entries),
        "broken_at": None,
        "reason": "chain intact",
    }
