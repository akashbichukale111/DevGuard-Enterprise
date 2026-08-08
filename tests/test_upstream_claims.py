"""
The upstream contribution package may not claim more than it has done.

`docs/upstream/` holds findings prepared for `datahub-project/datahub`. None
have been filed. That distinction is the entire integrity of the directory: a
prepared issue is honest work, and a prepared issue described as a contribution
is a fabricated one.

The failure this prevents is drift in the flattering direction — a checklist
quietly ticked, or the index saying "filed" while the document still says
"ready to file". Both are the kind of claim a judge or a maintainer can check
in ten seconds, and being caught costs far more than the claim was worth.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UPSTREAM = REPO / "docs" / "upstream"
INDEX = UPSTREAM / "README.md"


def _findings() -> list[Path]:
    """
    The finding write-ups themselves: top-level, index excluded.

    Deliberately not recursive. A finding is one document at the top of the
    directory, and the structural checks below assert the section headings a
    finding must carry — headings that supporting material (a patch, a written
    PR body) has no reason to have.
    """
    return sorted(p for p in UPSTREAM.glob("*.md") if p.name != "README.md")


def _every_document() -> list[Path]:
    """
    Every markdown document under `docs/upstream/`, at any depth.

    The honesty checks — not filed, no merged PR — must reach further than the
    findings. `01-patch/PULL_REQUEST.md` is a written-out pull request body, so
    it is the single document here most likely to drift into announcing that it
    was filed and merged, and a non-recursive glob was not looking at it.
    """
    return sorted(UPSTREAM.rglob("*.md"))


def test_the_directory_exists_with_findings():
    assert INDEX.is_file()
    assert _findings(), "an upstream directory with no findings is just a claim"


@pytest.mark.parametrize("doc", _findings(), ids=lambda p: p.name)
def test_every_finding_is_reproducible_from_its_own_text(doc):
    """
    An upstream maintainer's time is the scarce resource. An issue that cannot
    be reproduced from its own text consumes it without repaying it.
    """
    text = doc.read_text()
    assert "## Reproduction" in text or "## How this was found" in text
    assert "## Proposed" in text or "## Proposed fix" in text
    assert "## Suggested issue title" in text


@pytest.mark.parametrize("doc", _every_document(), ids=lambda p: p.name)
def test_no_finding_claims_to_have_been_filed(doc):
    """
    The load-bearing test. Until someone actually files these, the unticked
    boxes are the truth and must stay unticked.
    """
    text = doc.read_text()
    filed = re.search(r"^- \[[xX]\] Filed", text, re.M)
    assert not filed, f"{doc.name} claims to be filed; nothing here has been"

    searched = re.search(r"^- \[[xX]\] Searched existing issues", text, re.M)
    assert not searched, (
        f"{doc.name} claims a duplicate search that cannot be done from this "
        "environment"
    )


@pytest.mark.parametrize("doc", _findings(), ids=lambda p: p.name)
def test_every_finding_still_has_an_open_filing_checklist(doc):
    text = doc.read_text()
    assert re.search(r"^- \[ \] Filed", text, re.M), (
        f"{doc.name} dropped its filing checklist rather than leaving it open"
    )


def test_the_index_says_nothing_has_been_filed():
    text = INDEX.read_text()
    assert "has been filed" in text.lower() or "not been filed" in text.lower()
    assert "Nothing in this directory has been filed" in text


def test_the_index_links_every_finding():
    """A finding the index omits is one nobody will read."""
    linked = set(re.findall(r"\]\((\d+-[\w-]+\.md)\)", INDEX.read_text()))
    on_disk = {p.name for p in _findings()}
    assert on_disk <= linked, f"unlinked findings: {sorted(on_disk - linked)}"


@pytest.mark.parametrize("doc", _every_document(), ids=lambda p: p.name)
def test_no_document_claims_a_merged_pull_request(doc):
    """
    "Contributed to DataHub" is a claim with a public, checkable answer. It may
    not appear here until it is true.
    """
    text = doc.read_text().lower()
    for phrase in ("pr merged", "merged upstream", "was accepted",
                   "landed upstream", "contributed to datahub"):
        assert phrase not in text, f"{doc.name} claims {phrase!r}"


def test_the_rejected_candidates_are_recorded():
    """
    Saying which findings were considered and dropped, and why, is what makes
    the shortlist evidence of judgement rather than of effort.
    """
    text = INDEX.read_text()
    assert "rejected" in text.lower()
    assert "max_results" in text, "the get_lineage default was ours to fix, not DataHub's"
