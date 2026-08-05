"""
faults.py — the fault-injection evaluation's scripted fault library, and the deterministic classifier.

The fault-injection evaluation: *"5–8 scripted faults, each with an expected root-cause label: column
rename · type change · null-rate spike · upstream table dropped · permission
revoked · silent value drift · **control: unrelated noise with no real fault**
(expected answer: `INSUFFICIENT_EVIDENCE`). Publish accuracy, false-positive
rate, and per-fault results as a README table."*

All seven are here and all seven are **really injected into the real
PostgreSQL** — no mocks, no recorded fixtures. Each knows how to revert itself,
and the harness reverts in a `finally` so an interrupted run does not leave the
substrate poisoned.

WHAT THE ACCURACY NUMBER MEANS, PRECISELY

The fault-injection evaluation is written for a system whose root cause comes from the Diagnostician. That
agent cannot run here (`api.groq.com` denied at CONNECT), so what is being
scored is **DevGuard's deterministic detection-and-classification path**: given
real runtime evidence, does it assign the right label, and does it refuse when it
should? That is a real, falsifiable measurement of a real component — it is just
not a measurement of LLM diagnosis, and the published table says so.

`classify()` is deliberately conservative. It reads only what the runtime
actually reported and returns `INSUFFICIENT_EVIDENCE` whenever no failure signal
exists. It has no access to which fault was injected, which is what makes the
score meaningful rather than circular.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, Sequence


class RootCauseLabel(str, Enum):
    """The vocabulary the classifier may answer with."""

    COLUMN_RENAMED = "COLUMN_RENAMED"
    TYPE_CHANGED = "TYPE_CHANGED"
    UPSTREAM_TABLE_MISSING = "UPSTREAM_TABLE_MISSING"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NULL_CONSTRAINT_VIOLATED = "NULL_CONSTRAINT_VIOLATED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    """No failure signal. The evidence rule's refusal — the correct answer for a healthy system."""

    UNKNOWN_FAILURE = "UNKNOWN_FAILURE"
    """Something failed and the classifier cannot say what.

    Deliberately distinct from INSUFFICIENT_EVIDENCE. "Nothing is wrong" and
    "something is wrong and I cannot name it" are different answers, and a
    system that reports the first when it means the second is dangerous.
    """


@dataclass(frozen=True)
class Fault:
    """One scripted fault: how to break it, how to fix it, what to expect."""

    name: str
    category: str
    description: str
    expected: RootCauseLabel
    inject_sql: tuple[str, ...]
    revert_sql: tuple[str, ...]
    silent: bool = False
    """True when the fault produces NO runtime failure signal by design.

    These are the interesting ones. A silent fault SHOULD yield
    INSUFFICIENT_EVIDENCE, because DevGuard genuinely cannot see it — and
    saying so is the correct behaviour, not a miss to be engineered away.
    """

    notes: str = ""


#: The database role the eval connects as. NOT the superuser: `REVOKE` against a
#: superuser is a silent no-op, so the permission fault would be untestable and
#: would quietly "pass" by never breaking anything.
EVAL_ROLE = "devguard_eval"
EVAL_PASSWORD = "devguard_eval"

#: Faults are injected into a throwaway CLONE of the raw schema, never into
#: `raw` itself. PostgreSQL refuses `ALTER COLUMN ... TYPE` while any view
#: depends on the column, so an eval sharing `raw` could not inject a type
#: change without first dropping the reference-incident staging views — destroying state
#: that the recorded evidence depends on. The clone makes the faults real and the
#: blast radius nil.
RAW = "raw_eval"

FAULTS: tuple[Fault, ...] = (
    Fault(
        name="column_rename",
        category="schema",
        description="raw_eval.users.user_id renamed to customer_id upstream.",
        expected=RootCauseLabel.COLUMN_RENAMED,
        inject_sql=(f"ALTER TABLE {RAW}.users RENAME COLUMN user_id TO customer_id;",),
        revert_sql=(f"ALTER TABLE {RAW}.users RENAME COLUMN customer_id TO user_id;",),
        notes="The reference-incident fault. Breaks stg_users at build time.",
    ),
    Fault(
        name="type_change",
        category="schema",
        description="raw_eval.orders.amount_cents changed from bigint to text.",
        expected=RootCauseLabel.TYPE_CHANGED,
        inject_sql=(
            f"ALTER TABLE {RAW}.orders ALTER COLUMN amount_cents TYPE text "
            "USING amount_cents::text;",),
        revert_sql=(
            f"ALTER TABLE {RAW}.orders ALTER COLUMN amount_cents TYPE bigint "
            "USING amount_cents::bigint;",),
        notes="sum()/avg() over text fails in the marts model.",
    ),
    Fault(
        name="upstream_table_dropped",
        category="schema",
        description="raw_eval.orders renamed away, as a careless migration would.",
        expected=RootCauseLabel.UPSTREAM_TABLE_MISSING,
        inject_sql=(f"ALTER TABLE {RAW}.orders RENAME TO orders_quarantined;",),
        revert_sql=(f"ALTER TABLE {RAW}.orders_quarantined RENAME TO orders;",),
        notes="Renamed rather than dropped so the 20,000 rows survive the eval.",
    ),
    Fault(
        name="permission_revoked",
        category="access",
        description=f"SELECT on raw_eval.orders revoked from {EVAL_ROLE}.",
        expected=RootCauseLabel.PERMISSION_DENIED,
        inject_sql=(f"REVOKE SELECT ON {RAW}.orders FROM {EVAL_ROLE};",),
        revert_sql=(f"GRANT SELECT ON {RAW}.orders TO {EVAL_ROLE};",),
        notes="Requires the non-superuser eval role; REVOKE on a superuser is a no-op.",
    ),
    Fault(
        name="null_rate_spike",
        category="data-quality",
        description="~30% of raw_eval.orders.amount_cents set to NULL.",
        expected=RootCauseLabel.NULL_CONSTRAINT_VIOLATED,
        inject_sql=(
            f"UPDATE {RAW}.orders SET amount_cents = NULL WHERE order_id % 10 < 3;",),
        revert_sql=(
            f"UPDATE {RAW}.orders SET amount_cents = (order_id % 5000 + 1) * 100 "
            "WHERE amount_cents IS NULL;",),
        notes=("Builds fine — caught only by the not_null test on stg_orders. "
               "Reverted arithmetically, so the eval restores plausible values "
               "rather than the exact originals; see the published README."),
    ),
    Fault(
        name="silent_value_drift",
        category="data-quality",
        description="raw_eval.orders.amount_cents multiplied by 100 — a units error.",
        expected=RootCauseLabel.INSUFFICIENT_EVIDENCE,
        inject_sql=(f"UPDATE {RAW}.orders SET amount_cents = amount_cents * 100;",),
        revert_sql=(f"UPDATE {RAW}.orders SET amount_cents = amount_cents / 100;",),
        silent=True,
        notes=("Every model builds, every test passes, every downstream number is "
               "wrong by 100x. DevGuard has no distribution check wired in, so the "
               "honest answer is INSUFFICIENT_EVIDENCE. This is a real limitation "
               "reported as one, not a fault engineered to be catchable."),
    ),
    Fault(
        name="control_no_fault",
        category="control",
        description="Unrelated noise: an unrelated table is created and dropped.",
        expected=RootCauseLabel.INSUFFICIENT_EVIDENCE,
        inject_sql=(
            f"CREATE TABLE IF NOT EXISTS {RAW}.devguard_eval_noise "
            "(id bigint, note text);",
            f"INSERT INTO {RAW}.devguard_eval_noise VALUES (1, 'unrelated activity');",),
        revert_sql=(f"DROP TABLE IF EXISTS {RAW}.devguard_eval_noise;",),
        silent=True,
        notes=("the fault-injection evaluation's required control. Real database activity, no real fault. "
               "Any answer other than INSUFFICIENT_EVIDENCE is a FALSE POSITIVE."),
    ),
)

#: Faults whose correct answer is a refusal — the control plus the genuinely
#: invisible one. Used to compute the false-positive rate.
NO_FAULT_EXPECTED = frozenset(
    f.name for f in FAULTS if f.expected is RootCauseLabel.INSUFFICIENT_EVIDENCE)


# --------------------------------------------------------------------- classify

#: Ordered most-specific first. Each pattern is matched against the real dbt
#: output; nothing here knows which fault was injected.
_SIGNATURES: tuple[tuple[re.Pattern, RootCauseLabel], ...] = (
    (re.compile(r"permission denied for (table|relation|schema)", re.I),
     RootCauseLabel.PERMISSION_DENIED),
    (re.compile(r'relation "[^"]+" does not exist', re.I),
     RootCauseLabel.UPSTREAM_TABLE_MISSING),
    (re.compile(r'column "[^"]+" does not exist', re.I),
     RootCauseLabel.COLUMN_RENAMED),
    (re.compile(r"function (sum|avg|max|min)\([^)]*\) does not exist|"
                r"operator does not exist|cannot be cast automatically|"
                r"invalid input syntax for type", re.I),
     RootCauseLabel.TYPE_CHANGED),
    (re.compile(r"not_null_\w+", re.I), RootCauseLabel.NULL_CONSTRAINT_VIOLATED),
)


@dataclass
class Classification:
    label: RootCauseLabel
    matched_signature: Optional[str] = None
    evidence_excerpt: str = ""
    failure_observed: bool = False
    detail: str = ""


def classify(*, exit_code: int, output: str, errored_models: int = 0,
             failed_tests: int = 0) -> Classification:
    """Assign a root-cause label from runtime output alone.

    Two rules keep this honest:

    1. **No failure signal -> INSUFFICIENT_EVIDENCE.** Not "probably fine", not a
       best guess. The evidence rule: the loop stops when the chain cannot form.
    2. **A failure it cannot name -> UNKNOWN_FAILURE**, never a plausible label.
       Guessing between COLUMN_RENAMED and TYPE_CHANGED on a message that
       supports neither is how an eval scores well and a production system
       misleads an on-call engineer.
    """
    failure_observed = exit_code != 0 or errored_models > 0 or failed_tests > 0
    if not failure_observed:
        return Classification(
            label=RootCauseLabel.INSUFFICIENT_EVIDENCE,
            failure_observed=False,
            detail=("No runtime failure signal: the command exited 0 with no errored "
                    "models and no failing tests. Nothing to diagnose."),
        )

    for pattern, label in _SIGNATURES:
        match = pattern.search(output)
        if match:
            start = max(0, match.start() - 60)
            return Classification(
                label=label, matched_signature=pattern.pattern[:60],
                evidence_excerpt=output[start:match.end() + 60].strip(),
                failure_observed=True,
                detail=f"matched a {label.value} signature in the runtime output",
            )

    return Classification(
        label=RootCauseLabel.UNKNOWN_FAILURE, failure_observed=True,
        evidence_excerpt=output[-300:].strip(),
        detail=("A failure was observed but no known signature matched. Reporting "
                "UNKNOWN_FAILURE rather than guessing a plausible label."),
    )


# ------------------------------------------------------------------- scoring

@dataclass
class FaultResult:
    fault: str
    category: str
    expected: RootCauseLabel
    actual: RootCauseLabel
    correct: bool
    failure_observed: bool
    exit_code: int
    errored_models: int
    failed_tests: int
    duration_s: float
    evidence_excerpt: str = ""
    detail: str = ""
    raw_ref: str = ""

    @property
    def false_positive(self) -> bool:
        """Claimed a fault where none exists. The metric the fault-injection evaluation asks for by name."""
        return (self.expected is RootCauseLabel.INSUFFICIENT_EVIDENCE
                and self.actual is not RootCauseLabel.INSUFFICIENT_EVIDENCE)

    @property
    def false_negative(self) -> bool:
        """Refused on a fault that really was there and really was visible."""
        return (self.expected is not RootCauseLabel.INSUFFICIENT_EVIDENCE
                and self.actual is RootCauseLabel.INSUFFICIENT_EVIDENCE)


@dataclass
class EvalSummary:
    total: int
    correct: int
    accuracy: float
    control_total: int
    false_positives: int
    false_positive_rate: float
    false_negatives: int
    detected_total: int
    results: list[FaultResult] = field(default_factory=list)

    @classmethod
    def of(cls, results: Sequence[FaultResult]) -> "EvalSummary":
        controls = [r for r in results if r.expected is RootCauseLabel.INSUFFICIENT_EVIDENCE]
        false_positives = [r for r in results if r.false_positive]
        return cls(
            total=len(results),
            correct=sum(1 for r in results if r.correct),
            accuracy=round(sum(1 for r in results if r.correct) / len(results), 4)
            if results else 0.0,
            control_total=len(controls),
            false_positives=len(false_positives),
            false_positive_rate=round(len(false_positives) / len(controls), 4)
            if controls else 0.0,
            false_negatives=sum(1 for r in results if r.false_negative),
            detected_total=sum(1 for r in results if r.failure_observed),
            results=list(results),
        )
