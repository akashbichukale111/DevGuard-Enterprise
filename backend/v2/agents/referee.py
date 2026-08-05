"""
referee.py — the Referee: validate the fix, then verify recovery. Deterministic.

Role: *"Referee | deterministic | test runner, verification queries | Validate the
fix; later, verify recovery."*

Two calls at two different moments, and the difference matters:

  * `validate(proposal)` runs **before** anyone approves anything, against a
    throwaway schema, from a copy of the project with the patch applied. The
    working tree is never touched, so a fix that does not work costs nothing and
    changes nothing.
  * `verify_recovery(failure)` runs **after** remediation and asks one question:
    *does the exact signal that failed now pass?* Not "do the tests pass", not
    "does it look right" — the specific command, the specific model, the
    specific column.

The write-back hard rule is what makes the second one load-bearing: *"Nothing is written
before recovery is verified. Demonstrate this: a deliberately failing fix writes
nothing."* The Scribe takes a `RecoveryReport` and refuses to write unless
`verified` is True, so the guarantee lives in a type rather than in a comment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from backend.v2.agents.watcher import RuntimeEvidenceProvider, RuntimeFailure
from backend.v2.evidence import (
    Evidence, EvidenceBuilder, EvidenceConfidence, EvidenceSource, EvidenceTrust,
)
from backend.v2.handoff import AgentDecision, AgentHandoff, now
from backend.v2.proofpack import ProofPack

#: Where a proposed fix is tried out. Dropped afterwards, always.
VALIDATION_SCHEMA = "analytics_devguard_check"


@dataclass(frozen=True)
class ValidationReport:
    """Did the PROPOSED fix build, in isolation, before anyone approved it?"""

    passed: bool
    schema: str
    command: str
    exit_code: int
    output_tail: str
    raw_ref: str = ""


@dataclass(frozen=True)
class RecoveryReport:
    """Does the exact signal that failed now pass?

    Carries the original failure so the comparison is explicit rather than
    remembered — "it passes now" is only meaningful against a recorded "it
    failed then".
    """

    verified: bool
    original_failure: str
    now_command: str
    now_exit_code: int
    now_passed_models: int
    now_errored_models: int
    output_tail: str
    raw_ref: str = ""


class Referee:
    """Runs real commands and reports real outcomes. No model, no interpretation."""

    NAME = "referee"

    def __init__(self, builder: EvidenceBuilder, pack: ProofPack,
                 dbt_dir: str | Path = "substrate/dbt",
                 dbt_bin: Optional[str] = None) -> None:
        self._builder = builder
        self._pack = pack
        self._dbt_dir = Path(dbt_dir)
        self._dbt_bin = dbt_bin or os.environ.get("DBT_BIN", "dbt")

    # ------------------------------------------------------------- step 13

    def validate(self, model_path: str, patched_sql: str,
                 repo_root: str | Path = ".") -> ValidationReport:
        """Build the proposed fix in a throwaway schema, from a copy of the project.

        Nothing about the real project or the real marts is touched. If the
        patch is wrong, the only casualty is a temporary schema that gets
        dropped either way.
        """
        repo_root = Path(repo_root)
        with tempfile.TemporaryDirectory(prefix="devguard-validate-") as tmp:
            staging = Path(tmp) / "dbt"
            shutil.copytree(repo_root / self._dbt_dir, staging,
                            ignore=shutil.ignore_patterns("target", "logs",
                                                          "dbt_packages"))
            # Apply the patch HERE, never in the working tree.
            target = staging / Path(model_path).relative_to(self._dbt_dir)
            target.write_text(patched_sql)

            env = dict(os.environ, DBT_PROFILES_DIR=".",
                       SUBSTRATE_DBT_SCHEMA=VALIDATION_SCHEMA)
            completed = subprocess.run(
                [self._dbt_bin, "run"], cwd=staging, env=env,
                capture_output=True, text=True, timeout=900,
            )
            output = _clean(completed.stdout + completed.stderr)

        self._drop_validation_schemas()

        raw_ref = self._pack.write(
            "referee/validation.txt",
            f"$ dbt run   (proposed fix, isolated schema {VALIDATION_SCHEMA})\n"
            f"exit code: {completed.returncode}\n\n{output[-8000:]}",
            note="The proposed fix built in a throwaway schema. Working tree untouched.",
        )
        return ValidationReport(
            passed=completed.returncode == 0, schema=VALIDATION_SCHEMA,
            command="dbt run", exit_code=completed.returncode,
            output_tail=output[-4000:], raw_ref=raw_ref,
        )

    # ------------------------------------------------------------- step 17

    def verify_recovery(self, original: RuntimeFailure,
                        repo_root: str | Path = ".") -> RecoveryReport:
        """Re-run the exact command that failed, against the real project."""
        env = dict(os.environ, DBT_PROFILES_DIR=".")
        failure = RuntimeEvidenceProvider(cwd=Path(repo_root) / self._dbt_dir).observe(
            [self._dbt_bin, "run"], env=env)

        raw_ref = self._pack.write(
            "referee/recovery.txt",
            f"$ {failure.command}   (AFTER remediation)\n"
            f"exit code: {failure.exit_code}\n\n{failure.stdout_tail}",
            note="The exact signal that failed, re-run after the fix was applied.",
        )
        return RecoveryReport(
            # Both conditions, deliberately. A zero exit with an errored model
            # would be a false green, and "PASS>0" alone would accept a run that
            # built nothing.
            verified=failure.exit_code == 0 and failure.errored == 0 and failure.passed > 0,
            original_failure=(f"{original.command} exited {original.exit_code}; "
                              f"{original.failing_model} failed on column "
                              f"{original.missing_column}"),
            now_command=failure.command, now_exit_code=failure.exit_code,
            now_passed_models=failure.passed, now_errored_models=failure.errored,
            output_tail=failure.stdout_tail[-4000:], raw_ref=raw_ref,
        )

    # ------------------------------------------------------------------ runs

    def run_validation(self, model_path: str, patched_sql: str, incident_id: str,
                       repo_root: str | Path = ".", *, to_agent: str = "magistrate"):
        started = now()
        report = self.validate(model_path, patched_sql, repo_root)
        evidence = [self._builder.make(
            source=EvidenceSource.RUNTIME,
            trust=EvidenceTrust.TRUSTED_SYSTEM,
            confidence=EvidenceConfidence.OBSERVED,
            claim=(f"proposed fix {'BUILDS' if report.passed else 'FAILS TO BUILD'} "
                   f"in isolated schema {report.schema} (exit {report.exit_code})"),
            raw_ref=report.raw_ref,
        )]
        handoff = AgentHandoff(
            from_agent=self.NAME, to_agent=to_agent, incident_id=incident_id,
            evidence_ids=tuple(e.id for e in evidence),
            decision=AgentDecision.OK if report.passed else AgentDecision.INSUFFICIENT_EVIDENCE,
            rationale=(f"Validated against the real database in {report.schema}."
                       if report.passed else
                       "The proposed fix does not build. Nothing is approved, applied "
                       "or written."),
            started_at=started, ended_at=now(), tokens=0, model=None,
        )
        return report, evidence, handoff

    def run_recovery(self, original: RuntimeFailure, incident_id: str,
                     repo_root: str | Path = ".", *, to_agent: str = "scribe"):
        started = now()
        report = self.verify_recovery(original, repo_root)
        evidence = [self._builder.make(
            source=EvidenceSource.RUNTIME,
            trust=EvidenceTrust.TRUSTED_SYSTEM,
            confidence=EvidenceConfidence.OBSERVED,
            claim=(f"RECOVERY {'VERIFIED' if report.verified else 'NOT VERIFIED'}: "
                   f"`{report.now_command}` exited {report.now_exit_code} "
                   f"(PASS={report.now_passed_models} ERROR={report.now_errored_models}); "
                   f"previously {report.original_failure[:60]}"),
            raw_ref=report.raw_ref,
        )]
        handoff = AgentHandoff(
            from_agent=self.NAME, to_agent=to_agent, incident_id=incident_id,
            evidence_ids=tuple(e.id for e in evidence),
            decision=AgentDecision.OK if report.verified else AgentDecision.INSUFFICIENT_EVIDENCE,
            rationale=("The exact signal that failed now passes."
                       if report.verified else
                       "Recovery NOT verified. Rule: nothing is written before recovery "
                       "is verified."),
            started_at=started, ended_at=now(), tokens=0, model=None,
        )
        return report, evidence, handoff

    # --------------------------------------------------------------- cleanup

    def _drop_validation_schemas(self) -> None:
        """Leave nothing behind. dbt creates <schema> and <schema>_<custom>."""
        try:
            import psycopg2
        except ImportError:  # pragma: no cover
            return
        env = os.environ
        try:
            conn = psycopg2.connect(
                host=env.get("SUBSTRATE_PG_HOST", "localhost"),
                port=int(env.get("SUBSTRATE_PG_PORT", "5433")),
                user=env.get("SUBSTRATE_PG_USER", "devguard"),
                password=env.get("SUBSTRATE_PG_PASSWORD", "devguard"),
                dbname=env.get("SUBSTRATE_PG_DB", "devguard"),
            )
        except Exception:  # pragma: no cover - cleanup must never break the loop
            return
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name LIKE %s", (f"{VALIDATION_SCHEMA}%",))
            for (schema,) in cur.fetchall():
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.close()


def _clean(text: str) -> str:
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", text)
