# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

**DataHub agent platform**
- Nine bounded agents — Watcher, Cartographer, Archivist, Pathfinder,
  Diagnostician, Surgeon, Referee, Magistrate, Scribe — each with an explicit
  tool allowlist enforced before the request reaches the wire
- Typed evidence model with source, trust and confidence classification, and a
  chain rule requiring both `RUNTIME` and `DATAHUB_GRAPH` evidence for a valid
  root cause
- Structural refusal: `INSUFFICIENT_EVIDENCE` as a first-class outcome that names
  the missing evidence class
- `AgentHandoff` contract recording agents, evidence IDs, decision, duration,
  tokens, model and every tool call
- Column-level blast radius terminating at the ML model
- Five-artifact write-back — incident, runbook, column tag and description,
  structured properties, ownership — idempotent on
  `(incident_id, artifact_type, target_urn)` and gated on verified recovery

**Command Center and replay**
- Zero-infrastructure replay UI built from committed proof packs
- Replay bundle compiler and static export
- Browser-driven verification asserting all fourteen replay guarantees

**Observability**
- OpenTelemetry traces, metrics and logs over OTLP/gRPC, with log-to-trace
  correlation
- SigNoz dashboard and three alert rules, with an installer and a verifier
- `scripts/verify_otel.py` — proves the pipeline against decoded protobuf with no
  collector required
- Circuit breaker with fallback routing and an automatic postmortem on open
- Telemetry-aware model routing with a hard floor: `critical` severity is never
  downgraded

**Security**
- Untrusted-content boundary with sentinel fencing at every agent prompt
- Mutation allowlist across tools, entity types and URN scope
- Least-privilege service account with denied privileges verified as live DENYs
- Autonomy policy where published and enforced are the same object
- Proof-pack redaction at capture time
- Secret scanning over the working tree and the full git history

**Evaluation and evidence**
- Fault-injection suite: 7 faults, real injection, real `dbt build`, real
  classification, with a negative control
- Retrieval ablation: 2 arms, N = 5 each, interleaved
- Committed proof packs for every recorded run

**Engineering**
- 1041 tests in CI on every push, with no key, no collector and no network
- `make doctor` preflight
- Dependency advisory reporting
- Apache-2.0 licence

### Fixed

- Backend now imports and boots without an API key
- Approval gate reads the Scanner's finding rather than the request, so a caller
  cannot supply a low severity to bypass review
- Audit trail records the pipeline's real verdict
- Scan cache reads back what it writes
- Per-request cost accounting reports provider-reported figures where available,
  and says which
- Critical-severity routing floor no longer fails open
- Resilient fallback genuinely degrades, and the span reports it accurately
- `GET /audit-log` paginates at the storage layer instead of parsing the whole log
- Audit append and chain verification moved off the event loop
- Bounded three in-memory scan-state dictionaries, closing a measured
  53 KiB-per-scan leak
- RAG retrieval made deterministic
- Container build path corrected: build context is the repository root, and the
  application is launched as `backend.main:app`
- `SIGNOZ_MCP_URL` defaults to empty rather than to the backend's own port, and
  an unconfigured client raises before any network I/O

### Documentation

- README expanded with a judge quick-start, a documentation map, a DataHub
  catalog-reasoning matrix, an evidence-model section, a replay-system section, a
  testing matrix, an API overview, a deployment-architecture diagram and a
  references table — four new Mermaid diagrams, all parser-validated
- Documented the two rail roles that are not loop agents: the **Sentinel**
  renders `ran_no_record` because it produces artifacts without owning a handoff
  edge, and the **Auditor** is a `to_agent`-only terminal. This is why the
  Command Center shows eleven nodes and the agent table lists nine
- Documented the model-routing table (`MODEL_STRONG` / `MODEL_CHEAP`) and the
  severity floor that keeps a `critical` scan off the cheap model

### Fixed — documentation accuracy

- Corrected two screenshot captions that claimed more than the images showed:
  `evidence/d2/screenshots/02-schema.png` was captioned as *"schema with
  agent-written tags"* while the capture reads **No tags yet**, and
  `01-lineage.png` was captioned as *"column-level lineage"* while its explorer
  canvas rendered no edges. Both images are pre-write-back captures of the
  ingested substrate and are kept and re-captioned as such; the actual
  column-level-lineage evidence is the `fineGrainedLineages` aspect in
  `evidence/d2/02-` and `03-`, which the README now cites instead
- Recorded the absence of a post-write-back catalog screenshot in Limitations
  rather than letting a substrate capture stand in for one
- Corrected the SigNoz trace caption: the stored trace is a **Scanner** request
  with 8 of 9 spans erroring, not the nine-agent chain, which remains uncaptured
- Corrected the evidence inventory: **7** recorded loop runs and **7** replay
  bundles, not 10 and 8 — the ablation, eval and security packs are excluded from
  bundle compilation by design
- Corrected the MCP denominator in the judging matrix: 13 tools invoked out of
  **21** enumerated by the contract, of which the captured server exposed 18
- Refreshed stale counts: test total (676 / 1037 → **1041**) and repository scale
  (27,514 lines / 105 files → **30,537 / 117**), with the counting method stated
- Noted in `magistrate.py` that its quoted role line says *"deterministic + LLM"*
  while the implementation makes no model call — the docs describe it as
  deterministic because `diagnostician.py` is the only agent that imports an
  inference client

### Removed

- Fabricated values from the `/scan` response and the operations panels
- Unbacked accuracy figures — no number reaches the UI without an artifact
