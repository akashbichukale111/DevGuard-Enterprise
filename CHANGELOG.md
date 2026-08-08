# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Verified against a live DataHub v1.7.0

The whole stack was provisioned from the official `datahub docker quickstart`,
interrogated, driven end to end, and photographed. Trail:
[`evidence/datahub-live/`](evidence/datahub-live/).

### Added

- `scripts/verify_datahub_capabilities.py` — probes every capability against the
  live server and keeps *"is the field in this build's schema"* apart from *"did
  this catalog return data"*, so `ABSENT` and `PRESENT_NO_DATA` can never collapse
  into "supported". Result on v1.7.0: **25 verified · 2 present-but-empty · 0
  absent · 0 error** over 27 probes, with every raw GraphQL response kept.
- `scripts/capture_datahub_screenshots.py` — drives the real UI and records the
  outcome of every attempt in `MANIFEST.json`, including failures. **23 captures**
  in `docs/screenshots/datahub/`, five of them the post-write-back catalog state
  that was previously listed as the repository's one missing screenshot.
- `substrate/ml/register_model.py` — registers the trained churn model with the
  lineage hop that makes it reachable. `MLModelProperties.trainingData` produces
  no traversable graph edge, so the path runs `dataset → dataJob → mlModel`.
- `scripts/provision_catalog.py` (was `provision_domain.py`) — the domain and the
  **tag vocabulary** DevGuard writes into. The Scribe refuses to mint a missing
  tag, so this is an operator responsibility by design.
- `recipes/business_glossary.yml` + `recipes/glossary/devguard_glossary.yml` — the
  glossary the dbt `meta_mapping` references, as a committed file.
- **Catalog surface** panel in the Command Center
  (`frontend/components/command/CatalogSurface.tsx`) — the DataHub tool set each
  run negotiated, and which of it the agents used. Six tools on a document-less
  catalog, eight once a run has written a runbook: the loop closing, as two
  numbers read out of the proof pack.
- `evidence/datahub-live/` — service verification, the auth-off/auth-on A/B, the
  resolved configuration table, and the capability matrix.
- A DataHub block in `.env.example`, each variable naming the module that reads it.
- `tests/test_documentation_integrity.py` — every relative link resolves, every
  in-page anchor resolves, every committed screenshot is displayed somewhere, the
  documents judges are pointed at exist, and mermaid fences are closed and typed.
  The orphan check found a 724 KB Command Center capture committed and referenced
  by nothing; it is now the live-run screenshot in the README.
- The live v1.7.0 run on screen in the README — blast radius terminating at
  `devguard_churn_risk`, `NAMED_OWNER` routed to an owner read from the graph, and
  five write-back artifacts, four `WRITTEN` and one `ALREADY PRESENT`.
- 37 tests (`tests/test_replay_catalog_surface.py`,
  `tests/test_screenshot_manifest.py`, `tests/test_documentation_integrity.py`).
  Suite is now **1101**.

### Changed

- `recipes/dbt.yml` ingests **dbt test results as DataHub Assertions**
  (`test_results_path`) and maps ownership and PII classification in through
  DataHub's own `meta_mapping`. 13 dbt tests are now first-class `Assertion`
  entities, which is what lets the Referee corroborate recovery against a verdict
  it did not produce.
- Ownership, tags and glossary terms are declared in the dbt project rather than
  clicked into a UI, so catalog governance is reproducible from a clone.
- `versions.env` records **two** DataHub generations. `v1.6.0` stays because the
  committed `d4`/`d5`/`d6-loop` proof packs were captured against it; `v1.7.0` is
  what the quickstart gives a reviewer today.
- `scripts/verify_least_privilege.py` resolves the incident it raises. Every
  previous run left an ACTIVE incident on a production dataset.
- Two claims lost their *"not yet executed against a live catalog"* caveat: the
  blast radius reaching a registered `mlModel` (`reaches_ml_model=True` over 7
  impacted assets), and ownership resolved from the graph (`NAMED_OWNER`).
  Seven of seven privilege denials are now proven live, up from four.

### Fixed

- **`scripts/verify_least_privilege.py` could damage the catalog it was
  verifying.** Its DENY probes are real mutations, and under the quickstart's
  default `METADATA_SERVICE_AUTH_ENABLED=false` nothing refuses them — so they
  land. One run soft-deleted the hero dataset, added a cycle to its lineage, and
  created a policy granting the agent `MANAGE_POLICIES`. It now detects
  non-enforcement by presenting a forged token and refuses to run. The failing run
  is kept as evidence.
- `--only` in the screenshot capture overwrote `MANIFEST.json` with a partial
  record, so the index generated from it disagreed with the directory. Partial
  runs now merge — including the login-failure exit, which wrote the manifest on
  its way out and clobbered it just the same. Each capture also carries its own
  timestamp, so a two-panel re-shoot can no longer re-date the twenty-one panels
  it did not take.
- dbt requires model `tags` and `meta` under `config:`; a top-level `tags:` key is
  accepted and silently ignored, which is why the first ingestion produced no tags.
- The capability prober followed DataHub **siblings**. Profiling lands on the
  warehouse URN and ownership on the dbt one, so a single-URN probe reported no
  ownership on a catalog that had it.

### Withdrawn

- The proposed upstream suggestion that `get_lineage` responses should carry a
  truncation marker. Reading the live response shows `total` already present
  beside the results page. Recorded in `docs/upstream/README.md` rather than
  deleted — a withdrawn proposal is a result.

## [1.0.0] — 2026-08-08

First public release.

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

---

[Unreleased]: https://github.com/akashbichukale111/DevGuard-Enterprise/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/akashbichukale111/DevGuard-Enterprise/releases/tag/v1.0.0
