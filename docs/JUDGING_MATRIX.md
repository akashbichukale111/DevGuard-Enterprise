# Judging matrix

One row per shipped capability, mapped to the criterion it raises and the artifact that proves it. **Every artifact path in this file exists in the repository** — the table is checked by `tests/test_judging_matrix.py`, which fails if a path stops resolving.

Where a row is weaker than it looks, the Honest limits column says so. A matrix that only lists strengths is a marketing document.

---

## Criterion 1 — Meaningful DataHub usage

| Capability | Evidence | Honest limits |
|---|---|---|
| Incident raised → resolved via GraphQL | [`d6-loop-pass2/scribe/artifact1-raiseIncident.json`](../evidence/proof-pack/d6-loop-pass2/scribe/artifact1-raiseIncident.json) | Recorded against a local DataHub Core, not a hosted instance |
| Runbook published as a Context Document | [`artifact2-save_document.json`](../evidence/proof-pack/d6-loop-pass2/scribe/artifact2-save_document.json) | — |
| Column-level tag + description on a schema field | [`artifact3-add_tags.json`](../evidence/proof-pack/d6-loop-pass2/scribe/artifact3-add_tags.json) | — |
| Structured properties, definitions registered first | [`artifact4-add_structured_properties.json`](../evidence/proof-pack/d6-loop-pass2/scribe/artifact4-add_structured_properties.json) | — |
| Ownership signal | [`write-back-summary.json`](../evidence/proof-pack/d6-loop-pass2/scribe/write-back-summary.json) | — |
| Column-level lineage read for blast radius | [`backend/v2/agents/pathfinder.py`](../backend/v2/agents/pathfinder.py) | Column-level traversal stops at the last dataset; only the dataset-level trace reaches the `mlModel` |
| MCP over stdio, capability negotiated | [`backend/v2/datahub_client.py`](../backend/v2/datahub_client.py) | — |
| **13 of the 16 published `mcp-server-datahub` tools are actually invoked** | [`backend/v2/mcp_contract.py`](../backend/v2/mcp_contract.py) · [`tests/test_mcp_contract.py`](../tests/test_mcp_contract.py) (19 tests) | The three unused are `get_me` and the glossary/domain writes, which are deliberately outside the Scribe's allowlist |
| Tool names checked offline against the published contract | [`tests/test_mcp_contract.py`](../tests/test_mcp_contract.py) | The contract is a transcription recorded 2026-08-07; it cannot detect an upstream change until someone re-reads the source it names |
| Retrieval loop — prior runbooks read back | [`backend/v2/agents/archivist.py`](../backend/v2/agents/archivist.py) | Measured effect was negative; see Criterion 2 |
| **Assertions read as an independent second opinion on recovery** | [`backend/v2/assertions.py`](../backend/v2/assertions.py) · [`tests/test_assertion_corroboration.py`](../tests/test_assertion_corroboration.py) (27 tests) | Read-only. DataHub OSS has no `reportAssertionResult` mutation, so DevGuard corroborates but does not author assertion results. **Not yet executed against a live catalog** |
| Write-back idempotency + all-or-nothing resolve | [`tests/test_writeback_rules.py`](../tests/test_writeback_rules.py) (35 tests) | — |

## Criterion 2 — Technical execution

| Capability | Evidence | Honest limits |
|---|---|---|
| 944 tests, no API key or network required | `make test` | — |
| Clean-clone reproducibility | [`docs/REPRODUCIBILITY.md`](REPRODUCIBILITY.md) | — |
| Fault-injection eval: 7/7, 0 false positives, control case | [`examples/eval/results.json`](../examples/eval/results.json) | Measures the deterministic detection path, **not** LLM diagnosis — stated in the artifact itself |
| Retrieval ablation, N=5 per arm | [`examples/ablation/timings.json`](../examples/ablation/timings.json) | **Negative result**: 5.14 s with retrieval vs 4.87 s without. Published because measured |
| Circuit breaker with real model degradation | [`backend/core/resilience.py`](../backend/core/resilience.py) | — |
| Zero-infrastructure replay | [`frontend/public/replay/`](../frontend/public/replay/) · CI drift guard | — |
| Rate limiting on the endpoints that cost money | [`tests/test_rate_limit.py`](../tests/test_rate_limit.py) (12 tests) | Per-process; behind N replicas the effective limit is N× |
| Failure diagnosis surfaced, not swallowed | [`tests/test_scan_error_diagnosis.py`](../tests/test_scan_error_diagnosis.py) (9 tests) | — |

## Criterion 3 — Originality

| Capability | Evidence | Honest limits |
|---|---|---|
| Structural refusal — Diagnostician holds zero tools | [`tests/test_diagnostician_refusal.py`](../tests/test_diagnostician_refusal.py) (29 tests) · run `d5-refusal` | — |
| Evidence chain rule: ≥1 RUNTIME **and** ≥1 DATAHUB_GRAPH | [`backend/v2/evidence.py`](../backend/v2/evidence.py) | — |
| Prompt-injection resistance on catalog free-text | [`backend/v2/sentinel.py`](../backend/v2/sentinel.py) · [`tests/test_sentinel_fencing.py`](../tests/test_sentinel_fencing.py) | — |
| A bad patch writes **nothing** back | run `d6-fail-the-fix` | — |
| Dry-run shows exact payloads, sends nothing | run `d6-dry-run` | — |
| Per-agent tool allowlists enforced pre-wire | [`tests/test_agent_allowlists.py`](../tests/test_agent_allowlists.py) (26 tests) | — |
| Per-axis provenance that reports the **weakest** component, never the most flattering | [`backend/core/god_mode_orchestrator.py`](../backend/core/god_mode_orchestrator.py) · [`tests/test_god_mode_provenance.py`](../tests/test_god_mode_provenance.py) | A payload mixing measured and invented fields badges `partial`, never `live` |
| Unmeasured values render `N/A` with a reason rather than a plausible number | [`tests/test_measured_error_rate.py`](../tests/test_measured_error_rate.py) (21 tests) | The memory *leak rate* is still a scenario — labelled `synthetic_scenario`, not passed off as measured |

## Criterion 4 — Real-world value

| Capability | Evidence | Honest limits |
|---|---|---|
| Owner-routed approval from the graph | [`backend/v2/agents/magistrate.py`](../backend/v2/agents/magistrate.py) | — |
| Least-privilege service account, denials verified | [`evidence/proof-pack/security/least-privilege/`](../evidence/proof-pack/security/least-privilege/) · [`tests/test_least_privilege_claims.py`](../tests/test_least_privilege_claims.py) (7 tests) | **4** privilege denials proven live, plus one URN-scope denial. Glossary and domain probes are in the verifier but not yet executed; `EDIT_ENTITY_STATUS` has no probeable mutation in DataHub's GraphQL. The docs said all seven were verified — that overclaim is now build-enforced against the artifact |
| Hash-chained tamper-evident audit trail | [`tests/test_audit_chain.py`](../tests/test_audit_chain.py) (37 tests) | — |
| ZIP / repository scanning with hostile-input handling | [`tests/test_project_scan.py`](../tests/test_project_scan.py) (36 tests) | Capped at 25 files per run |
| Cost and token accounting per incident | [`backend/core/telemetry.py`](../backend/core/telemetry.py) | Renders `N/A` in recorded runs — no model was invoked |
| Secret redaction at capture | [`tests/test_proof_pack_redaction.py`](../tests/test_proof_pack_redaction.py) (19 tests) | — |
| Opt-in API-key auth on the spending endpoints | [`backend/core/auth.py`](../backend/core/auth.py) · [`tests/test_api_key_auth.py`](../tests/test_api_key_auth.py) (28 tests) | Shared secret, not an identity system — no users, scopes, rotation or expiry. **Off unless `DEVGUARD_API_KEYS` is set**, so a clean clone stays open by design |

## Criterion 5 — Submission quality

| Capability | Evidence | Honest limits |
|---|---|---|
| README with quickstart, architecture, limitations | [`README.md`](../README.md) | — |
| One-command demo | `make demo` | — |
| Zero-setup replay URL | https://dev-guard-enterprise.vercel.app | Scanner and Nexus need a backend; only the Command Center works without one |
| Architectural review | [`docs/ARCHITECTURE_REVIEW.md`](ARCHITECTURE_REVIEW.md) | — |
| Eligibility disclosure | [`DISCLOSURE.md`](../DISCLOSURE.md) | — |
| Apache-2.0, detected by GitHub | [`LICENSE`](../LICENSE) | — |
| **Demo video** | — | **Not recorded** |
| **Devpost submission** | — | **Not submitted** |

---

## The three things a judge should be told before they find them

1. **No recorded run invoked a model.** All 49 handoff records across the committed bundles carry `model=null, tokens=0`, and the Diagnostician reports `REASONER_UNAVAILABLE`. The evidence rule, the refusal path and the chain validation are proven; the *quality of model reasoning* is not. Root causes in those runs were derived deterministically from runtime evidence, and each artifact says so.

2. **The ablation is a negative result.** Retrieval made time-to-root-cause *slower* (5.14 s vs 4.87 s, N=5 per arm). Both arms reached the root cause the same way, so the delta is the cost of retrieval and says nothing about its benefit. It is published because it was measured.

3. **Semantic retrieval is degraded.** `sentence-transformers` and `chromadb` are pinned but neither is importable under a current dependency resolution, so the CWE store falls through to an embedder the code names `lexical-overlap-fallback[NOT-PROD]`. Retrieval stays deterministic and relevant; it is not semantic.
