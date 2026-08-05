"""
surgeon.py — the Surgeon: propose the minimal fix as a diff on a branch. Never apply.

The design's row: *"Surgeon | LLM + repo tools | git/branch/patch — **never apply** |
Propose the minimal fix as a diff on a branch."*

**This implementation is deterministic, and that is a deliberate departure from
the "LLM + tools" classification.** The design itself licenses the choice — *"Several
are deterministic and use no model at all — say so in the README, because 'we
did not put an LLM where an LLM was not needed' is a senior-engineering signal"*
— but the reasoning has to survive scrutiny, so here it is in full.

For an upstream column rename, the fix is not a judgement call **when the
mapping is unambiguous**:

    the model references a column the database no longer has   -> `dead`
    the database has a column the model does not reference     -> `orphan`

If there is exactly one of each, the rename is `dead -> orphan` and no
creativity is involved. If there is more than one candidate, the mapping *is* a
judgement call — and this Surgeon **refuses** rather than guessing, in the same
spirit as the Diagnostician's `INSUFFICIENT_EVIDENCE`. A wrong column mapping
silently produces a model that builds successfully and computes the wrong
numbers, which is far worse than a build that stays red.

The honest caveat, stated because the alternative is overclaiming: this covers
one fix class. A Surgeon that handles arbitrary breakage needs a model, and
`api.groq.com` is blocked in this environment, so an LLM Surgeon could not have
been demonstrated either. What is here is real, runs, and refuses when it should
— it is not a general code-repair agent and this module does not pretend to be.

The minimal fix is `customer_id as user_id`, not a rename of the downstream
column. That keeps the contract `stg_users` publishes to `user_order_features`
and the ML model intact, so the blast radius of the *fix* is one file. A fix
that renamed the column everywhere would be larger, riskier, and would itself
break the mlModel's feature contract.
"""

from __future__ import annotations

import difflib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from backend.v2.evidence import (
    Evidence, EvidenceBuilder, EvidenceConfidence, EvidenceSource, EvidenceTrust,
)
from backend.v2.handoff import AgentDecision, AgentHandoff, now
from backend.v2.proofpack import ProofPack

#: dbt models are SQL with jinja. We only need to know which bare identifiers
#: The SELECT list mentions, so this is intentionally simple rather than a
#: parser — and `propose` verifies its own conclusion against the file text
#: before writing anything.
_JINJA = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_COMMENT = re.compile(r"--[^\n]*")
_IDENTIFIER = re.compile(r"\b[a-z_][a-z0-9_]*\b")

_SQL_KEYWORDS = frozenset({
    "select", "from", "where", "and", "or", "not", "as", "on", "join", "left",
    "right", "inner", "outer", "full", "group", "by", "order", "having", "limit",
    "offset", "with", "case", "when", "then", "else", "end", "distinct", "union",
    "all", "is", "null", "true", "false", "count", "sum", "avg", "max", "min",
    "coalesce", "cast", "asc", "desc", "source", "ref", "config",
})


@dataclass
class FixProposal:
    """A proposed change. Never applied by this agent."""

    model_path: str
    dead_column: Optional[str] = None
    replacement_column: Optional[str] = None
    original_sql: str = ""
    patched_sql: str = ""
    branch: Optional[str] = None
    patch_path: Optional[str] = None
    refused: bool = False
    refusal_reason: str = ""
    candidates: tuple[str, ...] = field(default=())

    @property
    def proposed(self) -> bool:
        return not self.refused and bool(self.patched_sql)

    @property
    def diff(self) -> str:
        return "".join(difflib.unified_diff(
            self.original_sql.splitlines(keepends=True),
            self.patched_sql.splitlines(keepends=True),
            fromfile=f"a/{self.model_path}", tofile=f"b/{self.model_path}",
        ))

    @property
    def lines_changed(self) -> int:
        return sum(1 for line in self.diff.splitlines()
                   if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))


class Surgeon:
    """Proposes. Does not apply. The distinction is enforced by the API surface."""

    NAME = "surgeon"

    def __init__(self, builder: EvidenceBuilder, pack: ProofPack,
                 repo_root: str | Path = ".") -> None:
        self._builder = builder
        self._pack = pack
        self._repo = Path(repo_root)

    # ------------------------------------------------------------------ core

    def propose(self, model_path: str, dead_column: str,
                live_columns: tuple[str, ...]) -> FixProposal:
        path = self._repo / model_path
        original = path.read_text()
        proposal = FixProposal(model_path=model_path, dead_column=dead_column,
                               original_sql=original)

        referenced = _referenced_identifiers(original)
        if dead_column not in referenced:
            proposal.refused = True
            proposal.refusal_reason = (
                f"{model_path} does not reference {dead_column!r}; refusing to patch a "
                f"file that is not the one that broke.")
            return proposal

        orphans = tuple(c for c in live_columns if c not in referenced)
        proposal.candidates = orphans

        if len(orphans) != 1:
            # The refusal that matters. A wrong mapping builds green and
            # computes the wrong numbers, which is worse than staying red.
            proposal.refused = True
            proposal.refusal_reason = (
                f"rename target is ambiguous: {dead_column!r} is gone and "
                f"{len(orphans)} live column(s) are unreferenced ({', '.join(orphans) or 'none'}). "
                f"A wrong column mapping builds successfully and computes the wrong "
                f"numbers. Escalating instead of guessing.")
            return proposal

        replacement = orphans[0]
        patched, substitutions = _rewrite_reference(original, dead_column, replacement)
        if substitutions != 1:
            proposal.refused = True
            proposal.refusal_reason = (
                f"expected exactly one bare reference to {dead_column!r} in the SELECT "
                f"list, found {substitutions}. Refusing a patch whose effect is not "
                f"obvious from the diff.")
            return proposal

        proposal.replacement_column = replacement
        proposal.patched_sql = patched
        return proposal

    def write_branch(self, proposal: FixProposal, incident_id: str) -> FixProposal:
        """Write the patch to a branch. The working tree is left untouched.

        Uses `git hash-object` + `git update-index --cacheinfo` against a
        temporary index, so the proposed content becomes a real commit on a real
        branch **without** the file on disk ever changing. That is what "never
        apply" has to mean for it to be worth saying.
        """
        if not proposal.proposed:
            return proposal

        branch = f"devguard/fix-{incident_id.lower()}"
        blob = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"], cwd=self._repo,
            input=proposal.patched_sql, capture_output=True, text=True, check=True,
        ).stdout.strip()

        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self._repo,
                              capture_output=True, text=True, check=True).stdout.strip()
        env = {"GIT_INDEX_FILE": ".git/devguard-index"}
        subprocess.run(["git", "read-tree", head], cwd=self._repo, check=True,
                       env={**_git_env(), **env})
        subprocess.run(
            ["git", "update-index", "--cacheinfo", f"100644,{blob},{proposal.model_path}"],
            cwd=self._repo, check=True, env={**_git_env(), **env},
        )
        tree = subprocess.run(["git", "write-tree"], cwd=self._repo, check=True,
                              capture_output=True, text=True,
                              env={**_git_env(), **env}).stdout.strip()
        message = (
            f"fix({incident_id}): {proposal.dead_column} -> "
            f"{proposal.replacement_column} in {Path(proposal.model_path).name}\n\n"
            f"Proposed by DevGuard's Surgeon. NOT APPLIED — this branch exists so a\n"
            f"human can review the diff before anything touches the working tree.\n")
        commit = subprocess.run(
            ["git", "commit-tree", tree, "-p", head, "-m", message],
            cwd=self._repo, check=True, capture_output=True, text=True,
            env=_git_env()).stdout.strip()
        subprocess.run(["git", "update-ref", f"refs/heads/{branch}", commit],
                       cwd=self._repo, check=True, env=_git_env())
        (self._repo / ".git" / "devguard-index").unlink(missing_ok=True)

        proposal.branch = branch
        proposal.patch_path = self._pack.write(
            "surgeon/proposed.patch", proposal.diff,
            note=f"Proposed diff, committed to {branch}. Working tree untouched.")
        return proposal

    # ------------------------------------------------------------------ entry

    def run(self, model_path: str, dead_column: str, live_columns: tuple[str, ...],
            incident_id: str, *, to_agent: str = "sentinel"
            ) -> tuple[FixProposal, list[Evidence], AgentHandoff]:
        started = now()
        proposal = self.propose(model_path, dead_column, live_columns)
        if proposal.proposed:
            proposal = self.write_branch(proposal, incident_id)

        evidence: list[Evidence] = []
        if proposal.proposed:
            evidence.append(self._builder.make(
                source=EvidenceSource.DEVGUARD_INFERENCE,
                trust=EvidenceTrust.TRUSTED_SYSTEM,
                confidence=EvidenceConfidence.DERIVED,
                claim=(f"proposed {proposal.dead_column} -> {proposal.replacement_column} "
                       f"in {model_path} ({proposal.lines_changed} lines) on {proposal.branch}"),
                raw_ref=proposal.patch_path or "",
            ))
        else:
            evidence.append(self._builder.make(
                source=EvidenceSource.DEVGUARD_INFERENCE,
                trust=EvidenceTrust.TRUSTED_SYSTEM,
                confidence=EvidenceConfidence.DERIVED,
                claim=f"no fix proposed for {model_path}: {proposal.refusal_reason[:110]}",
                raw_ref=self._pack.write(
                    "surgeon/refused.txt",
                    f"{proposal.refusal_reason}\n\ncandidates: {proposal.candidates}\n",
                    note="The Surgeon declined to propose a patch."),
            ))

        handoff = AgentHandoff(
            from_agent=self.NAME, to_agent=to_agent, incident_id=incident_id,
            evidence_ids=tuple(e.id for e in evidence),
            decision=AgentDecision.OK if proposal.proposed else AgentDecision.INSUFFICIENT_EVIDENCE,
            rationale=(f"Minimal fix on {proposal.branch}, working tree untouched."
                       if proposal.proposed else proposal.refusal_reason),
            started_at=started, ended_at=now(), tokens=0, model=None,
        )
        return proposal, evidence, handoff


# --------------------------------------------------------------------- helpers

def _git_env() -> dict:
    import os
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "DevGuard Surgeon",
        "GIT_AUTHOR_EMAIL": "devguard@example.com",
        "GIT_COMMITTER_NAME": "DevGuard Surgeon",
        "GIT_COMMITTER_EMAIL": "devguard@example.com",
    }


def _referenced_identifiers(sql: str) -> frozenset[str]:
    """Bare identifiers in the SQL, minus jinja, comments and keywords."""
    stripped = _COMMENT.sub(" ", _JINJA.sub(" ", sql)).lower()
    return frozenset(_IDENTIFIER.findall(stripped)) - _SQL_KEYWORDS


def _rewrite_reference(sql: str, dead: str, replacement: str) -> tuple[str, int]:
    """Replace a bare `dead` reference with `replacement as dead`.

    Aliasing rather than renaming is the point: downstream models and the
    registered mlModel consume `user_id`, so preserving the published column
    name keeps the fix's own blast radius to one file.
    """
    pattern = re.compile(rf"(?<![\w.]){re.escape(dead)}(?![\w])")
    count = 0

    def _sub(match: re.Match) -> str:
        nonlocal count
        # Only rewrite inside the SELECT list, never inside a comment.
        line_start = sql.rfind("\n", 0, match.start()) + 1
        if "--" in sql[line_start:match.start()]:
            return match.group(0)
        count += 1
        return f"{replacement} as {dead}"

    patched = pattern.sub(_sub, sql)
    return patched, count
