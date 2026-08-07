"""
Catalog-corroborated recovery — a second opinion from DataHub's own assertions.

The problem this solves
-----------------------
The Referee declares recovery by re-running the exact command that failed and
observing that it now passes. That is real evidence, and it is *one source*:
DevGuard proposes the fix, DevGuard runs the test, DevGuard grades the result.
An incident is then resolved in DataHub on the strength of a check the same
system performed on itself.

Data platforms already have an independent judge of whether a dataset is
healthy — the assertions attached to it in the catalog. In a dbt substrate
those are the ingested dbt tests: authored by the data team, evaluated by the
platform, recorded against the dataset. They are exactly the second source, and
nothing here was reading them.

So this asks DataHub a question DevGuard cannot answer about itself: *does the
catalog agree the asset is healthy?*

The rule
--------
Two independent sources must agree before recovery is corroborated. Concretely:

  * the Referee's runtime check passed, **and**
  * every assertion DataHub holds for the dataset has a most-recent run that
    succeeded.

Anything else is `NOT_CORROBORATED`, with the reason attached.

The case that matters most is the boring one: **a dataset with no assertions is
not corroborated.** It is tempting to treat "nothing failed" as "everything
passed", and that is the single most flattering mistake available here — it
would silently upgrade every asset with no data-quality coverage into a
verified one. Absence of evidence is not evidence, and this module says so
rather than defaulting to a pass.

Why GraphQL and not MCP
-----------------------
`mcp-server-datahub` publishes no assertion tool (see `mcp_contract.py`), so
assertions are reachable only over GraphQL — the same situation as incidents,
which the Scribe already handles with an injected `gql` transport. This reuses
that transport rather than inventing a second integration path.

Why this is read-only
---------------------
Reading assertions needs no privilege beyond the reads the service account
already has, so none of the least-privilege evidence changes. Writing assertion
*results* back would need `reportAssertionResult`, which is not in DataHub OSS's
GraphQL schema — it is a DataHub Cloud surface. Emitting an `assertionRunEvent`
aspect through the ingestion REST path would work but is a second write channel
with its own credential and its own blast radius, and the single-writer rule
(only the Scribe mutates) exists precisely to prevent that from being added
casually. So this corroborates; it does not author.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, Sequence

from backend.v2.evidence import (
    Evidence,
    EvidenceBuilder,
    EvidenceConfidence,
    EvidenceSource,
    EvidenceTrust,
)

#: How many assertions to pull for one dataset. A dataset with more than this
#: many is unusual; the count is reported so a truncated read is visible rather
#: than silently narrowing the corroboration.
MAX_ASSERTIONS = 50

#: Most-recent run events to inspect per assertion. Only the latest is used for
#: the verdict; the rest give a reviewer the trend without a second round trip.
MAX_RUN_EVENTS = 5

#: The exact query. Field names verified against DataHub's published schema
#: (`datahub-graphql-core/src/main/resources/entity.graphql`) rather than
#: guessed — `Dataset.assertions` -> `EntityAssertionsResult`, and
#: `Assertion.runEvents` -> `AssertionRunEventsResult`.
ASSERTIONS_QUERY = """
query datasetAssertions($urn: String!, $count: Int!, $limit: Int!) {
  dataset(urn: $urn) {
    urn
    assertions(start: 0, count: $count) {
      start
      count
      total
      assertions {
        urn
        info { type description }
        runEvents(status: COMPLETE, limit: $limit) {
          total
          failed
          succeeded
          errored
          runEvents {
            timestampMillis
            runId
            status
            result { type }
          }
        }
      }
    }
  }
}
"""


class Corroboration(str, Enum):
    """Verdict of the catalog's own opinion."""

    #: Every assertion DataHub holds most recently succeeded.
    CORROBORATED = "CORROBORATED"

    #: At least one assertion's most recent run did not succeed.
    CONTRADICTED = "CONTRADICTED"

    #: The catalog cannot answer — no assertions, no run history, or the query
    #: failed. Never treated as agreement.
    NOT_CORROBORATED = "NOT_CORROBORATED"


@dataclass(frozen=True)
class AssertionOutcome:
    """One assertion and the verdict of its most recent completed run."""

    urn: str
    assertion_type: Optional[str]
    description: Optional[str]
    latest_result: Optional[str]     # SUCCESS | FAILURE | INIT | None
    latest_run_id: Optional[str]
    latest_timestamp_ms: Optional[int]
    total_runs: int

    @property
    def passed(self) -> bool:
        return self.latest_result == "SUCCESS"

    @property
    def has_history(self) -> bool:
        return self.latest_result is not None


@dataclass(frozen=True)
class CorroborationReport:
    """What the catalog says about a dataset's health, and why."""

    dataset_urn: str
    verdict: Corroboration
    reason: str
    outcomes: tuple[AssertionOutcome, ...] = field(default_factory=tuple)
    total_assertions: int = 0
    truncated: bool = False

    @property
    def corroborated(self) -> bool:
        return self.verdict is Corroboration.CORROBORATED

    @property
    def failing(self) -> tuple[AssertionOutcome, ...]:
        return tuple(o for o in self.outcomes if o.has_history and not o.passed)

    @property
    def without_history(self) -> tuple[AssertionOutcome, ...]:
        return tuple(o for o in self.outcomes if not o.has_history)

    def summary(self) -> str:
        passed = sum(1 for o in self.outcomes if o.passed)
        return (f"{self.verdict.value}: {passed}/{len(self.outcomes)} assertions "
                f"most recently succeeded — {self.reason}")


class AssertionCorroborator:
    """
    Reads a dataset's assertions from DataHub and judges whether the catalog
    agrees the asset is healthy.

    Never raises. A corroborator that throws would turn "the catalog could not
    be reached" into a failed incident response, when the correct behaviour is
    to record that the second opinion is unavailable and let the write-back
    rules decide what that means.
    """

    NAME = "referee"

    def __init__(self, gql: Optional[Callable[[str, dict], dict]], pack=None) -> None:
        self._gql = gql
        self._pack = pack

    # ------------------------------------------------------------------ read

    def corroborate(self, dataset_urn: str) -> CorroborationReport:
        if self._gql is None:
            return CorroborationReport(
                dataset_urn=dataset_urn,
                verdict=Corroboration.NOT_CORROBORATED,
                reason=("no GraphQL transport was supplied; assertions are "
                        "GraphQL-only — mcp-server-datahub publishes no assertion tool"),
            )

        try:
            payload = self._gql(
                ASSERTIONS_QUERY,
                {"urn": dataset_urn, "count": MAX_ASSERTIONS, "limit": MAX_RUN_EVENTS},
            )
        except Exception as exc:  # noqa: BLE001 — see class docstring
            return self._record(CorroborationReport(
                dataset_urn=dataset_urn,
                verdict=Corroboration.NOT_CORROBORATED,
                reason=f"assertion query failed: {type(exc).__name__}",
            ), raw={"error": repr(exc)})

        if payload.get("errors"):
            return self._record(CorroborationReport(
                dataset_urn=dataset_urn,
                verdict=Corroboration.NOT_CORROBORATED,
                reason="assertion query returned GraphQL errors",
            ), raw=payload)

        dataset = ((payload.get("data") or {}).get("dataset")) or {}
        block = dataset.get("assertions") or {}
        raw_assertions: Sequence[dict] = block.get("assertions") or []
        total = int(block.get("total") or 0)

        outcomes = tuple(self._outcome(a) for a in raw_assertions)
        truncated = total > len(outcomes)

        report = CorroborationReport(
            dataset_urn=dataset_urn,
            verdict=self._verdict(outcomes),
            reason=self._reason(outcomes, truncated),
            outcomes=outcomes,
            total_assertions=total,
            truncated=truncated,
        )
        return self._record(report, raw=payload)

    # ------------------------------------------------------------ judgement

    @staticmethod
    def _verdict(outcomes: Sequence[AssertionOutcome]) -> Corroboration:
        if not outcomes:
            # The flattering mistake, refused. See the module docstring.
            return Corroboration.NOT_CORROBORATED
        if any(o.has_history and not o.passed for o in outcomes):
            return Corroboration.CONTRADICTED
        if any(not o.has_history for o in outcomes):
            # An assertion that has never run cannot vouch for anything, and a
            # partial answer must not be reported as a whole one.
            return Corroboration.NOT_CORROBORATED
        return Corroboration.CORROBORATED

    @staticmethod
    def _reason(outcomes: Sequence[AssertionOutcome], truncated: bool) -> str:
        if not outcomes:
            return ("the catalog holds no assertions for this dataset, so it "
                    "cannot corroborate recovery — absence of a failure is not "
                    "a pass")
        failing = [o for o in outcomes if o.has_history and not o.passed]
        if failing:
            names = ", ".join(o.description or o.urn for o in failing[:3])
            return f"{len(failing)} assertion(s) most recently failed: {names}"
        never = [o for o in outcomes if not o.has_history]
        if never:
            return (f"{len(never)} of {len(outcomes)} assertion(s) have no completed "
                    "run, so the catalog's answer is incomplete")
        base = f"all {len(outcomes)} assertion(s) most recently succeeded"
        return base + (" (assertion list truncated)" if truncated else "")

    @staticmethod
    def _outcome(raw: dict) -> AssertionOutcome:
        info = raw.get("info") or {}
        events_block = raw.get("runEvents") or {}
        events = events_block.get("runEvents") or []
        latest = events[0] if events else None
        result = (latest or {}).get("result") or {}
        return AssertionOutcome(
            urn=raw.get("urn") or "",
            assertion_type=info.get("type"),
            description=info.get("description"),
            latest_result=result.get("type"),
            latest_run_id=(latest or {}).get("runId"),
            latest_timestamp_ms=(latest or {}).get("timestampMillis"),
            total_runs=int(events_block.get("total") or 0),
        )

    # -------------------------------------------------------------- evidence

    def _record(self, report: CorroborationReport, *, raw: Any) -> CorroborationReport:
        if self._pack is not None:
            self._pack.write(
                "referee/assertion-corroboration.json",
                {"dataset_urn": report.dataset_urn,
                 "verdict": report.verdict.value,
                 "reason": report.reason,
                 "total_assertions": report.total_assertions,
                 "response": raw},
                note=("DataHub's own assertions, read as an independent second "
                      "opinion on recovery. Read-only."),
            )
        return report

    def evidence(self, builder: EvidenceBuilder, report: CorroborationReport,
                 raw_ref: str) -> Evidence:
        """
        Typed evidence for the chain.

        DATAHUB_GRAPH, because it came from the catalog — but **only when the
        catalog actually answered**.

        `EvidenceChain.is_sufficient()` requires at least one RUNTIME and at
        least one DATAHUB_GRAPH item, and it checks `source` alone. Confidence
        does not enter into it. So minting a NOT_CORROBORATED reading as
        DATAHUB_GRAPH would let "the catalog told us nothing" satisfy the very
        rule that exists to force two independent sources — the chain would go
        sufficient on the strength of a failed lookup.

        Lowering its confidence is not enough to prevent that, so this refuses
        outright. A caller that wants the null reading on the record has it in
        the proof pack already; what it may not do is spend it as evidence.
        """
        if report.verdict is Corroboration.NOT_CORROBORATED:
            raise ValueError(
                "refusing to mint chain evidence for a NOT_CORROBORATED reading: "
                "is_sufficient() counts DATAHUB_GRAPH by source alone, so this "
                f"would satisfy the evidence rule with a failed lookup ({report.reason})"
            )
        confidence = {
            # Both of these were read directly off the catalog's own run events.
            Corroboration.CORROBORATED: EvidenceConfidence.OBSERVED,
            Corroboration.CONTRADICTED: EvidenceConfidence.OBSERVED,
            # Nothing was observed. INFERRED is the weakest rung and the honest
            # one — the claim is about what the catalog could not tell us.
            Corroboration.NOT_CORROBORATED: EvidenceConfidence.INFERRED,
        }[report.verdict]
        return builder.make(
            source=EvidenceSource.DATAHUB_GRAPH,
            # The catalog's structured run events, not free text a user wrote,
            # so this does not go through the Sentinel's fencing path.
            trust=EvidenceTrust.TRUSTED_SYSTEM,
            confidence=confidence,
            claim=report.summary()[:200],
            raw_ref=raw_ref,
            datahub_urn=report.dataset_urn,
        )


def recovery_is_agreed(runtime_passed: bool, report: CorroborationReport) -> tuple[bool, str]:
    """
    The two-source rule, in one place so the Scribe and the tests cannot drift.

    Returns `(agreed, reason)`. Both sources must say yes. A contradiction from
    the catalog outranks a local pass — DevGuard grading its own homework is
    exactly the situation the second source exists to check.
    """
    if not runtime_passed:
        return False, "the runtime re-run did not pass"
    if report.verdict is Corroboration.CONTRADICTED:
        return False, f"the catalog contradicts recovery: {report.reason}"
    if report.verdict is Corroboration.NOT_CORROBORATED:
        return False, f"the catalog cannot corroborate recovery: {report.reason}"
    return True, "the runtime re-run passed and every catalog assertion agrees"
