"""
The blast radius must not stop at the first page.

`get_lineage` in mcp-server-datahub takes `max_results` (default **30**) and
`offset` (default 0) — see `src/mcp_server_datahub/tools/lineage.py`. Neither
was ever passed, so every trace in this project read only the first 30 impacted
entities and reported that as the whole blast radius.

An understated radius is worse than the inflated one this module already fixed
once. An inflated count looks wrong and gets investigated; a truncated one
looks clean and closes the incident. And the terminal `mlModel` is typically
the furthest node in the graph, so it is exactly what falls off the end of
page one — the trace would report "no ML model reached" for a model that is
very much reached, and the ML owner would never be told.
"""

from __future__ import annotations

import json

import pytest

from backend.v2.agents.pathfinder import (
    LINEAGE_PAGE_SIZE,
    MAX_LINEAGE_PAGES,
    Pathfinder,
    _paginate_lineage,
)
from backend.v2.evidence import EvidenceBuilder
from backend.v2.proofpack import ProofPack

ROOT = "urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.raw.users,PROD)"
MODEL = "urn:li:mlModel:(urn:li:dataPlatform:mlflow,churn_predictor,PROD)"


class _Result:
    def __init__(self, text, ok=True, error=None, tool="get_lineage", arguments=None):
        self.text, self.ok, self.error = text, ok, error
        self.tool, self.arguments, self.duration_ms = tool, arguments or {}, 0.0

    def record(self, raw_ref=None):
        from backend.v2.handoff import ToolCallRecord

        return ToolCallRecord(tool=self.tool, arguments=self.arguments, ok=self.ok,
                              duration_ms=self.duration_ms, raw_ref=raw_ref,
                              error=self.error)


def _page(urns, degree=2):
    return json.dumps({"downstreams": {"searchResults": [
        {"degree": degree, "entity": {"urn": u, "type": "DATASET"}} for u in urns
    ]}})


class _PagingClient:
    """Serves a fixed list of pages, recording the offsets it was asked for."""

    def __init__(self, pages, ok=True):
        self._pages = pages
        self._ok = ok
        self.offsets: list[int] = []
        self.calls = 0

    def call(self, agent, tool, args):
        self.calls += 1
        self.offsets.append(args.get("offset"))
        idx = (args.get("offset") or 0) // LINEAGE_PAGE_SIZE
        text = self._pages[idx] if idx < len(self._pages) else _page([])
        return _Result(text, ok=self._ok, tool=tool, arguments=args)


def _run(client, tmp_path):
    return _paginate_lineage(
        client, "pathfinder", {"urn": ROOT, "upstream": False, "max_hops": 5},
        ProofPack(tmp_path, "r"), "pathfinder/get_lineage-downstream.json", "note")


# --------------------------------------------------------------------------- #
# Paging
# --------------------------------------------------------------------------- #

def test_page_parameters_are_actually_sent(tmp_path):
    """The bug, directly: neither parameter was ever passed."""
    c = _PagingClient([_page([f"urn:li:dataset:d{i}" for i in range(3)])])
    _run(c, tmp_path)
    assert c.offsets == [0]


def test_a_short_first_page_stops_immediately(tmp_path):
    """One round trip when everything fits, as before this existed."""
    c = _PagingClient([_page(["urn:li:dataset:a"])])
    _, impacted, truncated, _ = _run(c, tmp_path)
    assert c.calls == 1
    assert len(impacted) == 1
    assert not truncated


def test_a_full_page_triggers_the_next_one(tmp_path):
    """A page that is exactly full is indistinguishable from more to come."""
    full = [f"urn:li:dataset:d{i}" for i in range(LINEAGE_PAGE_SIZE)]
    c = _PagingClient([_page(full), _page(["urn:li:dataset:tail"])])
    _, impacted, truncated, _ = _run(c, tmp_path)
    assert c.calls == 2
    assert c.offsets == [0, LINEAGE_PAGE_SIZE]
    assert len(impacted) == LINEAGE_PAGE_SIZE + 1
    assert not truncated


def test_the_model_on_a_later_page_is_still_found(tmp_path):
    """
    The failure that motivated this. The terminal mlModel is the furthest node,
    so it is exactly what page one omits — and the trace would have reported
    "no ML model reached" for a model that is reached.
    """
    full = [f"urn:li:dataset:d{i}" for i in range(LINEAGE_PAGE_SIZE)]
    c = _PagingClient([_page(full), _page([MODEL])])
    _, impacted, _, _ = _run(c, tmp_path)
    assert any(e.urn == MODEL for e in impacted)


# --------------------------------------------------------------------------- #
# Hostile graphs
# --------------------------------------------------------------------------- #

def test_a_cycle_does_not_double_count(tmp_path):
    """
    A cyclic lineage graph returns the same URN on more than one page. Keyed
    de-duplication makes that harmless instead of inflating the radius — which
    is the fabricated-metric failure this module already fixed once.
    """
    repeated = [f"urn:li:dataset:d{i}" for i in range(LINEAGE_PAGE_SIZE)]
    c = _PagingClient([_page(repeated), _page(repeated[:10])])
    _, impacted, _, _ = _run(c, tmp_path)
    assert len(impacted) == LINEAGE_PAGE_SIZE


def test_an_unbounded_graph_stops_at_the_ceiling_and_says_so(tmp_path):
    """
    A pathological or cyclic graph must not spin forever, and a partial radius
    must never present as complete.
    """
    full = _page([f"urn:li:dataset:d{i}" for i in range(LINEAGE_PAGE_SIZE)])
    c = _PagingClient([full] * (MAX_LINEAGE_PAGES + 5))
    _, _, truncated, _ = _run(c, tmp_path)
    assert c.calls == MAX_LINEAGE_PAGES
    assert truncated, "a capped read must report itself as truncated"


def test_a_failed_page_stops_the_walk(tmp_path):
    """Paging on past an error would bury the failure under empty pages."""
    c = _PagingClient([_page(["urn:li:dataset:a"])], ok=False)
    _, _, truncated, records = _run(c, tmp_path)
    assert c.calls == 1
    assert records[0].ok is False


def test_malformed_page_text_does_not_raise(tmp_path):
    c = _PagingClient(["not json"])
    _, impacted, _, _ = _run(c, tmp_path)
    assert impacted == []


# --------------------------------------------------------------------------- #
# Artifacts and replay compatibility
# --------------------------------------------------------------------------- #

def test_the_first_page_keeps_its_original_artifact_name(tmp_path):
    """
    Existing replay bundles and proof-pack readers resolve this exact path.
    Renaming it to `-page1` would break every recorded run.
    """
    c = _PagingClient([_page(["urn:li:dataset:a"])])
    _, _, _, records = _run(c, tmp_path)
    assert records[0].raw_ref.endswith("get_lineage-downstream.json")


def test_later_pages_get_suffixed_artifacts(tmp_path):
    full = [f"urn:li:dataset:d{i}" for i in range(LINEAGE_PAGE_SIZE)]
    c = _PagingClient([_page(full), _page(["urn:li:dataset:tail"])])
    _, _, _, records = _run(c, tmp_path)
    assert records[1].raw_ref.endswith("get_lineage-downstream-page2.json")


def test_every_page_is_recorded_as_its_own_tool_call(tmp_path):
    """Auditability: three round trips must appear as three records."""
    full = [f"urn:li:dataset:d{i}" for i in range(LINEAGE_PAGE_SIZE)]
    c = _PagingClient([_page(full), _page(full), _page(["urn:li:dataset:x"])])
    _, _, _, records = _run(c, tmp_path)
    assert len(records) == 3
    assert all(r.tool == "get_lineage" for r in records)


# --------------------------------------------------------------------------- #
# Through the agent
# --------------------------------------------------------------------------- #

def test_the_pathfinder_reports_truncation_in_its_evidence(tmp_path):
    """A partial radius must say so where a reviewer will see it."""
    full = _page([f"urn:li:dataset:d{i}" for i in range(LINEAGE_PAGE_SIZE)])
    c = _PagingClient([full] * (MAX_LINEAGE_PAGES + 2))
    pf = Pathfinder(c, EvidenceBuilder("INC-1"), ProofPack(tmp_path, "r"))
    _, evidence, _ = pf.run(ROOT)
    claims = " ".join(e.claim for e in evidence)
    assert "TRUNCATED" in claims


def test_an_untruncated_run_makes_no_truncation_claim(tmp_path):
    c = _PagingClient([_page(["urn:li:dataset:a"])])
    pf = Pathfinder(c, EvidenceBuilder("INC-1"), ProofPack(tmp_path, "r"))
    _, evidence, _ = pf.run(ROOT)
    assert "TRUNCATED" not in " ".join(e.claim for e in evidence)
