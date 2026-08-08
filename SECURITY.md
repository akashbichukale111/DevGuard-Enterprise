# Security

DevGuard Enterprise is a privileged actor. It reads untrusted text, calls an
external model provider, writes to a shared metadata catalog, and maintains an
audit trail. This document states what is actually enforced today. Where a
control is designed but not yet implemented, it says so rather than implying
coverage that does not exist.

- [Reporting a vulnerability](#reporting-a-vulnerability)
- [Threat model](#threat-model)
- [Untrusted-content boundary](#untrusted-content-boundary)
- [Mutation allowlist](#mutation-allowlist)
- [Least privilege](#least-privilege)
- [Autonomy policy](#autonomy-policy)
- [Auditability](#auditability)
- [Secret hygiene](#secret-hygiene)
- [Application-layer controls](#application-layer-controls)
- [What is regression-protected](#what-is-regression-protected)
- [Assertions — read, never authored](#assertions--read-never-authored)
- [Known gaps](#known-gaps)
- [Dependencies](#dependencies)

---

## Reporting a vulnerability

Please open a GitHub issue, or contact the maintainer directly for anything you
believe should not be public first. There is no response SLA on this project.

**Supported versions:** `main` only. There are no released versions and no
backported fixes.

---

## Threat model

Two distinct surfaces, with different adversaries.

**The catalog agent.** DevGuard reads a shared metadata catalog that many people
can write to. Anyone who can edit a dataset description, a column comment or a
document body can place text in front of an agent. The catalog's *structure* —
URNs, schemas, lineage edges, ownership — is trusted. The catalog's *free text*
is not.

**The code scanner.** Submitted source is untrusted input that reaches a model.
The adversary's goal is a false negative: talking the Scanner out of reporting a
finding.

Assumed trusted: the DataHub server's structural responses, the runtime
environment, and the operator approving changes. Explicitly **not** defended
against: a fully compromised catalog server.

---

## Untrusted-content boundary

Every agent prompt carries an explicit rule declaring fenced content to be data
rather than instruction, and `fence_untrusted()` wraps that content in sentinel
markers — not a Markdown code fence, which untrusted text can close early to
break out of.

`backend/v2/sentinel.py` screens catalog free text before it reaches any prompt.
Model-generated free text derived from untrusted input is fenced too, because it
inherits the taint of its source.

Evidence is typed at the boundary: anything an external party could have authored
carries `EvidenceTrust.UNTRUSTED_TEXT`, and there is no accessor that yields the
raw string in a prompt-ready form.

**This raises the cost of an injection; it does not make one impossible.** No
prompt-level defence does. The screen is a shape-matcher and novel phrasings will
pass it. What actually holds is architectural: the Diagnostician — the agent an
injection would most want to influence — has **zero tools**. Text cannot cause a
tool call there because there is no tool call to cause.

Pinned by `tests/test_prompt_injection_boundary.py` and
`tests/test_sentinel_fencing.py`. Demonstrated live by
`scripts/run_injection_demo.py`; proof pack in
`evidence/proof-pack/security/injection-demo/`.

---

## Mutation allowlist

Three axes, all enforced in `DataHubMCPClient.call` **before** the request is
written to the pipe.

| Axis | Value | Where |
|---|---|---|
| Tools | `add_tags`, `update_description`, `save_document`, `add_structured_properties`, `add_owners` — held by **Scribe only** | `AGENT_TOOL_ALLOWLISTS` |
| Entity types | `dataset`, `document` | `MUTABLE_ENTITY_TYPES` |
| Scope | five named dataset URNs by default; `DEVGUARD_MUTATION_SCOPE_URNS` overrides | `MUTATION_SCOPE_URNS` |

An agent that requests a tool it does not hold raises `ToolNotAllowedError`; a
write outside scope raises `EntityNotInScopeError`. Both are Python exceptions
with stack traces rather than server-side rejections that have to be interpreted.

Reads are deliberately unrestricted. The blast radius of reading a dataset
DevGuard does not own is nil, and narrowing reads would break lineage traversal.

The scope axis is configurable through `DEVGUARD_MUTATION_SCOPE_URNS` because
onboarding a sixth asset used to be a code change and a redeploy — and a
control that is painful to adjust correctly is one people adjust incorrectly,
usually by removing it. Entries are separated by **whitespace, not commas**: a
dataset URN contains commas of its own, so a comma-joined list is a single
malformed token. It fails closed in both directions — an entry that is not
exactly one dataset URN is dropped with an error log, and a wholly invalid
value permits no dataset write at all rather than reverting to the default.
Widening it grants nothing the server-side Access Policy denies.

This duplicates the server-side Access Policy on purpose — and that redundancy
proved its worth, as the next section explains.

**A fourth axis lives on the server: `TOOLS_IS_MUTATION_ENABLED`.**
`mcp-server-datahub` hides every mutation tool unless this is `true`.
`DataHubMCPClient` sets it from `enable_mutations`, which defaults to **false**,
so a client that never asks for writes cannot make one even if every allowlist
above were bypassed.

The trap worth stating: with the flag off, the write tools are not *refused*,
they are **absent**. Capability negotiation reports them missing, the Scribe
records artifacts it could not attempt, and the run completes. A write-back
that silently did nothing looks very like a successful dry run, and the
difference is one environment variable. Check `capability_report()` in the
proof pack before concluding a run wrote anything.

Pinned by `tests/test_agent_allowlists.py`, and by
`tests/test_mcp_contract.py`, which additionally checks every tool named
anywhere in this project against the published `mcp-server-datahub` tool
reference — a tool renamed upstream or mistyped locally fails CI offline
instead of failing against a live catalog.

---

## Least privilege

Service account **`urn:li:corpuser:devguard_agent`**, created by
`scripts/setup_service_account.py`, replacing `urn:li:corpuser:__datahub_system`
— which holds `manageIngestion` and `managePolicies` and should never have been
the account an agent used.

| Artifact | Privilege required |
|---|---|
| Read: search, lineage, schema, queries | `VIEW_ENTITY_PAGE` |
| 1 — incident raised and resolved | `EDIT_ENTITY_INCIDENTS` |
| 2 — post-mortem runbook | `MANAGE_DOCUMENTS` (platform) |
| 3 — column-level tag | `EDIT_DATASET_COL_TAGS` |
| 3 — column-level description | `EDIT_DATASET_COL_DESCRIPTION` |
| 4 — structured properties | `EDIT_ENTITY_PROPERTIES` |
| 5 — ownership | `EDIT_ENTITY_OWNERS` |

Scoped to **five named dataset URNs**, not a domain. A URN allowlist is strictly
narrower — a domain grants access to whatever is later added to it — and this
substrate has no domain that would exist for any reason other than the policy.

**Never granted.** Not all of them are equally *proven*, and the difference
matters — this section previously said "each verified as a live DENY" of all
seven privileges below, when the committed artifact probed four. That was an
overclaim in a security document, which is the worst place to have one, and the
table now distinguishes the three cases.

| Privilege | Status | Probe |
|---|---|---|
| `DELETE_ENTITY` | **Proven denied** against a live server | `batchUpdateSoftDeleted` |
| `EDIT_LINEAGE` | **Proven denied** | `updateLineage` |
| `MANAGE_POLICIES` | **Proven denied** | `createPolicy` |
| `MANAGE_INGESTION` | **Proven denied** | `createIngestionSource` |
| `EDIT_ENTITY_GLOSSARY_TERMS` | **Proven denied** — executed against DataHub v1.7.0 | `addTerms` |
| `EDIT_DOMAINS_PRIVILEGE` | **Proven denied** — executed against DataHub v1.7.0 | `setDomain` |
| `EDIT_ENTITY_STATUS` | **Asserted, not probeable** | DataHub's GraphQL exposes no dataset-status mutation — only `updateUserStatus`, which targets corp users. The grant is absent from the policy; there is no way to demonstrate the refusal through this surface. |

A further DENY case in the artifact is not a privilege at all: it writes to a
real, ingested dbt dataset that sits **outside the five-URN scope**, proving the
scope axis independently of the privilege axis.

Current state, against DataHub v1.7.0 with authentication enforced —
[`evidence/datahub-live/03-least-privilege-AUTH-ON.txt`](evidence/datahub-live/03-least-privilege-AUTH-ON.txt):

```
$ python scripts/verify_least_privilege.py
auth enforcement: ON — a forged token was rejected with HTTP 401

ALLOW: 5/5 behaved as required
DENY : 7/7 correctly refused
```

The fifth ALLOW case is new and is a cleanup: the verifier now **resolves the
incident it raises**. Every previous run left an ACTIVE incident on a production
dataset — a verifier that degraded the catalog a little each time it proved the
catalog was safe.

Independent corroboration from the server's own view of the account:

```
$ curl … -H "Authorization: Bearer <agent token>" \
    -d '{"query":"query { me { corpUser { urn } platformPrivileges { … } } }"}'
{"data":{"me":{"corpUser":{"urn":"urn:li:corpuser:devguard_agent"},
  "platformPrivileges":{"managePolicies":false,"manageIngestion":false,
                        "manageDomains":false}}}}
```

### Why that verifier exists, and why it now refuses to run

On its first ever run the account passed all four ALLOW cases **and all five DENY
cases also succeeded** — a failed verification in which nothing errored. The
account could delete datasets, rewrite lineage, write anywhere in the catalog,
and grant itself further privileges. The policies existed and looked correct in
the UI.

**Cause: the DataHub quickstart ships with
`METADATA_SERVICE_AUTH_ENABLED=false`, under which Access Policies are not
enforced at all.** This is still the default in **v1.7.0** — re-confirmed this
session by reading the quickstart's own compose file.

**And there is a second, sharper problem, found by running it again.** Every DENY
probe is a *real mutation*: a soft-delete, a lineage edit, a policy creation, a
domain reassignment. The whole design rests on the server refusing them. When
nothing is enforcing, they are not refused — **they land.**

The auth-off run kept at
[`evidence/datahub-live/02-least-privilege-AUTH-OFF.txt`](evidence/datahub-live/02-least-privilege-AUTH-OFF.txt)
reported `ALLOW 4/4, DENY 0/7`, which was accurate. By then it had also:

- soft-deleted `raw.users`, the hero dataset,
- added a cycle to its lineage (`raw.users ← user_order_features`),
- attached a nonexistent glossary term and reassigned its domain,
- created an ingestion source, and
- created a policy named `devguard-escalation-probe` granting the agent
  `MANAGE_POLICIES`.

All of it was repaired. But a security verifier that damages the system it is
verifying is a defect in the verifier, so it now **fails closed**: it asks the
server whether authentication is enforced by presenting a token that could not
possibly be valid — a server that accepts one is not checking — and refuses to
proceed otherwise, naming the fix rather than offering a flag that sounds
harmless. The override exists only to reproduce the demonstration above and is
spelled `--i-understand-the-deny-probes-will-mutate`.

`auth_enforced` and the evidence for it are written into the summary artifact,
because a DENY row reading "refused" means nothing if nothing was checking.

> **Enabling `METADATA_SERVICE_AUTH_ENABLED=true` is a hard prerequisite for
> anyone reproducing this.** Preserve `DATAHUB_TOKEN_SERVICE_SIGNING_KEY` when
> you do, so existing tokens stay valid. Exact commands:
> [DEPLOYMENT.md](DEPLOYMENT.md#the-datahub-catalog).

A control you have not tried to violate is a control you have not verified. Full
record in [`evidence/d9/`](evidence/d9/) (v1.6.0) and
[`evidence/datahub-live/`](evidence/datahub-live/) (v1.7.0).

### One privilege the agent deliberately does not have: creating vocabulary

`EDIT_ENTITY_GLOSSARY_TERMS` and `EDIT_DOMAINS_PRIVILEGE` are denied on purpose,
and the same principle extends to tags even though applying one *is* granted. The
Scribe applies `urn:li:tag:devguard_incident_impacted` and will **not create it**:
DataHub rejects `add_tags` against a tag URN that does not exist, and rather than
minting one, the write-back fails and says so.

That is a security property, not an inconvenience. An agent that can invent
vocabulary can invent meaning — it can label an asset with a term nobody defined
and no reviewer approved. The vocabulary is therefore declared by an operator in
[`scripts/provision_catalog.py`](scripts/provision_catalog.py), reviewable in a
diff, and applied once at provisioning time.

The first live run on v1.7.0 failed write-back artifact 3 for exactly this reason.
The correct behaviour followed: the incident stayed **ACTIVE** rather than being
marked resolved, because the write-back's partial-failure policy forbids
asserting a verified state whose supporting knowledge is missing.

---

## Autonomy policy

`AUTONOMY_POLICY` in `backend/v2/agents/magistrate.py` is the same object the
documentation renders and the code branches on, so the published policy and the
enforced policy cannot drift.

| Risk | Allowed action | Who approves |
|---|---|---|
| LOW | propose + validate; apply only after approval | asset owner from the graph |
| MEDIUM | propose + validate; apply only after approval | asset owner from the graph |
| HIGH | propose + validate; apply only after approval | asset owner (always named) |
| CRITICAL | nothing applied; recorded and escalated | **nobody — no approval path exists** |

**Nothing is autonomous in this build**, asserted by a module-level `assert`.
`ApprovalRequest.approve()` raises `PermissionError` for CRITICAL, so no identity
can authorise destructive DDL, a data mutation, a permission change, or a
hard-coded credential.

Owners are resolved from the catalog graph rather than from configuration — an
approval routed to a name in a config file is an approval routed to whoever last
edited that file.

Pinned by `tests/test_writeback_rules.py` and `tests/test_approval_gate.py`.

---

## Auditability

Every tool call, decision and write lands in `evidence/proof-pack/<run-id>/`
alongside the evidence chain that justified it and the approver's identity.
`AgentHandoff` records `from_agent`, `to_agent`, `evidence_ids`, `decision`,
measured duration, tokens and model. Every write-back artifact is stamped with
the evidence IDs and the chain digest.

Separately, the scanner maintains a **hash-chained, tamper-evident audit log**
(`backend/core/audit.py`). `GET /audit-log/verify` recomputes the chain and
reports the first break if there is one.

> The log is runtime state and is created on first write, so a clean clone
> starts with an empty chain. Where historical entries exist and record a
> superseded verdict, they are deliberately **not** rewritten — editing a
> hash-chained audit log to make it look better is precisely the act the chain
> exists to detect.

Pinned by `tests/test_audit_chain.py` and `tests/test_audit_verdict.py`.

---

## Secret hygiene

- **Redaction at capture time.** `backend/v2/proofpack.py` is the only writer
  into `evidence/`, and it redacts bearer tokens, JWTs, API-key shapes,
  `*_TOKEN` / `*_SECRET` / `*_PASSWORD` assignments, and rewrites emails to
  `owner@example.com`. A token cannot reach an evidence file even if something
  logs it. Pinned by `tests/test_proof_pack_redaction.py`.
- **Secret scanning in `make verify`.** `scripts/scan_secrets.py` runs over every
  tracked file (9 patterns, 2 documented allowlist entries).
- **Full-history scanning in CI.** A separate job scans every commit for
  `gsk_*`, `sk-*`, `AKIA*` and PEM private-key patterns, and fails if `.env` or
  `frontend/.env.local` is ever tracked.
- **Tokens are read at runtime** from `DATAHUB_TOKEN_FILE` (`chmod 600`, outside
  the repository). Never logged, never echoed, never committed.
- **The backend boots without a key**, so a reviewer never has to supply one to
  inspect the system.

Local throwaway credentials for the disposable substrate PostgreSQL (`devguard`,
`devguard_eval`) **are** committed, deliberately and visibly, in
`substrate/dbt/profiles.yml`. They reach a container holding generated rows.

---

## Application-layer controls

- **Human-in-the-loop approval gate.** Critical and high severity findings pause
  for an explicit `/approve` or `/reject`. The gate reads the Scanner's finding,
  not the request, so a caller cannot supply a low severity to bypass review —
  and an unrecognised severity gates rather than passing.
- **Bounded agent loop.** Reflection retries are capped; a circuit breaker
  (`backend/core/resilience.py`) opens on repeated upstream failure rather than
  retrying without limit.
- **Request size cap.** `ScanRequest.code` carries `max_length=50_000`, enforced
  by Pydantic before any model call — 50,001 characters returns **HTTP 422** at
  the schema boundary, so an oversized payload never reaches a paid API or trips
  the breaker. The cap is on the `code` field, not on total body bytes.
- **Fail-safe telemetry.** If the collector or the MCP path is unreachable, every
  operation proceeds unchanged.
- **Honest provenance.** Every response that could come from more than one source
  carries `data_source` — `live`, `local_shadow`, `synthetic` or `partial` — and
  the UI renders it. An in-process estimate is never presented as retrieved
  telemetry.

---

## What is regression-protected

**1101 tests run on every push in CI**, with no API key, no collector and no
network. They cover the typed agent boundaries, tool allowlists, the mutation
scope, the evidence chain rule, refusal reasons, the untrusted-content boundary,
audit-chain tamper detection, the circuit-breaker state machine, telemetry
fail-safety, proof-pack redaction, replay-bundle integrity, and the `/scan`
response contract — including that no published number is a hard-coded constant
and that a model cannot author DevGuard's own measurements (`tokens_used`,
`model_used`) through its JSON output.

CI additionally runs a real OTLP export verification against decoded protobuf,
the working-tree secret scan, and the full-history credential scan.

The properties claimed in this document are checked mechanically rather than
asserted once.

---

## Known gaps

Real, open, and stated rather than buried.

**Deployment posture**

- **CORS defaults to `*`, and is configurable.** `DEVGUARD_ALLOWED_ORIGINS` takes a
  comma-separated allowlist. The default stays open so a clean clone works with no
  configuration; set it on anything public. `render.yaml` sets it for the hosted
  backend.
- **Authentication is opt-in and off by default.** Set `DEVGUARD_API_KEYS`
  (comma-separated, each at least 16 characters) and `POST /scan`, `/scan/zip`,
  `/scan/repository` and both approval-gate endpoints require
  `Authorization: Bearer <key>` or `X-API-Key: <key>`. Comparison is
  constant-time; the check runs before the rate limiter so anonymous traffic
  cannot burn a real caller's allowance; `GET /slo-status` reports which mode is
  live plus SHA-256 fingerprints of the loaded keys. **Unset means open** — that
  is deliberate, so a reviewer's first `curl` works, and it means an internet-facing
  instance with no key set is unauthenticated.
  - This is a shared secret, not an identity system: no users, no scopes, no
    rotation, no expiry. Front it with a real IdP if you need those.
  - **The bundled frontend does not send a key.** It is a static export, so a key
    compiled into it would be public anyway. Turning auth on therefore protects
    the API from scripts but breaks the Scanner UI and the approve/reject buttons
    against that instance. That is the intended trade for a non-demo deployment:
    keep the hosted demo keyless, and put a key on any instance that matters —
    calling it from a server you control, or behind a proxy that injects the
    header.
  - **Reads remain unauthenticated even when it is on**, including `GET /audit-log`.
    The audit log records what DevGuard did, not credentials or submitted source,
    and a health check that 401s is a health check the platform marks unhealthy.
    If the audit trail is sensitive in your deployment, put a proxy in front.
- **Rate limiting on the endpoints that spend money.** `POST /scan`, `/scan/zip`
  and `/scan/repository` are capped per client per window (default 20/60 s,
  `DEVGUARD_SCAN_RATE_LIMIT` / `DEVGUARD_SCAN_RATE_WINDOW_S`, `0` disables).
  A refusal is `429` with `Retry-After`. It keys on `X-Forwarded-For`, which a
  direct caller can forge, so it is a **cost guard, not a security control** —
  authentication is the control. State is per process, so behind N replicas the
  effective ceiling is N×.
- **Submitted code is sent to a third party.** Anything pasted into the Scanner
  is transmitted to the model provider. Do not submit proprietary source.
- **Container images are not built end to end** in the capture environment, so
  the non-root user and reduced attack surface defined in the Dockerfiles have
  not been exercised.

**Agent posture**

- **The injection screen is a shape-matcher.** Novel phrasings will pass it. The
  zero-tool Diagnostician is what actually holds.
- **`METADATA_SERVICE_AUTH_ENABLED` must be `true`.** The quickstart default is
  `false`, under which none of the server-side policies do anything.
- **The approver is the local operator** in every recorded run, not an
  independent reviewer on a real team.
- **`save_document` is granted platform-wide.** Documents are not
  resource-scoped in DataHub's privilege model, so no narrower grant exists for
  the runbook artifact.
- **No defence against a compromised DataHub server.** DevGuard distrusts the
  catalog's free text while trusting its structure — schemas, lineage, URNs.

---

## Dependencies

Pinned in `requirements.txt` and `frontend/package-lock.json`.

**Dependency scanning runs in CI** on every push — `pip-audit` against
`requirements.txt` and `npm audit` against the frontend lockfile, with both
reports uploaded as build artifacts.

It is deliberately **report-only and does not gate the build.** Both trees carry
known advisories with no in-range fix, so gating would leave CI red with no code
change available to fix it, and would go red again on any upstream publication
against a pinned version — which trains people to ignore CI.

> **Do not read a green tick on that job as "no advisories". Read the report.**

### Python

`pip-audit` originally reported **56 known vulnerabilities across 7 packages**.
Two changes — dropping a redundant `aiohttp` pin, then moving FastAPI/Starlette
to a patched pairing — brought that to **8 across 4**, each triaged against this
codebase rather than reported as a count:

| Package | Pinned | Advisories | Reachable here? |
|---|---|---|---|
| `transformers` | 4.57.6 | 5 | **No** — arrives via `sentence-transformers`, which is itself unimportable |
| `protobuf` | 4.25.9 | 1 | **No** — `json_format.ParseDict` is never called |
| `python-dotenv` | 1.0.0 | 1 | **No** — `set_key` / `unset_key` are never called |
| `pytest` | 8.4.2 | 1 | **No** — test-only, not shipped |

Reachability was established from the code, not assumed: importing the real
application and inspecting `sys.modules` shows `aiohttp`, `transformers`,
`torch`, `chromadb`, `sentence_transformers` and `multipart` are **never
loaded**.

**Stated carefully:** each is unreachable *given the code as it stands today*.
That is a fact about this application's current shape, not a clean bill of health
for the dependencies.

Notes on the two fixes, because the reasoning is not obvious:

- The `aiohttp` pin could never have removed the package — it arrives
  transitively via `chromadb → kubernetes → aiohttp`. All the pin did was hold a
  dependency nothing imports at the *oldest* version in range.
- Reaching a patched `starlette` required `fastapi` 0.104.1 → **0.136.0**, and
  deliberately *not* the latest: newer releases introduce `_IncludedRouter` in
  `app.routes`, which the pinned `opentelemetry-instrumentation-fastapi==0.41b0`
  crashes on — measured as HTTP 500 on every request. `starlette` is pinned
  explicitly so the package carrying the advisories cannot drift.

`chromadb` and `sentence-transformers` pull a very large transitive tree
(~5.4 GiB including CUDA). Both are optional accelerators behind `try/except` in
`backend/core/rag_store.py`. With both unimportable, retrieval falls back to
deterministic lexical overlap.

### Frontend

14 open advisories (12 high, 2 moderate). **None is in Next.js's own code** —
`next` carried 21 distinct advisories at 14.2.35, the latest 14.x with no
in-range fix, and carries zero at 16.

What remains, triaged rather than counted:

- **`sharp` (high, 4 inherited libvips CVEs)** — a new exposure the upgrade
  introduced, since Next 16 uses `sharp` for image optimization where 14 used a
  bundled wasm path. No in-range fix. Not invoked here: the app renders no
  `next/image` and no remote images. Adding a single `next/image` would make it
  reachable.
- **Nine ESLint-toolchain packages (high)** — a brace-expansion DoS reached
  through glob matching. Pre-existing, lint-time only, never reaches shipped code.
- **`postcss` nested inside `next` (high, 3)** — needs attacker-controlled CSS
  *source* at build time. The app's own top-level `postcss` is patched.
- **`monaco-editor` → `dompurify` (moderate)** — pre-existing, editor-only.

`npm audit fix` without `--force` clears nothing further; `--force` "resolves"
`sharp` by downgrading `next` to 9.3.3.

**Seven ESLint findings are demoted to warnings, not fixed.** The ESLint 9 flat
config that Next 16 required brought newer rule sets, and four rules that did not
exist before now fire on pre-existing code. They are demoted rather than
suppressed — there is no `eslint-disable` anywhere, and they are printed on every
lint run and in CI — with the reasoning in `frontend/eslint.config.mjs`. Two are
genuine minor defects: a bare `<a href="/">` to an internal route, and a ref
assigned during render.

---

Related: [Architecture](ARCHITECTURE.md) · [Deployment](DEPLOYMENT.md) · [API](docs/API.md)

## Assertions — read, never authored

`backend/v2/assertions.py` reads a dataset's assertions and their run events as
an independent second opinion on recovery. Three properties keep it inside the
existing security model rather than widening it:

- **Read-only.** Reads are already unrestricted, so no privilege changes and
  none of the least-privilege evidence is invalidated.
- **No second write channel.** DataHub OSS's GraphQL has no
  `reportAssertionResult` mutation — that is a DataHub Cloud surface. Emitting
  an `assertionRunEvent` aspect through the ingestion REST path would work, and
  is deliberately not done: it is a second write channel with its own
  credential and blast radius, and the single-writer rule exists to stop one
  being added casually.
- **A null answer is never agreement.** A dataset with no assertions, an
  assertion that never ran, an unreachable catalog and a GraphQL error all
  return `NOT_CORROBORATED`. `evidence()` refuses to mint a chain item for that
  verdict, because `EvidenceChain.is_sufficient()` counts `DATAHUB_GRAPH` by
  source alone — a failed lookup would otherwise satisfy the two-source rule it
  exists to enforce.
