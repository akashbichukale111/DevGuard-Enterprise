"""
Every path in the judging matrix must resolve, and every number in it must be
true.

A submission document that points at a file which has been renamed or deleted
is worse than one that omits the row: it invites a reviewer to click, find a
404, and start doubting the rows they cannot check. Same for the test counts —
a number that has drifted upward is harmless, but one that overstates is the
single fastest way to lose a reviewer's trust in the whole document.

This keeps the matrix honest automatically rather than by remembering.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MATRIX = REPO / "docs" / "JUDGING_MATRIX.md"


def _local_links() -> list[str]:
    """Markdown links in the matrix that point at repository paths."""
    text = MATRIX.read_text()
    links = re.findall(r"\]\(([^)]+)\)", text)
    return [
        link
        for link in links
        if not link.startswith(("http://", "https://", "mailto:", "#"))
    ]


def _collected_counts() -> dict[str, int]:
    """
    How many tests pytest actually collects, per file.

    Counting `def test_` in the source undercounts every parametrized test,
    which would make this guard fire on accurate numbers and stay silent on
    inflated ones — exactly backwards. Collection is the only source of truth,
    and a `--collect-only` sweep of the suite costs about two seconds.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    counts: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        m = re.match(r"^(tests/[\w/]+\.py): (\d+)$", line.strip())
        if m:
            counts[m.group(1)] = int(m.group(2))
    if not counts:
        pytest.skip(f"could not collect the suite: {proc.stdout[-500:]}")
    return counts


def test_the_matrix_exists():
    assert MATRIX.is_file()


@pytest.mark.parametrize("link", _local_links())
def test_every_referenced_path_resolves(link):
    """Paths are relative to docs/, since that is where the matrix lives."""
    target = (MATRIX.parent / link.split("#")[0]).resolve()
    assert target.exists(), f"judging matrix points at a missing path: {link}"


def test_quoted_per_file_test_counts_are_not_overstated():
    """
    The matrix cites per-file test counts. They may lag as the suite grows,
    but they must never claim more tests than exist.
    """
    counts = _collected_counts()
    text = MATRIX.read_text()
    cited = re.findall(r"\]\(\.\./(tests/[\w/]+\.py)\)[^|]*?\((\d+) tests\)", text)
    assert cited, "expected the matrix to cite per-file test counts"

    for path_str, claimed in cited:
        actual = counts.get(path_str)
        assert actual is not None, f"matrix cites {path_str}, which pytest does not collect"
        assert actual >= int(claimed), (
            f"{path_str}: matrix claims {claimed} tests, pytest collects {actual}"
        )


def test_the_quoted_suite_total_is_not_overstated():
    """The headline number is the one a reviewer is most likely to check."""
    counts = _collected_counts()
    total = sum(counts.values())
    claimed = re.search(r"([\d,]+) tests, no API key", MATRIX.read_text())
    assert claimed, "expected the matrix to quote a suite total"
    assert total >= int(claimed.group(1).replace(",", "")), (
        f"matrix claims {claimed.group(1)} tests, pytest collects {total}"
    )


#: Markdown that quotes the suite size as a current fact, as opposed to
#: generated reports. SECURITY.md, DISCLOSURE.md and DEMO.md were added after
#: all three were found sitting at 1041 against a suite of 1101; DEVPOST.md
#: after it was found at 1,039. Each drifted precisely because nothing checked
#: it. CHANGELOG.md is deliberately absent: its release sections record what was
#: true at each release and must be allowed to say 1041 forever. Same for the
#: numbers inside evidence/, which are capture records, not claims.
_CLAIM_DOCS = ["README.md", "ARCHITECTURE.md", "docs/JUDGING_MATRIX.md",
               "docs/ARCHITECTURE_REVIEW.md", "docs/REPRODUCIBILITY.md",
               "SECURITY.md", "DISCLOSURE.md", "DEMO.md",
               "docs/INSTALLATION.md", "docs/DEVPOST.md"]


def test_no_document_claims_more_tests_than_exist():
    """
    Test counts are quoted across the documents in _CLAIM_DOCS and every one of
    them drifts as the suite grows. Understating is harmless; overstating is a reviewer running
    `make test`, seeing a smaller number than the README promised, and
    reasonably concluding the rest of the document is padded too.
    """
    total = sum(_collected_counts().values())
    overstated = []
    for rel in _CLAIM_DOCS:
        doc = REPO / rel
        if not doc.is_file():
            continue
        text = doc.read_text()
        # Prose ("831 tests") and the shields.io badge ("tests-831%20passing"),
        # which is the most visible number in the repository and the easiest
        # one to forget.
        claims = (re.findall(r"([\d,]{3,}) tests\b", text)
                  + re.findall(r"badge/tests-([\d,]+)%20passing", text))
        for claimed in claims:
            if int(claimed.replace(",", "")) > total:
                overstated.append(f"{rel} claims {claimed} tests")
    assert not overstated, f"pytest collects {total}; " + "; ".join(overstated)


def test_the_honest_limits_section_is_present():
    """
    The matrix's value is that it states weaknesses. If that section is ever
    dropped, the document becomes marketing and this test should fail.
    """
    text = MATRIX.read_text()
    assert "No recorded run invoked a model" in text
    assert "negative result" in text.lower()
    assert "lexical-overlap-fallback[NOT-PROD]" in text
