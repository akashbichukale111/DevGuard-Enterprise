"""
Every link resolves, and every committed image is actually shown somewhere.

A judge's first interaction with this repository is a markdown file full of
cross-references. A 404 in that file costs more than the paragraph it sits in:
it is the reader's first evidence about whether the rest of the document was
checked or merely written. `test_judging_matrix.py` already holds the *claims*
in the docs to the repository; this holds the *navigation*.

Both checks below were written against a tree that passes them, and both caught
something real on the way in — a 724 KB Command Center screenshot that had been
committed and then referenced by nothing.

The orphan check is deliberately not a lint about file hygiene. An image that no
document displays is either a missing section or dead weight, and on a
submission branch the first is much more likely and much more expensive.
"""

from __future__ import annotations

import re
import subprocess
import urllib.parse
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: `![alt](target)` and `[text](target)`, ignoring an optional "title" suffix.
LINK = re.compile(r'!?\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)')

#: Directories whose images are evidence captures displayed by the docs.
IMAGE_DIRS = ("docs/screenshots", "evidence/d10/screenshots")


def _tracked(*patterns: str) -> list[str]:
    """
    Tracked paths matching the globs.

    `-z` rather than line-splitting: without it git quotes any path holding a
    non-ASCII byte and hands back `"...\\342\\200\\224..."`, which is not a path
    that opens. This repository has evidence files with em-dashes in their names.
    """
    out = subprocess.run(["git", "ls-files", "-z", *patterns],
                         cwd=REPO, capture_output=True, text=True).stdout
    return [path for path in out.split("\0") if path]


def _heading_slugs(text: str) -> set[str]:
    """
    GitHub's anchor derivation, closely enough to catch a real typo.

    Strips inline code and emphasis, unwraps links to their text, drops
    everything that is not a word character, space or hyphen, then lowercases
    and hyphenates. Explicit `<a id=...>` anchors count too.
    """
    slugs = set()
    for line in text.splitlines():
        heading = re.match(r"^#{1,6}\s+(.*?)\s*$", line)
        if not heading:
            continue
        title = re.sub(r"[`*_]", "", heading.group(1))
        title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title)
        slugs.add(re.sub(r"[^\w\s-]", "", title).strip().lower().replace(" ", "-"))
    slugs.update(m.group(1) for m in re.finditer(r'<a\s+(?:id|name)="([^"]+)"', text))
    return slugs


@pytest.fixture(scope="module")
def markdown_files() -> list[str]:
    files = _tracked("*.md")
    assert files, "no tracked markdown found — is this running outside the repo?"
    return files


def test_every_relative_link_points_at_something_that_exists(markdown_files):
    """A broken relative link is a 404 on the page judges start from."""
    broken = []
    for rel in markdown_files:
        doc = REPO / rel
        for match in LINK.finditer(doc.read_text(encoding="utf-8", errors="replace")):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:", "tel:", "#")):
                continue
            path = urllib.parse.unquote(target.partition("#")[0])
            if path and not (doc.parent / path).resolve().exists():
                broken.append(f"{rel} -> {target}")
    assert not broken, "broken relative links:\n  " + "\n  ".join(broken)


def test_every_in_page_anchor_resolves(markdown_files):
    """
    The README navigates by anchor. A heading rename silently breaks the table
    of contents that points at it, and nothing else in the suite would notice.
    """
    slugs = {rel: _heading_slugs((REPO / rel).read_text(encoding="utf-8",
                                                        errors="replace"))
             for rel in markdown_files}
    broken = []
    for rel in markdown_files:
        doc = REPO / rel
        for match in LINK.finditer(doc.read_text(encoding="utf-8", errors="replace")):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:", "tel:")):
                continue
            path, _, fragment = target.partition("#")
            if not fragment:
                continue
            if not path:
                where = rel
            else:
                dest = (doc.parent / urllib.parse.unquote(path)).resolve()
                if dest.suffix != ".md" or not dest.exists():
                    continue
                try:
                    where = str(dest.relative_to(REPO))
                except ValueError:
                    continue
            if where in slugs and fragment.lower() not in slugs[where]:
                broken.append(f"{rel} -> {target}")
    assert not broken, "links to missing anchors:\n  " + "\n  ".join(broken)


def test_every_committed_screenshot_is_displayed_somewhere():
    """
    An evidence capture nothing references is either a section someone forgot to
    write or weight the clone carries for nothing. Both are worth failing on.

    MANIFEST.json is excluded as a referencing document on purpose: it lists every
    PNG in the directory by construction, so counting it would make this test
    incapable of failing.
    """
    images = _tracked(*[f"{d}/**/*.png" for d in IMAGE_DIRS])
    assert images, "no screenshots tracked — the globs are wrong"

    referencing = _tracked("*.md", "*.tsx", "*.ts", "*.json")
    haystack = []
    for rel in referencing:
        if Path(rel).name == "MANIFEST.json":
            continue
        haystack.append((REPO / rel).read_text(encoding="utf-8", errors="replace"))
    blob = "\n".join(haystack)

    orphans = [img for img in images if Path(img).name not in blob]
    assert not orphans, (
        "committed but displayed nowhere:\n  " + "\n  ".join(orphans))


def test_the_documents_judges_are_pointed_at_all_exist():
    """
    These are named in the README's navigation and in the submission write-up.
    If one is renamed, this says so before a judge finds it.
    """
    required = [
        "README.md", "ARCHITECTURE.md", "DEPLOYMENT.md", "SECURITY.md",
        "CHANGELOG.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "DISCLOSURE.md",
        "LICENSE",
        "docs/API.md", "docs/ARCHITECTURE_REVIEW.md", "docs/DEVPOST.md",
        "docs/INSTALLATION.md", "docs/JUDGE_WALKTHROUGH.md",
        "docs/JUDGING_MATRIX.md", "docs/LLM_EGRESS_BLOCKED.md",
        "docs/MCP_DECISION.md", "docs/REPRODUCIBILITY.md",
    ]
    missing = [rel for rel in required if not (REPO / rel).is_file()]
    assert not missing, f"documents referenced by the submission are missing: {missing}"


def test_mermaid_blocks_are_closed_and_declare_a_diagram_type():
    """
    An unclosed or untyped ```mermaid fence renders on GitHub as a grey error
    box, which looks worse than having no diagram at all.

    This is a structural check, not a parse: it catches the fence-level mistakes
    an editor makes. Whether each diagram is valid mermaid is answered by
    rendering it, not by a regex pretending to be a parser.
    """
    known = ("graph", "flowchart", "sequenceDiagram", "classDiagram",
             "stateDiagram", "stateDiagram-v2", "erDiagram", "journey",
             "gantt", "pie", "gitGraph", "mindmap", "timeline", "quadrantChart",
             "C4Context", "block-beta", "sankey-beta", "xychart-beta")
    problems = []
    for rel in _tracked("*.md"):
        text = (REPO / rel).read_text(encoding="utf-8", errors="replace")
        opens = len(re.findall(r"^```mermaid\s*$", text, re.M))
        blocks = re.findall(r"^```mermaid\s*\n(.*?)^```\s*$", text, re.M | re.S)
        if opens != len(blocks):
            problems.append(f"{rel}: {opens} mermaid fences opened, "
                            f"{len(blocks)} closed")
            continue
        for i, body in enumerate(blocks, 1):
            first = next((ln.strip() for ln in body.splitlines()
                          if ln.strip() and not ln.strip().startswith("%%")), "")
            if not first.startswith(known):
                problems.append(f"{rel} block #{i}: no diagram type "
                                f"(starts {first[:40]!r})")
    assert not problems, "mermaid blocks:\n  " + "\n  ".join(problems)
