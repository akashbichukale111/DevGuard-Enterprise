"""
Tamper-evidence tests for the hash-chained audit trail.

The README calls this "hash-chained, tamper-evident." That is a security claim,
and until now nothing demonstrated that the chain actually *detects* tampering
— only that it reported "chain intact" on an untampered file, which any
function that returns True would also do.

These tests write a real chain to a temp file and then modify it the way an
attacker would: editing a record in place, reordering, deleting, and inserting.
Each must be caught, and caught with the right reason.
"""

from __future__ import annotations

import json

import pytest

from backend.core import audit


@pytest.fixture
def chain(tmp_path, monkeypatch):
    """A real 3-entry chain in an isolated file."""
    log = tmp_path / "audit_log.jsonl"
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(log))
    return log


async def _append(scan_id: str, code_hash: str, verdict: str):
    return await audit.append_entry(scan_id, code_hash, verdict)


@pytest.fixture
def populated(chain, anyio_backend):
    import anyio

    async def build():
        await _append("scan-1", "a" * 64, "pass")
        await _append("scan-2", "b" * 64, "fail")
        await _append("scan-3", "c" * 64, "pass")

    anyio.run(build)
    return chain


def _entries(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _rewrite(path, entries):
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #

def test_a_freshly_written_chain_verifies(populated):
    report = audit.verify_chain()
    assert report["valid"] is True
    assert report["entries_checked"] == 3
    assert report["broken_at"] is None
    assert report["reason"] == "chain intact"


def test_first_entry_links_to_genesis(populated):
    first = _entries(populated)[0]
    assert first["prev_hash"] == audit.GENESIS_PREV_HASH


def test_each_entry_links_to_its_predecessor(populated):
    entries = _entries(populated)
    for prev, cur in zip(entries, entries[1:]):
        assert cur["prev_hash"] == prev["entry_hash"]


def test_empty_chain_is_vacuously_valid(chain):
    report = audit.verify_chain()
    assert report["valid"] is True
    assert report["entries_checked"] == 0


# --------------------------------------------------------------------------- #
# Tamper detection — the actual security claim
# --------------------------------------------------------------------------- #

def test_editing_a_record_in_place_is_detected(populated):
    """The classic attack: flip a verdict and leave everything else alone."""
    entries = _entries(populated)
    entries[1]["verdict"] = "pass"  # was "fail"
    _rewrite(populated, entries)

    report = audit.verify_chain()
    assert report["valid"] is False
    assert report["broken_at"] == 1
    assert "entry_hash mismatch" in report["reason"]


def test_editing_the_code_hash_is_detected(populated):
    """Swapping which code a verdict refers to must not be silent."""
    entries = _entries(populated)
    entries[0]["code_hash"] = "f" * 64
    _rewrite(populated, entries)

    report = audit.verify_chain()
    assert report["valid"] is False
    assert report["broken_at"] == 0


def test_deleting_a_record_is_detected(populated):
    """Removing an inconvenient entry breaks the prev_hash link."""
    entries = _entries(populated)
    del entries[1]
    _rewrite(populated, entries)

    report = audit.verify_chain()
    assert report["valid"] is False
    assert "prev_hash mismatch" in report["reason"]


def test_reordering_records_is_detected(populated):
    entries = _entries(populated)
    entries[1], entries[2] = entries[2], entries[1]
    _rewrite(populated, entries)

    report = audit.verify_chain()
    assert report["valid"] is False
    assert "prev_hash mismatch" in report["reason"]


def test_appending_a_forged_record_is_detected(populated):
    """An attacker who does not know the chain state cannot append cleanly."""
    entries = _entries(populated)
    entries.append(
        {
            "scan_id": "forged",
            "timestamp": 1.0,
            "code_hash": "d" * 64,
            "verdict": "pass",
            "prev_hash": "0" * 64,      # wrong link
            "entry_hash": "e" * 64,     # wrong hash
        }
    )
    _rewrite(populated, entries)

    report = audit.verify_chain()
    assert report["valid"] is False
    assert report["broken_at"] == 3


def test_rehashing_a_tampered_record_still_breaks_the_link(populated):
    """A smarter attacker recomputes the edited record's own hash.

    That defeats check (a) but not check (b): the *next* record's prev_hash
    still points at the old value, so the chain is what provides the
    tamper-evidence, not the per-record hash alone. This is the test that
    justifies calling it a chain.
    """
    entries = _entries(populated)
    entries[0]["verdict"] = "fail"
    entries[0]["entry_hash"] = audit._hash_entry(entries[0])
    _rewrite(populated, entries)

    report = audit.verify_chain()
    assert report["valid"] is False
    # Entry 0 now self-verifies, so the break surfaces at entry 1's link.
    assert report["broken_at"] == 1
    assert "prev_hash mismatch" in report["reason"]


def test_report_names_the_scan_that_broke(populated):
    entries = _entries(populated)
    entries[2]["verdict"] = "fail"
    _rewrite(populated, entries)

    report = audit.verify_chain()
    assert report["valid"] is False
    assert report["scan_id"] == "scan-3"


# --------------------------------------------------------------------------- #
# Tail read — the O(1) replacement for a full-file scan
# --------------------------------------------------------------------------- #

def test_tail_read_on_a_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "nope.jsonl"))
    assert audit._read_last_entry() is None


def test_tail_read_on_an_empty_file(chain):
    chain.write_text("")
    assert audit._read_last_entry() is None


def test_tail_read_on_a_whitespace_only_file(chain):
    chain.write_text("\n\n   \n")
    assert audit._read_last_entry() is None


def test_tail_read_with_a_single_entry(populated, chain):
    entries = _entries(chain)
    last = audit._read_last_entry()
    assert last["entry_hash"] == entries[-1]["entry_hash"]


def test_tail_read_tolerates_trailing_newlines(populated, chain):
    expected = _entries(chain)[-1]["entry_hash"]
    chain.write_text(chain.read_text() + "\n\n")
    assert audit._read_last_entry()["entry_hash"] == expected


def test_tail_read_spans_multiple_chunks(chain, monkeypatch, anyio_backend):
    """The backward walk must keep growing its read until it finds a newline.

    Forced with a tiny chunk size so the loop iterates many times over a file
    it cannot cover in one read.
    """
    import anyio

    monkeypatch.setattr(audit, "_TAIL_CHUNK_BYTES", 16)

    async def build():
        for i in range(40):
            await _append(f"scan-{i}", "d" * 64, "pass")

    anyio.run(build)

    entries = _entries(chain)
    assert len(entries) == 40
    assert audit._read_last_entry()["entry_hash"] == entries[-1]["entry_hash"]
    # And the chain the tail read produced is internally consistent.
    assert audit.verify_chain()["valid"] is True


def test_tail_read_reports_a_corrupt_tail(populated, chain):
    chain.write_text(chain.read_text() + "{not json\n")
    assert audit._read_last_entry() is None


def test_appending_to_a_large_log_keeps_the_chain_intact(chain, anyio_backend):
    """Regression guard: the tail read must find the true last entry, not a
    stale one. A wrong prev_hash would fork the chain silently."""
    import anyio

    async def build():
        for i in range(300):
            await _append(f"scan-{i}", "e" * 64, "pass")

    anyio.run(build)

    report = audit.verify_chain()
    assert report["valid"] is True
    assert report["entries_checked"] == 300


# --------------------------------------------------------------------------- #
# read_tail / count_entries — the paginated read path
# --------------------------------------------------------------------------- #

def _seed(path, n):
    prev = audit.GENESIS_PREV_HASH
    with open(path, "w") as fh:
        for i in range(n):
            e = {
                "scan_id": f"s{i}",
                "timestamp": 1.0,
                "code_hash": "a" * 64,
                "verdict": "pass",
                "prev_hash": prev,
            }
            e["entry_hash"] = audit._hash_entry(e)
            fh.write(json.dumps(e) + "\n")
            prev = e["entry_hash"]


@pytest.mark.parametrize("limit", [1, 2, 5, 50, 99, 100])
def test_read_tail_matches_read_all_slice(chain, limit):
    """The invariant: read_tail(n) must equal read_all()[-n:], exactly.

    This is what makes it a safe substitution in the endpoint — same records,
    same order, without parsing the whole log.
    """
    _seed(chain, 300)
    assert audit.read_tail(limit) == audit.read_all()[-limit:]


def test_read_tail_returns_oldest_first(chain):
    _seed(chain, 50)
    ids = [e["scan_id"] for e in audit.read_tail(3)]
    assert ids == ["s47", "s48", "s49"]


def test_read_tail_limit_exceeding_the_log_returns_everything(chain):
    _seed(chain, 7)
    got = audit.read_tail(500)
    assert len(got) == 7
    assert got == audit.read_all()


def test_read_tail_with_a_limit_of_exactly_the_log_size(chain):
    """Boundary: the window must cover the first record too, not drop it as a
    partial leading line."""
    _seed(chain, 12)
    got = audit.read_tail(12)
    assert len(got) == 12
    assert got[0]["scan_id"] == "s0"


@pytest.mark.parametrize("limit", [0, -1, -100])
def test_read_tail_rejects_non_positive_limits(chain, limit):
    _seed(chain, 10)
    assert audit.read_tail(limit) == []


def test_read_tail_on_a_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "nope.jsonl"))
    assert audit.read_tail(10) == []


def test_read_tail_on_an_empty_file(chain):
    chain.write_text("")
    assert audit.read_tail(10) == []


def test_read_tail_grows_its_window_across_many_chunks(chain, monkeypatch):
    """With a tiny starting window the backward walk must keep expanding.

    This is the path that would silently return too few records if the growth
    loop were wrong.
    """
    monkeypatch.setattr(audit, "_TAIL_CHUNK_BYTES", 8)
    _seed(chain, 200)
    assert audit.read_tail(60) == audit.read_all()[-60:]


def test_read_tail_skips_an_unparseable_record_without_raising(chain):
    _seed(chain, 20)
    with open(chain, "a") as fh:
        fh.write("{ this is not json\n")
    got = audit.read_tail(5)
    # The corrupt record is dropped; the good ones still come back.
    assert all("scan_id" in e for e in got)
    assert len(got) == 4


def test_count_entries_matches_read_all_length(chain):
    for n in (0, 1, 13, 500):
        _seed(chain, n)
        assert audit.count_entries() == n == len(audit.read_all())


def test_count_entries_on_a_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "nope.jsonl"))
    assert audit.count_entries() == 0


@pytest.fixture
def anyio_backend():
    return "asyncio"
