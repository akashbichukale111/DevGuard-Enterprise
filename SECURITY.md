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
| Scope | five named dataset URNs | `MUTATION_SCOPE_URNS` |

An agent that requests a tool it does not hold raises `ToolNotAllowedError`; a
write outside scope raises `EntityNotInScopeError`. Both are Python exceptions
with stack traces rather than server-side rejections that have to be interpreted.

Reads are deliberately unrestricted. The blast radius of reading a dataset
DevGuard does not own is nil, and narrowing reads would break lineage traversal.

This duplicates the server-side Access Policy on purpose — and that redundancy
proved its worth, as the next section explains.

Pinned by `tests/test_agent_allowlists.py`.

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

**Never granted, each verified as a live DENY:** `DELETE_ENTITY`,
`EDIT_LINEAGE`, `EDIT_ENTITY_STATUS`, `MANAGE_POLICIES`, `MANAGE_INGESTION`,
`EDIT_ENTITY_GLOSSARY_TERMS`, `EDIT_DOMAINS_PRIVILEGE`.

```
$ python scripts/verify_least_privilege.py
ALLOW: 4/4 behaved as required
DENY : 5/5 correctly refused
```

### Why that verifier exists

On its first run the account passed all four ALLOW cases **and all five DENY
cases also succeeded** — a failed verification in which nothing errored. The
account could delete datasets, rewrite lineage, write anywhere in the catalog,
and grant itself further privileges. The policies existed and looked correct in
the UI.

**Cause: the DataHub quickstart ships with
`METADATA_SERVICE_AUTH_ENABLED=false`, under which Access Policies are not
enforced at all.** Until this check existed, the server-side authorization
control was silently absent and creating policies gave a false sense of security.

> **Enabling `METADATA_SERVICE_AUTH_ENABLED=true` is a hard prerequisite for
> anyone reproducing this.** Preserve the token signing key when you do, so
> existing tokens stay valid.

A control you have not tried to violate is a control you have not verified. Full
record in [`evidence/d9/`](evidence/d9/).

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

**676 tests run on every push in CI**, with no API key, no collector and no
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

- **CORS is wide open.** `allow_origins=["*"]`. Acceptable locally; it must be an
  explicit allowlist before any public deployment.
- **No authentication on any endpoint.** Anyone who can reach the backend can
  submit a scan, read the audit log, and approve or reject a gated fix. Do not
  expose this to the internet as-is.
- **No rate limiting.** `POST /scan` forwards code to a paid API with no
  per-caller quota, so an open instance is a cost-exhaustion target. A single
  request's blast radius is bounded by the size cap; the request *rate* is not.
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
