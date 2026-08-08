# Reproducibility

Every claim in this repository is reproducible, and this document says exactly
what each command needs. Where something cannot be reproduced without
infrastructure, it is listed with the infrastructure it needs rather than left
for you to discover.

---

## Needs nothing

No API key, no catalog, no collector, no network. These run on a clean clone.

| Command | What it proves | Time |
|---|---|---|
| `make doctor` | What is present and what is missing | seconds |
| `make test` | 1041 tests across agents, evidence, security, replay | ~2.5 min |
| `make replay` | Replay bundles build from the committed proof packs | seconds |
| `make replay-build` | The Command Center exports as a static site | ~1 min |
| `make verify-replay-ui` | The built site behaves as claimed, in a real browser | ~1 min |
| `python scripts/verify_otel.py` | OTLP export, trace context and log correlation, against decoded protobuf | ~30 s |
| `make scan-secrets` | No credential patterns in any tracked file | seconds |
| `make verify` | Everything CI runs | ~3 min |

### Expected output

```
$ make test
1041 passed

$ make replay
[ok]   d6-loop-pass2         30 evidence   32 artifacts    159.5 KiB
[ok]   d6-loop-pass1         29 evidence   31 artifacts    151.9 KiB
[ok]   d6-dry-run            31 evidence   32 artifacts    165.3 KiB
[ok]   d6-fail-the-fix       24 evidence   24 artifacts    140.5 KiB
[ok]   d5-refusal             4 evidence    5 artifacts     22.5 KiB
[ok]   d5-full               12 evidence   13 artifacts     78.4 KiB
[ok]   d4-evidence-chain     12 evidence   12 artifacts     77.0 KiB

wrote 7 bundle(s) + manifest.json to frontend/public/replay

$ make verify-replay-ui
14/14 checks passed

$ python scripts/verify_otel.py
PASSED: OTLP export, trace context propagation, and log<->trace correlation
        all verified against decoded protobuf.

$ make scan-secrets
secret scan: clean
```

`verify_otel.py` is worth understanding rather than trusting: it stands up an
in-process OTLP/gRPC receiver, starts a real uvicorn, drives traffic through it,
and asserts against the **decoded protobuf** the receiver actually got. That is
why the telemetry pipeline can be proven in CI without SigNoz running.

---

## Needs the substrate

Bring it up with `docker compose -f substrate/docker-compose.yml up -d`.

| Command | What it proves |
|---|---|
| `make eval` | Fault-injection suite: 7 faults really injected, really built, really classified |
| `python scripts/render_eval_readme.py` | The published numbers are rendered from `results.json`, never typed |

The evaluation isolates itself: faults hit `raw_eval`, a real clone built per
run; models build into `analytics_eval*` as the non-superuser role
`devguard_eval`; everything is dropped in a `finally`. The demo substrate is
never touched.

The non-superuser role matters — `REVOKE` against a superuser is a silent no-op,
so `permission_revoked` would otherwise "pass" by never breaking anything.

---

## Needs the substrate, DataHub and a token

| Command | What it proves |
|---|---|
| `python scripts/reset_demo.py` | The substrate returns to the broken demo state |
| `python scripts/run_d4_evidence_chain.py` | The evidence chain forms from real evidence |
| `python scripts/run_d5_diagnosis.py --scenario refusal` | The Diagnostician refuses on a one-sided chain |
| `python scripts/run_d6_loop.py` | The full remediation loop, including write-back |
| `make ablation` | Retrieval ablation, N = 5 per arm, interleaved |
| `python scripts/verify_least_privilege.py` | 4/4 ALLOW, 5/5 DENY |
| `python scripts/run_injection_demo.py` | Prompt injection fenced before reaching a prompt |

Set `DATAHUB_TOKEN_FILE` to a `chmod 600` file outside the repository.

> `verify_least_privilege.py` reporting **DENY 0/5** means
> `METADATA_SERVICE_AUTH_ENABLED` is still `false` and the server is enforcing
> nothing. That is a real finding, not a script bug — see
> [SECURITY.md](../SECURITY.md#least-privilege).

---

## Needs a running SigNoz

| Command | What it proves |
|---|---|
| `./scripts/verify_signoz.sh` | The stack comes up and spans are genuinely stored |
| `./scripts/apply_signoz_assets.sh` | Dashboard and three alert rules install and load |

---

## Pinned versions

Everything is resolved, not floating. See [`versions.env`](../versions.env):

```
DATAHUB_VERSION=v1.6.0
DATAHUB_COMMIT=059a36c0b035a6057de00114ccac0ea9003d6bc2
MCP_SERVER_VERSION=0.6.0
DATAHUB_SDK_VERSION=1.6.0.16
DATAHUB_CLI_VERSION=1.6.0.16
```

SigNoz images are pinned in [DEPLOYMENT.md](../DEPLOYMENT.md#option-b--self-hosted).
Python dependencies are pinned in `requirements.txt`; frontend dependencies in
`frontend/package-lock.json`.

The DataHub version was read back from the **running instance**
(`GET /config`), not taken from documentation. The capture is in
[`evidence/d0/datahub-config.json`](../evidence/d0/datahub-config.json).

---

## Generated documents

Two published documents are rendered from data, never edited by hand:

| Document | Rendered from | By |
|---|---|---|
| `examples/eval/README.md` | `examples/eval/results.json` | `scripts/render_eval_readme.py` |
| `examples/ablation/README.md` | `examples/ablation/timings.json` | `scripts/render_ablation_readme.py` |

`tests/test_fault_eval.py` and `tests/test_ablation.py` fail if either file
drifts from its source, so a number cannot be edited into the documentation
without the suite noticing.

---

## What cannot be reproduced here

Stated so you do not spend time on it:

- **Model-backed diagnosis.** Every recorded run reports
  `REASONER_UNAVAILABLE`, because the capture environment's egress policy denied
  the inference endpoint. The refusal path, the chain rule and the evidence
  typing are all proven; the quality of model reasoning is not.
- **The retrieval ablation's benefit side**, for the same reason. What was
  measured is retrieval's *cost*.
- **The scanner accuracy benchmark**, which has never been run to an artifact.
  No accuracy figure is published anywhere as a result.
- **Complete container image builds**, blocked by the same egress policy on the
  Debian and PyPI mirrors.

Each of these is a missing execution, not a missing implementation. The harnesses
are complete and run to completion on a machine with the relevant access.

---

Related: [Installation](INSTALLATION.md) · [Evidence](../evidence/) · [Evaluation](../examples/eval/) · [Ablation](../examples/ablation/)
