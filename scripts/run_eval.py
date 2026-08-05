#!/usr/bin/env python
"""
run_eval.py — the fault-injection evaluation's fault-injection evaluation suite. `make eval` runs this.

Seven scripted faults, each **really injected into the real PostgreSQL**, each
followed by a real `dbt build` against the real substrate, each classified from
the real output, each reverted in a `finally`.

    DBT_BIN=<dbt> python scripts/run_eval.py

Publishes accuracy, false-positive rate and per-fault results to
`examples/eval/` (the fault-injection evaluation), rendered from `results.json` so no number in the
published table is typed by hand (the generated-numbers rule).

ISOLATION, AND WHY IT MATTERS HERE

Faults are injected into `raw_eval`, a real clone of `raw` built at the start of
each run, and models are built into `analytics_eval*` as the non-superuser role
`devguard_eval`. Nothing the eval does touches `raw` or the reference-incident marts.
Three reasons, each learned the hard way:

  * `REVOKE` against a superuser is a silent no-op, so the permission fault
    would be untestable and would "pass" by never breaking anything;
  * PostgreSQL refuses `ALTER COLUMN ... TYPE` while any view depends on the
    column, so an eval sharing `raw` cannot inject a type change without first
    dropping the production staging views;
  * seven fault injections against the real substrate would leave the recorded
    evidence unreproducible.

Everything the eval creates — the clone, the role, the build schemas — is
dropped in a `finally`.

WHAT THE ACCURACY NUMBER IS A MEASUREMENT OF

The fault-injection evaluation assumes the root cause comes from the Diagnostician. It cannot run here, so
this scores DevGuard's **deterministic detection-and-classification path**. The
classifier sees only the runtime output — never which fault was injected — so
the score is falsifiable, but it is not a measurement of LLM diagnosis and the
published table says so in its first paragraph.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.v2.faults import (
    EVAL_PASSWORD, EVAL_ROLE, FAULTS, EvalSummary, Fault, FaultResult,
    RootCauseLabel, classify,
)
from backend.v2.proofpack import ProofPack

EVAL_SCHEMA = "analytics_eval"
EVAL_RAW_SCHEMA = "raw_eval"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_TOTALS = re.compile(
    r"Done\. PASS=(?P<passed>\d+) WARN=(?P<warn>\d+) ERROR=(?P<error>\d+) "
    r"SKIP=(?P<skip>\d+)")


def admin_dsn() -> dict:
    env = os.environ
    return {
        "host": env.get("SUBSTRATE_PG_HOST", "localhost"),
        "port": int(env.get("SUBSTRATE_PG_PORT", "5433")),
        "user": env.get("SUBSTRATE_PG_USER", "devguard"),
        "password": env.get("SUBSTRATE_PG_PASSWORD", "devguard"),
        "dbname": env.get("SUBSTRATE_PG_DB", "devguard"),
    }


def sql(statements, *, autocommit: bool = True) -> None:
    import psycopg2
    conn = psycopg2.connect(**admin_dsn())
    conn.autocommit = autocommit
    try:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
    finally:
        conn.close()


def setup_role() -> None:
    """Create the non-superuser role the eval builds as."""
    sql([
        f"DROP OWNED BY {EVAL_ROLE} CASCADE;" if _role_exists() else "SELECT 1;",
        f"DROP ROLE IF EXISTS {EVAL_ROLE};",
        f"CREATE ROLE {EVAL_ROLE} LOGIN PASSWORD '{EVAL_PASSWORD}';",
        f"GRANT CONNECT ON DATABASE {admin_dsn()['dbname']} TO {EVAL_ROLE};",
        f"GRANT CREATE ON DATABASE {admin_dsn()['dbname']} TO {EVAL_ROLE};",
        f"GRANT USAGE ON SCHEMA {EVAL_RAW_SCHEMA} TO {EVAL_ROLE};",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA {EVAL_RAW_SCHEMA} TO {EVAL_ROLE};",
    ])


def _role_exists() -> bool:
    import psycopg2
    conn = psycopg2.connect(**admin_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (EVAL_ROLE,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def teardown_role() -> None:
    sql([f"DROP SCHEMA IF EXISTS {EVAL_SCHEMA} CASCADE;",
         f"DROP SCHEMA IF EXISTS {EVAL_SCHEMA}_staging CASCADE;",
         f"DROP SCHEMA IF EXISTS {EVAL_SCHEMA}_marts CASCADE;",
         f"DROP OWNED BY {EVAL_ROLE} CASCADE;",
         f"DROP ROLE IF EXISTS {EVAL_ROLE};"])


def clone_raw_schema() -> None:
    """Build `raw_eval` as a real copy of `raw`, healed to the pre-incident state.

    Real tables with real rows — the faults below are genuine DDL and DML — but
    entirely separate from `raw`, so the eval cannot disturb the reference-incident
    substrate or the production staging views. It also removes the need to heal
    and re-break `raw` around every run, which was the previous design and was
    one interrupted process away from leaving the substrate in a state no phase
    expects.

    The clone is healed (`user_id`, not `customer_id`) because the fault-injection evaluation scores
    detection of faults it injects, and starting from an already-broken source
    would contaminate every single one of them.
    """
    import psycopg2
    conn = psycopg2.connect(**admin_dsn())
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {EVAL_RAW_SCHEMA} CASCADE;")
            cur.execute(f"CREATE SCHEMA {EVAL_RAW_SCHEMA};")
            for table in ("users", "orders"):
                cur.execute(
                    f"CREATE TABLE {EVAL_RAW_SCHEMA}.{table} AS "
                    f"SELECT * FROM raw.{table};")
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name='users'", (EVAL_RAW_SCHEMA,))
            columns = {r[0] for r in cur.fetchall()}
            if "customer_id" in columns and "user_id" not in columns:
                cur.execute(f"ALTER TABLE {EVAL_RAW_SCHEMA}.users "
                            f"RENAME COLUMN customer_id TO user_id;")
            # Keys the tests assert on. CREATE TABLE AS copies data, not
            # constraints, so they are re-added here rather than assumed.
            cur.execute(f"ALTER TABLE {EVAL_RAW_SCHEMA}.users "
                        f"ADD PRIMARY KEY (user_id);")
            cur.execute(f"ALTER TABLE {EVAL_RAW_SCHEMA}.orders "
                        f"ADD PRIMARY KEY (order_id);")
    finally:
        conn.close()


def drop_raw_clone() -> None:
    sql([f"DROP SCHEMA IF EXISTS {EVAL_RAW_SCHEMA} CASCADE;"])


def dbt_build(dbt_dir: Path, dbt_bin: str) -> tuple[int, str, int, int]:
    """Run `dbt build` as the eval role into the eval schema.

    `build` rather than `run`, because it runs the schema tests too — without
    them a data-quality fault produces no signal at all and the eval would be
    scoring schema faults only.
    """
    env = dict(
        os.environ,
        DBT_PROFILES_DIR=".",
        SUBSTRATE_DBT_SCHEMA=EVAL_SCHEMA,
        SUBSTRATE_RAW_SCHEMA=EVAL_RAW_SCHEMA,
        SUBSTRATE_PG_USER=EVAL_ROLE,
        SUBSTRATE_PG_PASSWORD=EVAL_PASSWORD,
    )
    completed = subprocess.run([dbt_bin, "build"], cwd=dbt_dir, env=env,
                               capture_output=True, text=True, timeout=900)
    output = _ANSI.sub("", completed.stdout + completed.stderr)
    errored = failed = 0
    if (m := _TOTALS.search(output)):
        errored = int(m.group("error"))
    # dbt reports test failures as FAIL lines; count them explicitly because a
    # failing test and an errored model are different signals.
    failed = len(re.findall(r"^\d+ of \d+ FAIL", output, re.M))
    return completed.returncode, output, errored, failed


def drop_eval_schemas() -> None:
    """Clear the eval schemas so each fault starts from an empty slate.

    Not tidiness — correctness. Models built by the previous fault are real
    views over `raw`, and PostgreSQL refuses `ALTER COLUMN ... TYPE` while a
    view depends on the column ("cannot alter type of a column used by a view").
    So without this, `type_change` cannot even be injected, and the fault it
    could not inject would be scored against whatever the previous fault left
    behind.
    """
    sql([f"DROP SCHEMA IF EXISTS {EVAL_SCHEMA} CASCADE;",
         f"DROP SCHEMA IF EXISTS {EVAL_SCHEMA}_staging CASCADE;",
         f"DROP SCHEMA IF EXISTS {EVAL_SCHEMA}_marts CASCADE;"])


def run_fault(fault: Fault, *, dbt_dir: Path, dbt_bin: str,
              pack: ProofPack) -> FaultResult:
    started = time.monotonic()
    injected = False
    drop_eval_schemas()
    try:
        sql(fault.inject_sql)
        injected = True
        exit_code, output, errored, failed = dbt_build(dbt_dir, dbt_bin)
    finally:
        if injected:
            # Always, even on an exception. A half-reverted substrate would
            # poison every subsequent fault and silently corrupt the score.
            # The eval schemas are dropped FIRST for the same reason injection
            # needed them gone: a view can block the revert too.
            try:
                drop_eval_schemas()
                sql(fault.revert_sql)
            except Exception as exc:  # pragma: no cover - surfaced, never swallowed
                print(f"  !! REVERT FAILED for {fault.name}: {exc}", file=sys.stderr)
                raise

    result = classify(exit_code=exit_code, output=output,
                      errored_models=errored, failed_tests=failed)
    raw_ref = pack.write(
        f"{fault.name}/dbt-build.txt",
        f"$ dbt build   (fault: {fault.name}, schema {EVAL_SCHEMA}, role {EVAL_ROLE})\n"
        f"injected: {'; '.join(fault.inject_sql)}\n"
        f"exit code: {exit_code}  errored_models={errored}  failed_tests={failed}\n\n"
        f"{output[-12000:]}",
        note=f"Real dbt build with {fault.name} injected into the live database.")

    return FaultResult(
        fault=fault.name, category=fault.category, expected=fault.expected,
        actual=result.label, correct=result.label is fault.expected,
        failure_observed=result.failure_observed, exit_code=exit_code,
        errored_models=errored, failed_tests=failed,
        duration_s=round(time.monotonic() - started, 3),
        evidence_excerpt=result.evidence_excerpt[:300], detail=result.detail,
        raw_ref=raw_ref,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="examples/eval")
    parser.add_argument("--pack-root", default="evidence/proof-pack/eval")
    parser.add_argument("--dbt-dir", default="substrate/dbt")
    parser.add_argument("--dbt-bin", default=os.environ.get("DBT_BIN", "dbt"))
    parser.add_argument("--only", default=None, help="run one fault by name")
    args = parser.parse_args()

    if not shutil.which(args.dbt_bin):
        print(f"dbt not found at {args.dbt_bin!r}; set DBT_BIN", file=sys.stderr)
        return 2

    repo = Path(__file__).resolve().parent.parent
    dbt_dir = repo / args.dbt_dir
    pack = ProofPack(args.pack_root, "eval")
    faults = [f for f in FAULTS if args.only in (None, f.name)]
    if not faults:
        print(f"no fault named {args.only!r}", file=sys.stderr)
        return 2

    print(f"the fault-injection evaluation eval — {len(faults)} scripted faults, real injection, "
          f"isolated schema {EVAL_SCHEMA}")

    print(f"  cloning raw -> {EVAL_RAW_SCHEMA} (the reference-incident substrate is never touched)")
    clone_raw_schema()
    setup_role()
    results: list[FaultResult] = []
    try:
        # A green baseline BEFORE any injection. Without this the whole run is
        # uninterpretable: a pre-existing failure would be attributed to
        # whichever fault happened to be injected at the time.
        print("  baseline (no fault)      ", end="", flush=True)
        base_code, base_output, base_err, base_fail = dbt_build(dbt_dir, args.dbt_bin)
        base_ref = pack.write(
            "baseline/dbt-build.txt",
            f"$ dbt build   (NO fault injected)\nexit code: {base_code}\n\n"
            f"{base_output[-8000:]}",
            note="Green baseline. Everything after this is attributable to a fault.")
        baseline_green = base_code == 0 and base_err == 0 and base_fail == 0
        print(f"{'GREEN' if baseline_green else 'RED'} "
              f"(exit {base_code}, errors {base_err}, failed tests {base_fail})")
        if not baseline_green:
            print("\nBaseline is not green. Refusing to score faults against a "
                  "substrate that is already broken — the numbers would be "
                  "meaningless.\n"
                  f"See {base_ref}", file=sys.stderr)
            return 3

        for fault in faults:
            print(f"  {fault.name:24} ", end="", flush=True)
            result = run_fault(fault, dbt_dir=dbt_dir, dbt_bin=args.dbt_bin, pack=pack)
            results.append(result)
            flag = "OK " if result.correct else "MISS"
            print(f"{flag} expected={result.expected.value:26} "
                  f"actual={result.actual.value:26} ({result.duration_s:5.1f}s)")
    finally:
        teardown_role()
        drop_raw_clone()
        print(f"  dropped {EVAL_RAW_SCHEMA}")

    summary = EvalSummary.of(results)
    payload = {
        "schema": "devguard.eval/1",
        "generated_by": "scripts/run_eval.py",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "measures": ("DevGuard's deterministic detection-and-classification path. "
                     "The Diagnostician could not run in this environment, so this "
                     "is NOT a measurement of LLM diagnosis."),
        "accuracy": summary.accuracy,
        "correct": summary.correct,
        "total": summary.total,
        "false_positives": summary.false_positives,
        "false_positive_rate": summary.false_positive_rate,
        "control_total": summary.control_total,
        "false_negatives": summary.false_negatives,
        "detected_total": summary.detected_total,
        "results": [
            {k: (v.value if isinstance(v, RootCauseLabel) else v)
             for k, v in r.__dict__.items()} | {
                "false_positive": r.false_positive,
                "false_negative": r.false_negative,
            }
            for r in results],
    }
    out = repo / args.out
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(payload, indent=2) + "\n")
    pack.write_index({"accuracy": summary.accuracy,
                      "false_positive_rate": summary.false_positive_rate})

    print()
    print(f"accuracy            : {summary.correct}/{summary.total} "
          f"= {summary.accuracy:.1%}")
    print(f"false positives     : {summary.false_positives}/{summary.control_total} "
          f"= {summary.false_positive_rate:.1%}")
    print(f"false negatives     : {summary.false_negatives}")
    print(f"faults with a signal: {summary.detected_total}/{summary.total}")
    print(f"\nresults: {out / 'results.json'}")
    return 0 if summary.false_positives == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
