"""
Detect and validate DataHub configuration before a run depends on it.

The gap this closes
-------------------
Three separate problems, all found by auditing what the code actually reads
against what an operator is likely to set:

1. **`DATAHUB_TOKEN` was read by nothing.** The client reads `DATAHUB_GMS_TOKEN`
   and `DATAHUB_TOKEN_FILE`. `DATAHUB_TOKEN` is the shorter, more obvious name —
   it is what DataHub's own CLI environment uses and what most people will
   export first. Setting it produced an *unauthenticated* client with no
   warning, and the failure surfaced much later as a permissions error against
   a server that was configured correctly.

2. **`make doctor` had no DataHub check at all.** The preflight tool reported on
   Python, Node, the model key and the collector, and said nothing about the
   single dependency the flagship module is built around. "Is my catalog
   reachable, and am I who I think I am?" had no answer until an agent failed
   mid-run.

3. **Reachable and authorised are different questions**, and a run that
   conflates them wastes the operator's time on the wrong one. A GMS that
   answers `/config` but rejects the token is a credential problem; one that
   does not answer at all is a network problem. They are reported separately.

Fails honestly
--------------
Nothing here raises, and nothing reports success it did not observe. An absent
variable is `NOT_CONFIGURED`, an unreachable host is `UNREACHABLE`, and a
rejected token is `UNAUTHENTICATED` — three distinct states, none of which is
allowed to read as "fine". The one thing this module must never do is let a
misconfigured deployment believe it is configured.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

#: Token sources, most explicit first. `DATAHUB_TOKEN` is listed first because
#: it is the name an operator reaches for; the others are kept because existing
#: deployments and `datahub` CLI conventions use them, and silently dropping a
#: name that used to work is its own outage.
TOKEN_ENV_VARS = ("DATAHUB_TOKEN", "DATAHUB_GMS_TOKEN")
TOKEN_FILE_ENV_VAR = "DATAHUB_TOKEN_FILE"

#: Where GMS answers. `/config` is unauthenticated on every DataHub deployment
#: and is the cheapest way to ask "is there a GMS here at all".
CONFIG_PATH = "/config"

#: The GraphQL entry point, verified in use by scripts/verify_least_privilege.py.
GRAPHQL_PATH = "/api/graphql"

DEFAULT_TIMEOUT_S = 10


class Status(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNREACHABLE = "UNREACHABLE"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    OK = "OK"


@dataclass(frozen=True)
class PreflightReport:
    status: Status
    detail: str
    gms_url: Optional[str] = None
    token_source: Optional[str] = None
    graphql_url: Optional[str] = None
    server_version: Optional[str] = None
    checks: tuple[tuple[str, bool, str], ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        """True only when a run can actually depend on this catalog."""
        return self.status is Status.OK

    def summary(self) -> str:
        return f"{self.status.value}: {self.detail}"


def resolve_token() -> tuple[Optional[str], Optional[str]]:
    """
    Return `(token, source_name)` from the first source that supplies one.

    The source is returned so an operator can be told *which* variable was used
    — two set to different values is a real and confusing situation, and the
    answer "whichever came first" is only useful if you say which.
    """
    for var in TOKEN_ENV_VARS:
        value = (os.environ.get(var) or "").strip()
        if value:
            return value, var

    path = (os.environ.get(TOKEN_FILE_ENV_VAR) or "").strip()
    if path:
        try:
            value = Path(path).read_text().strip()
        except OSError:
            return None, f"{TOKEN_FILE_ENV_VAR} (unreadable: {path})"
        if value:
            return value, f"{TOKEN_FILE_ENV_VAR} ({path})"
    return None, None


def resolve_gms_url() -> Optional[str]:
    url = (os.environ.get("DATAHUB_GMS_URL") or "").strip()
    return url.rstrip("/") or None


def resolve_graphql_url() -> Optional[str]:
    """
    Explicit override, else derived from GMS.

    `DATAHUB_GRAPHQL_URL` is accepted because a deployment can put GraphQL
    behind a different host or path than GMS, and hard-coding the derivation
    would make that deployment unconfigurable.
    """
    explicit = (os.environ.get("DATAHUB_GRAPHQL_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    gms = resolve_gms_url()
    return f"{gms}{GRAPHQL_PATH}" if gms else None


def _get(url: str, token: Optional[str], timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(1024).decode("utf-8", "replace")


def _post_graphql(url: str, token: Optional[str], query: str,
                  timeout: float) -> tuple[int, str]:
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(1024).decode("utf-8", "replace")


def preflight(timeout_s: float = DEFAULT_TIMEOUT_S) -> PreflightReport:
    """
    Detect DataHub configuration and validate it, without ever raising.

    Order matters: configuration, then reachability, then authentication. Each
    answers a question the next one assumes, and reporting them together would
    tell an operator to check the wrong thing.
    """
    checks: list[tuple[str, bool, str]] = []

    gms = resolve_gms_url()
    token, token_source = resolve_token()
    graphql = resolve_graphql_url()

    checks.append(("DATAHUB_GMS_URL", bool(gms), gms or "not set"))
    checks.append(("token", bool(token),
                   f"from {token_source}" if token else
                   f"none of {', '.join(TOKEN_ENV_VARS)}, {TOKEN_FILE_ENV_VAR}"))

    if not gms:
        return PreflightReport(
            status=Status.NOT_CONFIGURED,
            detail=("DATAHUB_GMS_URL is not set, so there is no catalog to reach. "
                    "The Command Center replays committed proof packs without one; "
                    "a live run needs it."),
            token_source=token_source, checks=tuple(checks))

    # --- reachability: is there a GMS here at all? -------------------------
    try:
        code, body = _get(f"{gms}{CONFIG_PATH}", None, timeout_s)
    except Exception as exc:  # noqa: BLE001 — a preflight must not raise
        checks.append((CONFIG_PATH, False, type(exc).__name__))
        return PreflightReport(
            status=Status.UNREACHABLE,
            detail=f"{gms}{CONFIG_PATH} could not be reached: {type(exc).__name__}",
            gms_url=gms, token_source=token_source, graphql_url=graphql,
            checks=tuple(checks))

    reachable = code == 200
    checks.append((CONFIG_PATH, reachable, f"HTTP {code}"))
    if not reachable:
        return PreflightReport(
            status=Status.UNREACHABLE,
            detail=f"{gms}{CONFIG_PATH} answered HTTP {code}; this does not look like a GMS",
            gms_url=gms, token_source=token_source, graphql_url=graphql,
            checks=tuple(checks))

    version = None
    try:
        version = (json.loads(body).get("versions") or {}).get(
            "acryldata/datahub", {}).get("version")
    except Exception:  # noqa: BLE001 — version is a nicety, not a gate
        pass

    # --- authentication: does the token work? ------------------------------
    # A trivial, side-effect-free query. It must be one every deployment
    # answers, so it asserts nothing about entity types this substrate may not
    # have.
    status_code, gql_body = _post_graphql(
        graphql or f"{gms}{GRAPHQL_PATH}", token, "query { __typename }", timeout_s)
    authed = status_code == 200 and '"errors"' not in gql_body
    checks.append((GRAPHQL_PATH, authed, f"HTTP {status_code}"))

    if not authed:
        return PreflightReport(
            status=Status.UNAUTHENTICATED,
            detail=(f"GMS is reachable but GraphQL returned HTTP {status_code}"
                    + (f" using the token from {token_source}" if token
                       else " and no token was supplied")
                    + ". Reachable and authorised are different problems; this is the second."),
            gms_url=gms, token_source=token_source, graphql_url=graphql,
            server_version=version, checks=tuple(checks))

    return PreflightReport(
        status=Status.OK,
        detail=f"GMS reachable and GraphQL authenticated"
               + (f" (DataHub {version})" if version else ""),
        gms_url=gms, token_source=token_source, graphql_url=graphql,
        server_version=version, checks=tuple(checks))
