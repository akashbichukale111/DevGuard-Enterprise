"""
Tests for the bundle's `catalog` section — the negotiated DataHub tool surface.

The panel this feeds makes one specific claim: *the tool list is what the server
offered on the day, not a constant.* That claim is only worth showing if the
data behind it is trustworthy in three ways, and each of these tests pins one of
them, because each failure mode produces a screen that looks fine:

  * **Offered comes from the server, not from us.** If the builder ever fell
    back to a hard-coded list, the panel would render a plausible eight tools on
    a run that negotiated six — and the run where the Archivist correctly
    degraded would look identical to the run where it succeeded. That is the one
    thing this panel must never do.
  * **Used is counted, not assumed.** Call counts come from the handoff records.
    A tool that was offered and never called must read as unused rather than
    quietly disappearing, because the gap between available and needed is
    information about the agents.
  * **Absent is a state, not an empty list.** A pack whose Archivist never ran
    has no negotiated set at all. It must be distinguishable from a server that
    offered nothing, and it must be recorded in `missing` so the UI can explain
    the gap instead of rendering a blank panel.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.v2.replay import build_bundle

REPO_ROOT = Path(__file__).resolve().parents[1]
PACK_ROOT = REPO_ROOT / "evidence" / "proof-pack"

#: The run captured end to end against a live DataHub v1.7.0.
LIVE_RUN = "d6-live-v170"
#: The refusal: no Archivist, therefore no negotiated capability set.
NO_ARCHIVIST_RUN = "d5-refusal"


def _packs() -> list[Path]:
    return sorted(p for p in PACK_ROOT.iterdir()
                  if p.is_dir() and (p / "INDEX.json").is_file())


@pytest.fixture(scope="module")
def live() -> dict:
    return build_bundle(PACK_ROOT / LIVE_RUN, repo_root=REPO_ROOT)


def test_offered_tools_come_from_the_captured_handshake(live: dict) -> None:
    """`offered` must equal the server's own tools/list, read from the pack."""
    captured = json.loads(
        (PACK_ROOT / LIVE_RUN / "archivist" / "capabilities.json").read_text()
    )
    catalog = live["catalog"]
    assert catalog["offered"] == sorted(captured["tools"])
    assert catalog["offered_count"] == len(captured["tools"])
    # Reported verbatim. This deliberately does not match the MCP package
    # version on PyPI, and normalising it would hide a fact about the server.
    assert catalog["server"]["version"] == captured["serverInfo"]["version"]
    assert catalog["protocol_version"] == captured["protocolVersion"]


def test_used_counts_match_the_handoff_records(live: dict) -> None:
    """Call counts are derived from what the agents actually invoked."""
    expected: dict[str, int] = {}
    for node in live["rail"]:
        for record in node["records"]:
            for call in record["tool_calls"]:
                expected[call["tool"]] = expected.get(call["tool"], 0) + 1
    assert live["catalog"]["used"] == expected
    assert sum(live["catalog"]["used"].values()) > 0, (
        "a live run that called no tools would mean the rail lost its tool calls"
    )


def test_no_tool_was_called_that_the_server_never_offered(live: dict) -> None:
    """A call outside the negotiated set means the allowlist and the server disagree.

    The panel renders this case in red because it would be a contract
    violation. It must not actually happen.
    """
    catalog = live["catalog"]
    unexpected = set(catalog["used"]) - set(catalog["offered"])
    assert not unexpected, f"called but never offered: {sorted(unexpected)}"


def test_a_pack_without_a_handshake_says_so_rather_than_showing_nothing() -> None:
    bundle = build_bundle(PACK_ROOT / NO_ARCHIVIST_RUN, repo_root=REPO_ROOT)
    catalog = bundle["catalog"]
    assert catalog["capabilities_ref"] is None
    assert catalog["offered"] == []
    # `offered_count is None` is the distinction that matters: zero would claim
    # the server offered nothing, which is a different and false statement.
    assert catalog["offered_count"] is None
    assert "catalog" in bundle["missing"]


@pytest.mark.parametrize("pack", _packs(), ids=lambda p: p.name)
def test_the_capabilities_ref_resolves_in_every_pack(pack: Path) -> None:
    """The panel prints this ref; a ref outside the bundle is a dead pointer."""
    bundle = build_bundle(pack, repo_root=REPO_ROOT)
    ref = bundle["catalog"]["capabilities_ref"]
    if ref is not None:
        assert ref in bundle["raw"], f"{pack.name}: {ref} is not embedded"


@pytest.mark.parametrize("pack", _packs(), ids=lambda p: p.name)
def test_catalog_section_is_always_present_and_serialisable(pack: Path) -> None:
    """The UI reads these keys unconditionally, on every pack including old ones."""
    catalog = build_bundle(pack, repo_root=REPO_ROOT)["catalog"]
    for key in ("server", "protocol_version", "offered", "offered_count",
                "used", "capabilities_ref"):
        assert key in catalog, f"{pack.name} is missing catalog.{key}"
    assert json.loads(json.dumps(catalog)) == catalog
