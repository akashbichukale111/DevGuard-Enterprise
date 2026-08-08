#!/usr/bin/env python
"""
build_replay.py — compile proof packs into replay bundles for the UI (the zero-infrastructure replay requirement).

    python scripts/build_replay.py                  # every loop pack
    python scripts/build_replay.py d6-loop-pass2    # one pack
    python scripts/build_replay.py --list           # what is available

Writes `frontend/public/replay/<run-id>.json` plus a `manifest.json` the UI
reads to populate its run picker. Needs no DataHub, no key and no database —
it only reads files that are already committed.

`--datahub-url` is optional and off by default. It is the base URL the pack was
captured against; supplying it turns URNs into clickable links. Without it the
bundle carries the URNs alone, because a link to a host that is not running is
worse than no link (the Command Center).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.v2.replay import ReplayBuildError, build_bundle  # noqa: E402

PACK_ROOT = REPO_ROOT / "evidence" / "proof-pack"
OUT_DIR = REPO_ROOT / "frontend" / "public" / "replay"

#: Packs that represent a whole incident run. `ablation` and `eval` are
#: measurement sweeps of many runs and `security` holds the security demos; those have
#: their own renderings and would only half-populate an incident view.
_EXCLUDE_TREES = {"ablation", "eval", "security"}

#: Ordered so the picker opens on the strongest run rather than alphabetically.
#: `d6-live-v170` leads because it is the only pack captured end to end against
#: a live DataHub v1.7.0 with metadata-service authentication enforced, and the
#: only one in which all five write-back artifacts landed and the *next* run
#: retrieved the runbook the previous one wrote.
_PREFERRED_ORDER = (
    "d6-live-v170",
    "d6-loop-pass2",
    "d6-loop-pass1",
    "d6-dry-run",
    "d6-fail-the-fix",
    "d5-refusal",
    "d5-full",
    "d4-evidence-chain",
)


def discover() -> list[Path]:
    if not PACK_ROOT.is_dir():
        return []
    packs = [
        p for p in PACK_ROOT.iterdir()
        if p.is_dir() and p.name not in _EXCLUDE_TREES and (p / "INDEX.json").is_file()
    ]
    rank = {name: i for i, name in enumerate(_PREFERRED_ORDER)}
    return sorted(packs, key=lambda p: (rank.get(p.name, len(rank)), p.name))


def _headline(bundle: dict) -> str:
    """One line for the picker. Says what the run demonstrates, from its data."""
    if bundle["chain"].get("is_sufficient") is False:
        missing = ", ".join(bundle["chain"].get("missing_for_sufficiency") or []) or "evidence"
        return f"Refusal — insufficient evidence ({missing})"
    if bundle["banners"]["dry_run"]:
        return "Full loop, dry run — every payload captured, nothing written"
    wb = bundle.get("write_back")
    if wb and wb.get("performed"):
        landed = sum(1 for a in wb["artifacts"] if a.get("outcome") in ("written", "already_present"))
        return f"Full loop — {landed} write-back artifact(s) landed in DataHub"
    state = bundle["incident"].get("state") or "partial"
    return f"Partial run — reached {state}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_id", nargs="*", help="pack name(s); default: all loop packs")
    ap.add_argument("--list", action="store_true", help="list available packs and exit")
    ap.add_argument("--datahub-url", default=None,
                    help="base URL the pack was captured against, e.g. http://localhost:9002")
    ap.add_argument("--out", default=str(OUT_DIR), help="output directory")
    args = ap.parse_args()

    available = discover()
    if args.list:
        if not available:
            print(f"no proof packs under {PACK_ROOT.relative_to(REPO_ROOT)}")
            return 1
        for p in available:
            print(p.name)
        return 0

    if args.run_id:
        by_name = {p.name: p for p in available}
        # Allow explicitly naming a pack outside the loop set (e.g. a nested
        # security demo) rather than refusing work the caller clearly wants.
        packs = []
        for name in args.run_id:
            if name in by_name:
                packs.append(by_name[name])
            elif (PACK_ROOT / name / "INDEX.json").is_file():
                packs.append(PACK_ROOT / name)
            else:
                print(f"error: no proof pack named {name!r} "
                      f"(try --list)", file=sys.stderr)
                return 1
    else:
        packs = available

    if not packs:
        print(f"error: no proof packs found under {PACK_ROOT}", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = []
    failures = 0
    for pack in packs:
        try:
            bundle = build_bundle(pack, datahub_base_url=args.datahub_url,
                                  repo_root=REPO_ROOT)
        except ReplayBuildError as exc:
            print(f"[FAIL] {pack.name}: {exc}", file=sys.stderr)
            failures += 1
            continue
        target = out_dir / f"{pack.name}.json"
        target.write_text(json.dumps(bundle, indent=1, sort_keys=False), encoding="utf-8")
        size_kb = target.stat().st_size / 1024
        manifest.append({
            "run_id": bundle["run_id"],
            "headline": _headline(bundle),
            "incident_id": bundle["incident"]["incident_id"],
            "state": bundle["incident"]["state"],
            "captured_at": bundle["pack_written_at"],
            "evidence_count": len(bundle["evidence"]),
            "artifact_count": bundle["artifact_count"],
            "refused": bundle["chain"].get("is_sufficient") is False,
            "dry_run": bundle["banners"]["dry_run"],
            "bytes": target.stat().st_size,
        })
        print(f"[ok]   {pack.name:<20} {len(bundle['evidence']):>3} evidence  "
              f"{bundle['artifact_count'] or 0:>3} artifacts  {size_kb:>7.1f} KiB")

    (out_dir / "manifest.json").write_text(
        json.dumps({"runs": manifest, "default": manifest[0]["run_id"] if manifest else None},
                   indent=1),
        encoding="utf-8")
    print(f"\nwrote {len(manifest)} bundle(s) + manifest.json to "
          f"{out_dir.relative_to(REPO_ROOT) if out_dir.is_relative_to(REPO_ROOT) else out_dir}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
