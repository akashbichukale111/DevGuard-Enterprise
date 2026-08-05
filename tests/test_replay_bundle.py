"""
Tests for zero-infrastructure replay's replay bundle — the thing the Command Center renders.

The bundle is the only input to the UI, so a defect here is invisible in code
review and highly visible to a judge. The four properties worth pinning are the
four that would each produce a *plausible* wrong screen:

  * **Every `raw_ref` resolves.** The evidence chips, the tool-call rows and the
    panel "raw" buttons are all dictionary lookups into `raw`. A second spelling
    of the same path renders a dead button rather than an error, and this test
    is what caught exactly that (panels were emitting `<run-id>/…` while the
    chain used `evidence/proof-pack/<run-id>/…`).
  * **Unknown stays unknown.** the metrics strip forbids placeholder numbers. Cost must be
    None on every captured run, because no model was ever invoked, and a `0.0`
    creeping in would read as "this was free".
  * **A refusal is not a success.** The refused run must carry no root cause and
    no time-to-root-cause, and must still name the missing evidence class.
  * **Derived state is justified.** A partial pack must not report a state its
    artifacts do not support.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.v2.replay import (
    AGENT_ORDER,
    INCIDENT_STATES,
    ReplayBuildError,
    ReplayBundleBuilder,
    build_bundle,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PACK_ROOT = REPO_ROOT / "evidence" / "proof-pack"

#: The full loop, twice-run, with write-back — the run the video uses.
FULL_RUN = "d6-loop-pass2"
#: The evidence rule's refusal, captured against a genuinely unreachable graph.
REFUSED_RUN = "d5-refusal"
#: Chain-only: no Diagnostician, no Surgeon, no Scribe.
PARTIAL_RUN = "d4-evidence-chain"


def _packs() -> list[Path]:
    return sorted(p for p in PACK_ROOT.iterdir()
                  if p.is_dir() and (p / "INDEX.json").is_file())


@pytest.fixture(scope="module")
def full() -> dict:
    return build_bundle(PACK_ROOT / FULL_RUN, repo_root=REPO_ROOT)


@pytest.fixture(scope="module")
def refused() -> dict:
    return build_bundle(PACK_ROOT / REFUSED_RUN, repo_root=REPO_ROOT)


# --------------------------------------------------------------------------
# Every ref the UI can click must resolve inside the bundle.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("pack", _packs(), ids=lambda p: p.name)
def test_every_raw_ref_resolves(pack: Path) -> None:
    """No dead 'open raw' button anywhere, in any pack.

    Zero-infrastructure means the bundle is the whole filesystem as far as the
    UI is concerned. A ref that is not a key in `raw` cannot be recovered at
    render time — there is nothing to fall back to.
    """
    bundle = build_bundle(pack, repo_root=REPO_ROOT)
    raw = set(bundle["raw"])

    for item in bundle["evidence"]:
        assert item["raw_ref"] in raw, f"{item['id']} points at a ref not in the bundle"

    for node in bundle["rail"]:
        for record in node["records"]:
            for call in record["tool_calls"]:
                if call["raw_ref"]:
                    assert call["raw_ref"] in raw, (
                        f"{node['agent']}'s {call['tool']} call points outside the bundle"
                    )

    panel_refs = [
        bundle["root_cause"]["raw_ref"],
        (bundle["blast_radius"] or {}).get("queries_raw_ref"),
        bundle["prior_knowledge"]["capabilities_ref"],
        (bundle["policy"] or {}).get("proposed_diff_ref"),
        (bundle["policy"] or {}).get("validation_ref"),
    ]
    for ref in filter(None, panel_refs):
        assert ref in raw, f"panel ref {ref} is not embedded in the bundle"


@pytest.mark.parametrize("pack", _packs(), ids=lambda p: p.name)
def test_embedded_payloads_have_content(pack: Path) -> None:
    """An embedded artifact carries bytes, or is explicitly flagged missing."""
    bundle = build_bundle(pack, repo_root=REPO_ROOT)
    for ref, entry in bundle["raw"].items():
        assert entry.get("missing") or isinstance(entry.get("content"), str), (
            f"{ref} is neither present nor flagged missing"
        )


# --------------------------------------------------------------------------
# The metrics strip — unknown values stay unknown.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("pack", _packs(), ids=lambda p: p.name)
def test_cost_is_never_a_placeholder(pack: Path) -> None:
    """Cost is None on every captured run, and the reason is recorded.

    No model was invoked anywhere in the recorded runs — every handoff records
    `model=null` and the Diagnostician reports REASONER_UNAVAILABLE. Rendering
    $0.00 would claim the loop was free rather than unmeasured.
    """
    bundle = build_bundle(pack, repo_root=REPO_ROOT)
    assert bundle["metrics"]["cost_usd"] is None
    assert "metrics.cost_usd" in bundle["missing"]


@pytest.mark.parametrize("pack", _packs(), ids=lambda p: p.name)
def test_every_none_metric_has_a_reason(pack: Path) -> None:
    """Anything the UI will render as N/A can say why it is N/A."""
    bundle = build_bundle(pack, repo_root=REPO_ROOT)
    for field in ("time_to_root_cause_ms", "total_loop_ms", "cost_usd"):
        if bundle["metrics"][field] is None:
            assert f"metrics.{field}" in bundle["missing"], (
                f"metrics.{field} is None with no recorded reason"
            )


def test_tokens_are_not_reported_as_measured(full: dict) -> None:
    """Zero tokens from a run with no model is not a measurement."""
    assert full["metrics"]["tokens"] is None
    assert full["metrics"]["models"] == []
    assert full["metrics"]["tokens_note"]


# --------------------------------------------------------------------------
# The evidence rule / the root-cause panel — a refusal must read as a refusal.
# --------------------------------------------------------------------------

def test_refusal_names_the_missing_class(refused: dict) -> None:
    assert refused["chain"]["is_sufficient"] is False
    assert refused["root_cause"]["refused"] is True
    assert refused["root_cause"]["missing_evidence_classes"] == ["DATAHUB_GRAPH"]
    assert refused["root_cause"]["datahub_error"], "the captured cause must survive into the UI"


def test_refusal_has_no_time_to_root_cause(refused: dict) -> None:
    """A run that reached no root cause reports no time to reach one.

    The elapsed span exists and is shown as total loop duration; presenting it
    as time-to-root-cause would put a success metric directly above a
    full-width INSUFFICIENT EVIDENCE panel.
    """
    assert refused["metrics"]["time_to_root_cause_ms"] is None
    assert refused["metrics"]["total_loop_ms"] is not None
    assert "refused" in refused["missing"]["metrics.time_to_root_cause_ms"]


def test_refusal_is_distinct_from_blocked(full: dict, refused: dict) -> None:
    """BLOCKED is a missing dependency; INSUFFICIENT_EVIDENCE is a verdict.

    The dry-run Diagnostician stops with BLOCKED and its own rationale insists this
    is "NOT a refusal — the question was never asked". Collapsing the two would
    claim a governance decision the run never made.
    """
    diagnostician = next(n for n in full["rail"] if n["agent"] == "diagnostician")
    assert diagnostician["state"] == "blocked"
    assert full["root_cause"]["refused"] is False

    refused_node = next(n for n in refused["rail"] if n["agent"] == "diagnostician")
    assert refused_node["state"] == "refused"


# --------------------------------------------------------------------------
# The Command Center / the Command Center — derived state must be justified by artifacts.
# --------------------------------------------------------------------------

def test_full_run_reaches_knowledge_written(full: dict) -> None:
    assert full["incident"]["state"] == "KNOWLEDGE_WRITTEN"
    assert full["incident"]["states_reached"] == list(INCIDENT_STATES)


def test_partial_run_does_not_claim_states_it_never_reached() -> None:
    bundle = build_bundle(PACK_ROOT / PARTIAL_RUN, repo_root=REPO_ROOT)
    reached = bundle["incident"]["states_reached"]
    assert "DETECTED" in reached
    for state in ("REMEDIATING", "VERIFIED", "KNOWLEDGE_WRITTEN"):
        assert state not in reached, f"{PARTIAL_RUN} has no artifact justifying {state}"
    assert bundle["write_back"] is None
    assert "write_back" in bundle["missing"]


def test_dry_run_is_banner_flagged_and_not_knowledge_written() -> None:
    """A dry run captured payloads and wrote nothing. Both must be visible.

    Four of the five artifacts are `skipped_dry_run`. The fifth — the ownership
    signal — is `already_present`, because the Scribe *read* the owner, found it
    already set and had nothing to write. That is a genuine no-op rather than a
    suppressed write, so the pack records it differently and the panel shows it
    differently. Asserting all five were skipped would be asserting a mistake.
    """
    bundle = build_bundle(PACK_ROOT / "d6-dry-run", repo_root=REPO_ROOT)
    assert bundle["banners"]["dry_run"] is True
    assert bundle["incident"]["state"] != "KNOWLEDGE_WRITTEN"
    assert bundle["write_back"]["incident_resolved"] is False

    outcomes = {a["number"]: a["outcome"] for a in bundle["write_back"]["artifacts"]}
    assert all(outcomes[n] == "skipped_dry_run" for n in (1, 2, 3, 4))
    assert outcomes[5] == "already_present"


def test_replay_banner_is_always_on() -> None:
    """The Command Center: a recorded run must never be mistakable for a live one."""
    for pack in _packs():
        bundle = build_bundle(pack, repo_root=REPO_ROOT)
        assert bundle["banners"]["replay"] is True
        assert "NOT LIVE" in bundle["banners"]["replay_text"]


# --------------------------------------------------------------------------
# The Command Center — the rail is the pipeline, not the record list.
# --------------------------------------------------------------------------

def test_rail_has_one_node_per_agent_in_section_6_order(full: dict) -> None:
    assert [n["agent"] for n in full["rail"]] == list(AGENT_ORDER)


def test_agent_that_ran_without_a_handoff_is_not_reported_idle(full: dict) -> None:
    """The Sentinel writes patch-scan.json but the Surgeon owns the edge.

    Showing it idle would claim a security control did not run when it did; the
    fix must not go the other way either, so its duration stays None rather
    than borrowing a neighbour's.
    """
    sentinel = next(n for n in full["rail"] if n["agent"] == "sentinel")
    assert sentinel["state"] == "ran_no_record"
    assert sentinel["duration_ms"] is None
    assert sentinel["tokens"] is None


# --------------------------------------------------------------------------
# The blast-radius panel / the prior-knowledge banner — panels must agree with the agents that produced them.
# --------------------------------------------------------------------------

def test_blast_radius_matches_the_pathfinder_rationale(full: dict) -> None:
    """The panel's counts are the Pathfinder's counts, not a second derivation.

    The rationale string is the agent's own summary of the trace; if the panel
    disagreed with it, one of the two would be wrong on screen at the same time.
    """
    radius = full["blast_radius"]
    pathfinder = next(n for n in full["rail"] if n["agent"] == "pathfinder")
    rationale = pathfinder["records"][0]["rationale"]

    assert f"{len(radius['dataset_level'])} impacted entities" in rationale
    assert f"{len(radius['column_level'])} carrying column" in rationale
    assert radius["terminal_ml_models"], "the trace terminates at an ML model"
    assert radius["terminal_ml_models"][0]["urn"] in rationale


def test_prior_knowledge_agrees_with_the_archivist(full: dict) -> None:
    """The prior-knowledge banner banner must not contradict the Archivist's own verdict.

    This is the regression that motivated the test: the document parser looked
    for a flat `documents` list, DataHub returns `searchResults[].entity`, and
    the banner read NO PRIOR VERIFIED INCIDENTS on a run whose Archivist had
    just retrieved four runbooks.
    """
    pk = full["prior_knowledge"]
    assert pk["state"] == "PRIOR"
    assert pk["document_count"] == 4
    assert "4 document(s) retrieved" in pk["rationale"]
    assert all(d["urn"].startswith("urn:li:document:") for d in pk["documents"])


def test_prior_time_to_root_cause_is_absent_with_a_reason(full: dict) -> None:
    """The runbook template records no such figure, so the banner shows N/A."""
    assert full["prior_knowledge"]["time_to_root_cause_s"] is None
    assert "prior_knowledge.time_to_root_cause_s" in full["missing"]


# --------------------------------------------------------------------------
# The Command Center — the write-back panel is the money shot; it must be exact.
# --------------------------------------------------------------------------

def test_write_back_reports_five_artifacts_and_the_guard(full: dict) -> None:
    wb = full["write_back"]
    assert wb["performed"] is True
    assert len(wb["artifacts"]) == 5
    assert {a["number"] for a in wb["artifacts"]} == {1, 2, 3, 4, 5}
    assert all(a["outcome"] in ("written", "already_present") for a in wb["artifacts"])
    assert "until recovery is verified" in wb["guard"]


def test_created_urns_are_surfaced_for_linking(full: dict) -> None:
    """Artifacts that create an entity expose the created URN, not just the target."""
    wb = full["write_back"]
    runbook = next(a for a in wb["artifacts"] if a["number"] == 2)
    assert runbook["created_urn"] and runbook["created_urn"].startswith("urn:li:document:")


# --------------------------------------------------------------------------
# Links, and the errors.
# --------------------------------------------------------------------------

def test_no_links_without_a_base_url(full: dict) -> None:
    """A link to a host that is not running is worse than a plain URN."""
    assert full["incident"]["datahub_link"] is None
    assert all(e["datahub_link"] is None for e in full["evidence"])


def test_links_are_built_when_a_base_url_is_supplied() -> None:
    bundle = build_bundle(PACK_ROOT / FULL_RUN, repo_root=REPO_ROOT,
                          datahub_base_url="http://localhost:9002/")
    link = bundle["incident"]["datahub_link"]
    assert link and link.startswith("http://localhost:9002/dataset/urn:li:dataset:")


def test_a_directory_that_is_not_a_pack_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(ReplayBuildError, match="INDEX.json is missing"):
        build_bundle(tmp_path / "empty", repo_root=tmp_path)


def test_malformed_json_is_reported_not_swallowed(tmp_path: Path) -> None:
    pack = tmp_path / "broken"
    pack.mkdir()
    (pack / "INDEX.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ReplayBuildError, match="not valid JSON"):
        ReplayBundleBuilder(pack, repo_root=tmp_path).build()


def test_bundle_is_json_serialisable(full: dict) -> None:
    """The bundle is written to disk and fetched by a browser; it must round-trip."""
    assert json.loads(json.dumps(full))["run_id"] == FULL_RUN
