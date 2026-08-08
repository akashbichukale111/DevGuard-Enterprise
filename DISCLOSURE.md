# Disclosure

This project **evolves an existing repository** rather than starting a new one. That
choice moves the burden of proof onto this document, so it is written to be exact
rather than reassuring.

---

## 1 · Work authored during the Registration & Submission Period

**All DataHub integration, evidence, write-back, agent, and hero-scenario code was
authored during the Registration & Submission Period.**

The DataHub work begins at the repository audit that opened it and runs to the current
`main`:

| | Commit | Date |
|---|---|---|
| First commit of the DataHub work | `1f2c4a8` — environment verdict, `versions.env`, V2 skeletons | 2026-07-31 |
| First live DataHub integration | `144b2c2` — DataHub Core v1.6.0 up, MCP tool list captured, all write paths proven | 2026-07-31 |
| Final implementation commit | `f243042` — Command Center and zero-infrastructure replay | 2026-08-05 |

Everything in the table below marked **New** was authored in that window.

---

## 2 · Component attribution

### New — authored in-window

| Component | Path | What it is |
|---|---|---|
| The nine-agent system | `backend/v2/agents/` | Watcher, Cartographer, Archivist, Pathfinder, Diagnostician, Surgeon, Referee, Magistrate, Scribe |
| DataHub MCP client | `backend/v2/datahub_client.py` | JSON-RPC 2.0 over stdio, with tool/entity/URN allowlist enforcement |
| Evidence model | `backend/v2/evidence.py` | Typed evidence, trust and confidence classes, chain sufficiency rule |
| Handoff contract | `backend/v2/handoff.py` | `AgentHandoff`, tool allowlists, mutation scope |
| Proof-pack writer | `backend/v2/proofpack.py` | Capture-time redaction, size caps |
| Replay compiler | `backend/v2/replay.py` | Proof pack → self-contained replay bundle |
| Prompt-injection boundary | `backend/v2/sentinel.py` | Untrusted-text fencing and screening |
| Fault injection + ablation | `backend/v2/faults.py`, `backend/v2/ablation.py` | Evaluation and measurement harnesses |
| Command Center UI | `frontend/app/command/`, `frontend/components/command/` | Incident header, handoff rail, evidence ledger, blast radius, root cause, policy/approval, write-back, security panels |
| Data substrate | `substrate/` | PostgreSQL seed, dbt project, scikit-learn churn model |
| Ingestion recipes | `recipes/` | PostgreSQL, dbt, structured-property definitions |
| All evidence | `evidence/` | Every proof pack and captured artifact |
| Evaluation + ablation results | `examples/` | Fault-injection suite, retrieval ablation |
| Verification and demo scripts | `scripts/` | Least-privilege verifier, injection demo, replay builder, loop runners |
| Test suite | `tests/` | 1101 tests |

### Pre-existing — carried over and disclosed

| Component | Path | Why it was reused |
|---|---|---|
| Scanner → Fixer → Validator loop | `backend/core/ai_agent.py` | A working multi-agent reflection loop with typed boundaries |
| Typed agent schemas | `backend/core/schemas.py` | Pydantic contracts at every agent boundary |
| OpenTelemetry layer | `backend/core/telemetry.py` | Traces, metrics, logs, W3C propagation, log↔trace bridge |
| Circuit breaker / resilience | `backend/core/resilience.py` | Fallback routing and graceful degradation |
| Hash-chained audit trail | `backend/core/audit.py` | Tamper-evident scan record |
| Benchmark harness | `backend/core/benchmark.py` | Accuracy harness; has never been run to an artifact |
| RAG store | `backend/core/rag_store.py` | CWE/OWASP retrieval |
| SigNoz MCP client | `backend/core/mcp_client.py` | Self-observation interface — **never verified against a real MCP server**, see [`docs/MCP_DECISION.md`](docs/MCP_DECISION.md) |
| Local cost shadow | `backend/core/local_telemetry.py` | In-process spend estimate |
| Scanner / Nexus UI | `frontend/app/scanner/`, `frontend/app/nexus/`, `frontend/app/result/` | Pre-existing surfaces, retained and labelled |

The pre-existing components are a code-security scanner. **They are not the submitted
thesis.** The DataHub incident agent — everything in the *New* table — is.

---

## 3 · AI-assisted development

AI coding assistants were used as an engineering productivity tool during development,
in the same category as an IDE, a linter, or a code generator.

All architecture, implementation decisions, testing, validation and final verification
were reviewed by the project author, who is responsible for the contents of this
repository.

---

## 4 · What the evidence is, and is not

The evidence, evaluation results and benchmark figures published here were **produced by
executing the code in this repository**, not written as prose:

- Proof packs under `evidence/` are captured request and response payloads from real runs
  against a real DataHub catalog and a real PostgreSQL substrate.
- Evaluation results in `examples/eval/` come from faults really injected into a real
  database, followed by a real `dbt build`, classified from real output.
- Ablation timings in `examples/ablation/` come from real clocks over interleaved runs.
- Both published result documents are **rendered from JSON by scripts**, and the test
  suite fails if either drifts from its source.

**What the evidence does not show:** no language model executed in any recorded run. All
49 handoff records carry `model=null, tokens=0`, and the Diagnostician returns
`REASONER_UNAVAILABLE` — the capture environment denied egress to the inference endpoint.
Root causes in those runs were derived deterministically from runtime evidence, and each
artifact says so. This is stated in the README's [Limitations](README.md#limitations) and
is not worked around anywhere.

---

## 5 · Verification

```bash
make test                # 1101 tests
make verify              # everything CI runs
make verify-replay-ui    # the demonstration UI, in a real browser
```

See [`docs/REPRODUCIBILITY.md`](docs/REPRODUCIBILITY.md) for what each command proves and
what infrastructure, if any, it requires.
