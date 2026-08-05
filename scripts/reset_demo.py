#!/usr/bin/env python
"""
reset_demo.py — the write-back rules\' "honest reset, not fake revert".

Rule: *"DataHub incidents are resolved, not deleted; edits are overwritten or
soft-deleted. Ship `make reset-demo` (tear down + re-seed the environment) and
document precisely what can and cannot be un-written. Do **not** promise
`revert-writeback`."*

So this resets **the substrate**, which is genuinely resettable, and is explicit
about the catalog, which is not.

WHAT THIS RESETS (completely):
  * `substrate/dbt/models/staging/stg_users.sql` — restored from git, so the
    model once again references `user_id` by name.
  * `raw.users` — the column is put back to `customer_id`, i.e. the BROKEN
    state, because that is the state the recorded evidence was collected against
    and the state the reference-incident starts from.
  * `analytics_devguard_check*` — any leftover Referee validation schemas.

WHAT THIS CANNOT UN-WRITE, and says so rather than pretending:
  * **Incidents.** DataHub incidents are resolved, never deleted. A reset run
    raises a NEW incident; the old ones stay in the entity's history, which is
    correct — they really happened.
  * **Context Documents.** `save_document` has no delete in the negotiated tool
    set. Re-running overwrites by title where the server supports it and
    otherwise adds another document.
  * **Column descriptions and tags.** Overwritten on the next run, not removed.
  * **Structured properties.** `devguard.verified_at` keeps the most recent
    value. It is only ever written after a verified recovery, so a stale value
    still refers to a real one.

Usage:
    python scripts/reset_demo.py [--check]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

MODEL = "substrate/dbt/models/staging/stg_users.sql"
BROKEN_COLUMN = "customer_id"
ORIGINAL_COLUMN = "user_id"


def dsn() -> dict:
    env = os.environ
    return {
        "host": env.get("SUBSTRATE_PG_HOST", "localhost"),
        "port": int(env.get("SUBSTRATE_PG_PORT", "5433")),
        "user": env.get("SUBSTRATE_PG_USER", "devguard"),
        "password": env.get("SUBSTRATE_PG_PASSWORD", "devguard"),
        "dbname": env.get("SUBSTRATE_PG_DB", "devguard"),
    }


def current_columns(schema: str, table: str) -> tuple[str, ...]:
    import psycopg2
    with psycopg2.connect(**dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
            (schema, table))
        return tuple(r[0] for r in cur.fetchall())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="report state and change nothing")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parent.parent
    columns = current_columns("raw", "users")
    model_dirty = subprocess.run(
        ["git", "diff", "--quiet", "--", MODEL], cwd=repo).returncode != 0

    print(f"raw.users columns : {columns}")
    print(f"{MODEL}: {'MODIFIED' if model_dirty else 'clean'}")

    broken = BROKEN_COLUMN in columns and ORIGINAL_COLUMN not in columns
    print(f"substrate is in the BROKEN (reference-incident) state: {broken and not model_dirty}")

    if args.check:
        return 0

    if model_dirty:
        subprocess.run(["git", "checkout", "--", MODEL], cwd=repo, check=True)
        print(f"restored {MODEL} from git")

    if ORIGINAL_COLUMN in columns and BROKEN_COLUMN not in columns:
        import psycopg2
        with psycopg2.connect(**dsn()) as conn, conn.cursor() as cur:
            cur.execute(f'ALTER TABLE raw.users RENAME COLUMN {ORIGINAL_COLUMN} '
                        f'TO {BROKEN_COLUMN}')
        print(f"renamed raw.users.{ORIGINAL_COLUMN} -> {BROKEN_COLUMN} "
              f"(back to the broken state)")

    # Referee validation schemas, if a run was interrupted before cleanup.
    import psycopg2
    with psycopg2.connect(**dsn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name LIKE 'analytics_devguard_check%'")
        for (schema,) in cur.fetchall():
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            print(f"dropped leftover validation schema {schema}")

    print()
    print("Substrate reset. NOT reset (and cannot be — see this file's docstring):")
    print("  incidents (resolved, never deleted) · context documents ·")
    print("  column descriptions and tags (overwritten) · structured properties")
    return 0


if __name__ == "__main__":
    sys.exit(main())
