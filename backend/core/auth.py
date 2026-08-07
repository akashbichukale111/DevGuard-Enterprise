"""
Optional API-key authentication for the endpoints that spend money.

Why optional, and why that is not a cop-out
-------------------------------------------
The rate limiter in `ratelimit.py` is a *cost* guard, not a security control:
it keys on `X-Forwarded-For`, which any direct caller can forge. Something has
to actually stand between the open internet and a paid inference endpoint, and
that something is authentication.

It is off by default because the project's central claim is that a clean clone
runs with no configuration — `make test`, `make demo` and a local backend all
work with no key, no catalog and no collector. Shipping auth *on* would mean
every reviewer's first `curl` returns 401, and the honest way to protect a
public deployment is to make the protection available and documented, not to
tax the person evaluating the code.

So: set `DEVGUARD_API_KEYS` and the scan endpoints require a key. Leave it
unset and behaviour is exactly what it was. The mode is reported by
`/slo-status` so an operator can see which one they are in rather than
assuming — a deployment that *believes* it is authenticated but is not is
worse than one that knows it is open.

What this is not
----------------
This is a shared-secret check, not an identity system. There are no users, no
scopes, no rotation and no expiry. It is the right size for "stop strangers
spending my Groq budget" and the wrong size for multi-tenant access control,
which would need a real IdP. Saying so here is cheaper than someone assuming
otherwise in production.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets

from fastapi import HTTPException, Request

logger = logging.getLogger("devguard.auth")

#: Comma-separated shared secrets. Empty or unset disables authentication.
_ENV_VAR = "DEVGUARD_API_KEYS"

#: Keys shorter than this are rejected at load time. A three-character key is
#: worse than no key: it fails to keys, but reads as protection on a dashboard.
MIN_KEY_LENGTH = 16


def _load_keys() -> frozenset[str]:
    raw = os.environ.get(_ENV_VAR, "")
    keys = {k.strip() for k in raw.split(",") if k.strip()}

    too_short = {k for k in keys if len(k) < MIN_KEY_LENGTH}
    if too_short:
        # Never log the key itself, not even a rejected one — rejected here
        # often means "typo of a real one somewhere else".
        logger.warning(
            "%s: ignoring %d key(s) shorter than %d characters",
            _ENV_VAR, len(too_short), MIN_KEY_LENGTH,
        )
    return frozenset(keys - too_short)


#: Resolved once at import, like the rate limiter's settings. Tests reload it
#: through `reload_keys()` rather than reaching into the module.
_KEYS: frozenset[str] = _load_keys()


def reload_keys() -> None:
    """Re-read the environment. For tests and for a process that re-execs."""
    global _KEYS
    _KEYS = _load_keys()


def auth_enabled() -> bool:
    return bool(_KEYS)


def key_fingerprints() -> list[str]:
    """
    Short, non-reversible identifiers for the configured keys.

    An operator needs to answer "is the key I just rotated the one that is
    live?" without the answer being the key. Eight hex characters of SHA-256
    is enough to compare against a fingerprint they compute themselves and far
    too little to invert.
    """
    return sorted(hashlib.sha256(k.encode()).hexdigest()[:8] for k in _KEYS)


def _presented_key(request: Request) -> str | None:
    """Accept either `Authorization: Bearer <key>` or `X-API-Key: <key>`."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    direct = request.headers.get("x-api-key", "")
    return direct.strip() or None


async def enforce_api_key(request: Request) -> None:
    """
    FastAPI dependency. A no-op when `DEVGUARD_API_KEYS` is unset.

    Comparison is constant-time against every configured key. Short-circuiting
    on the first mismatch would leak, through timing, how many characters of a
    guess were right — which is the whole attack against a naive `==`.
    """
    if not _KEYS:
        return

    presented = _presented_key(request)
    if presented is not None:
        # `any()` short-circuits, but each individual comparison is still
        # constant-time, so what leaks is at most "which key matched" — and
        # the caller already knows the key they sent.
        if any(secrets.compare_digest(presented, k) for k in _KEYS):
            return

    logger.warning(
        "unauthenticated request: path=%s presented=%s",
        request.url.path, "yes" if presented else "no",
    )
    raise HTTPException(
        status_code=401,
        detail={
            "error": "Missing or invalid API key.",
            "hint": "Send 'Authorization: Bearer <key>' or 'X-API-Key: <key>'.",
        },
        # RFC 7235: a 401 must say how to authenticate.
        headers={"WWW-Authenticate": "Bearer"},
    )
