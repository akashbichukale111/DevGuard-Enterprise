"""
proofpack.py — the generated-numbers rule's capture layer. Redacted at capture time, size-capped.

Rule: "Proof pack — evidence/proof-pack/<run-id>/: raw MCP request/response JSON
with timestamps … **Redacted at capture time**, size-capped with truncation
markers."

"At capture time" is the whole design constraint. A redaction pass that runs
before publishing is a pass you can forget to run; one that runs inside the
writer cannot be forgotten, because there is no other way to get bytes into the
pack. Every agent writes through `ProofPack.write`, and `ProofPack.write` is the
only thing in this package that opens a file for writing under evidence/.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

#: Hard cap per artifact. Big enough for a full lineage response, small enough
#: that one pathological payload cannot make the pack unreviewable.
MAX_ARTIFACT_BYTES = 256 * 1024
TRUNCATION_MARKER = "\n... [TRUNCATED BY PROOF PACK CAPTURE — see MAX_ARTIFACT_BYTES]"

#: Ordered so the more specific patterns win. Each entry is (regex, replacement).
#:
#: These are deliberately blunt. A redactor that tries to be clever about
#: context is a redactor that fails open on the one payload shape nobody
#: anticipated, and failing open here means a token in a committed file.
_REDACTIONS: tuple[tuple[re.Pattern, str], ...] = (
    # Bearer tokens, wherever they appear — headers, URLs, prose.
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer [REDACTED]"),
    # Authorization headers in any JSON-ish or header-ish rendering.
    (re.compile(r'(?i)("?authorization"?\s*[:=]\s*"?)[^",\s}]{8,}'), r"\1[REDACTED]"),
    # JWTs anywhere.
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*"),
     "[REDACTED-JWT]"),
    # Provider key shapes we actually use. Groq keys start gsk_.
    (re.compile(r"\bgsk_[A-Za-z0-9]{20,}"), "[REDACTED-API-KEY]"),
    # Any *_TOKEN / *_KEY / *_SECRET / *_PASSWORD assignment.
    #
    # The negative lookahead matters: without it this rule fires *after* the
    # JWT/API-key rules above and overwrites their specific markers, so
    # `token=<a jwt>` ends up as `token=[REDACTED]` and a reviewer loses the
    # information that the secret was a JWT. Still safe either way — the secret
    # is gone in both cases — but the specific marker is the useful one.
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY))\s*[:=]\s*"
                r"(?!\[REDACTED)\S+"),
     r"\1=[REDACTED]"),
    # The secret-hygiene rule explicitly asks for emails to become owner@example.com.
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
     "owner@example.com"),
)

#: The secret-hygiene rule also asks for internal hostnames. Ours are localhost and the compose
#: service names; those are not secrets and redacting them would make the
#: evidence unreproducible, so they are deliberately left alone. Recorded here
#: so the omission reads as a decision rather than an oversight.


def redact(text: str) -> str:
    """Apply every redaction. Idempotent — running it twice is a no-op."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _cap(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_ARTIFACT_BYTES:
        return text
    return encoded[:MAX_ARTIFACT_BYTES].decode("utf-8", errors="ignore") + TRUNCATION_MARKER


class ProofPack:
    """A single run's evidence directory.

    The run id is caller-supplied rather than generated so a re-run against the
    same incident lands in the same directory — the generated-numbers rule wants the pack to be
    idempotent alongside the write-back.
    """

    def __init__(self, root: str | os.PathLike, run_id: str) -> None:
        self.run_id = run_id
        self.root = Path(root) / run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self._index: list[dict] = []

    def write(self, name: str, content: Any, *, note: str = "") -> str:
        """Write one artifact, redacted and capped. Returns its raw_ref.

        The returned path is repo-relative so it can be pasted straight into an
        `Evidence.raw_ref` and still resolve from the repository root.
        """
        if not isinstance(content, str):
            content = json.dumps(content, indent=2, default=str)
        body = _cap(redact(content))

        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body + ("\n" if not body.endswith("\n") else ""))

        raw_ref = str(target)
        self._index.append({
            "name": name,
            "raw_ref": raw_ref,
            "bytes": len(body.encode()),
            "truncated": body.endswith(TRUNCATION_MARKER),
            "note": note,
            "written_at": datetime.now(timezone.utc).isoformat(),
        })
        return raw_ref

    def write_index(self, extra: Optional[dict] = None) -> str:
        """Write the manifest last. Everything else is discoverable from it."""
        payload = {
            "run_id": self.run_id,
            "written_at": datetime.now(timezone.utc).isoformat(),
            "artifact_count": len(self._index),
            "artifacts": self._index,
        }
        if extra:
            payload.update(extra)
        return self.write("INDEX.json", payload)

    @property
    def artifacts(self) -> tuple[dict, ...]:
        return tuple(self._index)
