#!/usr/bin/env python
"""
scan_secrets.py — the secret-hygiene rule's secret scan. Runs inside `make verify`.

The secret-hygiene rule: *"`.env.example` only; redaction in the capture layer …; **secret
scanning in `make verify`**; never commit a token."*

Scans every git-tracked file for credential shapes. Exits non-zero on a hit, so
a committed token fails the build rather than being noticed later by someone
reading a proof pack.

WHAT IT DOES NOT DO, said plainly: this is a shape-matcher, not entropy
analysis, and it will not catch a credential that looks like ordinary text. It
raises the cost of leaking one; it does not make leaking one impossible. The
real protection is that nothing in this repository ever writes a token to disk
outside the scratchpad, and `backend/v2/proofpack.py` redacts at capture time so
a token cannot reach an evidence file in the first place.

    python scripts/scan_secrets.py [--all]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

#: (name, pattern). Deliberately narrow — a scanner with false positives gets
#: `# noqa`'d into uselessness within a week.
PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("groq api key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}")),
    ("openai api key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("aws access key id", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private key block", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]+")),
    ("bearer token", re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._\-]{20,}")),
    ("datahub token assignment",
     re.compile(r"(?i)\bDATAHUB_GMS_TOKEN\s*[:=]\s*['\"]?[A-Za-z0-9._\-]{20,}")),
)

#: Files that legitimately contain credential-shaped strings, each with the
#: reason. An allowlist entry is a decision, so it carries an explanation.
ALLOWLIST: dict[str, str] = {
    "tests/test_proof_pack_redaction.py":
        "synthetic fixtures asserting the redactor removes these exact shapes",
    "scripts/scan_secrets.py":
        "this file — the patterns themselves",
}

#: Local throwaway credentials for a disposable container, committed on purpose
#: and already documented in substrate/dbt/profiles.yml. Matched literally so a
#: real secret in the same file is still caught.
LOCAL_DEV_LITERALS = frozenset({"devguard", "devguard_eval", "datahub"})


def tracked_files(scan_all: bool) -> list[Path]:
    args = ["git", "ls-files"] if scan_all else ["git", "ls-files"]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return [Path(p) for p in out.splitlines() if p]


def scan(paths: list[Path]) -> list[tuple[Path, int, str, str]]:
    hits: list[tuple[Path, int, str, str]] = []
    for path in paths:
        if str(path) in ALLOWLIST or not path.is_file():
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            for name, pattern in PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                if any(lit in match.group(0) for lit in LOCAL_DEV_LITERALS):
                    continue
                hits.append((path, line_no, name, match.group(0)[:40]))
    return hits


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true",
                        help="scan every tracked file (default)")
    args = parser.parse_args()

    paths = tracked_files(args.all)
    hits = scan(paths)

    print(f"secret scan: {len(paths)} tracked files, "
          f"{len(PATTERNS)} patterns, {len(ALLOWLIST)} allowlisted")
    if not hits:
        print("secret scan: clean")
        return 0

    print(f"\nsecret scan: {len(hits)} POSSIBLE CREDENTIAL(S) IN TRACKED FILES\n",
          file=sys.stderr)
    for path, line_no, name, excerpt in hits:
        # The excerpt is truncated; printing a whole key into CI logs would be
        # its own leak.
        print(f"  {path}:{line_no}  {name}  {excerpt[:12]}…", file=sys.stderr)
    print("\nIf one of these is a false positive, add it to ALLOWLIST with a "
          "reason. Do not widen a pattern.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
