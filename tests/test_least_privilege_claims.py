"""
The security documentation may not claim more than the evidence proves.

This exists because it had. SECURITY.md and the README both said seven
privileges were "never granted, and each is verified as a live DENY". The
committed artifact contains five DENY checks, of which four probe a privilege
and one probes the URN scope. Three privileges — glossary terms, domains and
entity status — were asserted rather than tested.

An overclaim in a security document is worse than the same overclaim anywhere
else: it is the section a reviewer is most likely to check and least likely to
forgive. Prose drifts from artifacts silently, so this makes the drift fail the
build instead.

The rule enforced here is deliberately one-directional. Documentation may
under-claim freely — describing a probe as unproven when it has since passed
costs nothing. It may never over-claim.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SUMMARY = REPO / "evidence" / "proof-pack" / "security" / "least-privilege" / "summary.json"
VERIFIER = REPO / "scripts" / "verify_least_privilege.py"
SECURITY = REPO / "SECURITY.md"
README = REPO / "README.md"

#: Privileges the documentation is allowed to describe as proven, derived from
#: the artifact rather than restated here — a hard-coded list would be one more
#: thing to drift.
def _artifact() -> dict:
    return json.loads(SUMMARY.read_text())


def _proven_privileges() -> set[str]:
    """Privilege names appearing in a DENY check that actually passed."""
    names: set[str] = set()
    for check in _artifact()["checks"]:
        if check["expectation"] != "DENY" or not check["passed"]:
            continue
        names.update(re.findall(r"\b[A-Z][A-Z_]{4,}\b", check["detail"]))
    return names


def test_the_artifact_exists_and_is_self_consistent():
    a = _artifact()
    assert a["total"] == len(a["checks"])
    assert a["passed"] == sum(1 for c in a["checks"] if c["passed"])
    assert a["deny_checks"] == sum(1 for c in a["checks"] if c["expectation"] == "DENY")


def test_the_quoted_counts_match_the_artifact():
    """
    Both documents paste the verifier's output. If the artifact is regenerated
    with more probes, that pasted block goes stale and starts understating —
    harmless — or the artifact shrinks and it starts overstating.
    """
    a = _artifact()
    for doc in (SECURITY, README):
        text = doc.read_text()
        for allow_n, deny_n in re.findall(r"ALLOW: (\d+)/\d+.*?\n.*?DENY : (\d+)/(?:\d+)", text):
            assert int(allow_n) <= a["allow_passed"], (
                f"{doc.name} quotes ALLOW {allow_n}, artifact proves {a['allow_passed']}"
            )
            assert int(deny_n) <= a["deny_passed"], (
                f"{doc.name} quotes DENY {deny_n}, artifact proves {a['deny_passed']}"
            )


@pytest.mark.parametrize("doc_path", [SECURITY, README])
def test_no_document_calls_an_unproven_privilege_proven(doc_path):
    """
    The specific defect: naming a privilege in a sentence that asserts live
    verification, when no DENY check for it exists in the artifact.
    """
    proven = _proven_privileges()
    text = doc_path.read_text()

    # Sentences that assert verification, not merely absence of a grant.
    verified_claims = re.findall(
        r"[^.\n]*\b(?:verified as a live|proven denied against a live|each is verified)\b[^.\n]*",
        text,
    )
    offenders = []
    for sentence in verified_claims:
        for priv in re.findall(r"`([A-Z][A-Z_]{4,})`", sentence):
            if priv not in proven:
                offenders.append(f"{doc_path.name}: claims {priv} verified")

    assert not offenders, (
        "documentation claims live verification for privileges the artifact does "
        f"not prove. Proven: {sorted(proven)}. Offenders: {offenders}"
    )


def test_every_privilege_the_verifier_probes_is_named_in_security_md():
    """
    The converse drift: a probe added to the verifier that nobody documents is
    security work nobody gets credit for and nobody maintains.
    """
    probed = set(re.findall(r"\b(DELETE_ENTITY|EDIT_LINEAGE|MANAGE_POLICIES|"
                            r"MANAGE_INGESTION|EDIT_ENTITY_GLOSSARY_TERMS|"
                            r"EDIT_DOMAINS_PRIVILEGE)\b", VERIFIER.read_text()))
    text = SECURITY.read_text()
    missing = [p for p in probed if p not in text]
    assert not missing, f"verifier probes these but SECURITY.md never mentions them: {missing}"


def test_the_unprobeable_privilege_is_labelled_as_such():
    """
    EDIT_ENTITY_STATUS has no dataset-level mutation in DataHub's GraphQL —
    only updateUserStatus, which targets corp users. It must be present as an
    honest gap rather than quietly dropped, because dropping it would hide that
    the grant is absent but undemonstrable.
    """
    text = SECURITY.read_text()
    assert "EDIT_ENTITY_STATUS" in text
    assert "not probeable" in text.lower() or "no dataset-status mutation" in text.lower()


def test_the_scope_check_is_not_counted_as_a_privilege_denial():
    """
    One DENY case writes to an out-of-scope dataset. It proves the URN scope,
    not a privilege, and conflating the two is how "5 denials" came to imply
    "5 privileges refused".
    """
    scope_checks = [
        c for c in _artifact()["checks"]
        if c["expectation"] == "DENY" and "OUT-OF-SCOPE" in c["name"].upper()
    ]
    assert len(scope_checks) == 1
    assert "not a privilege" in SECURITY.read_text().lower()
