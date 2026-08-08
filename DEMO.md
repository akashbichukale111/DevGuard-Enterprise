# Demo guide

A walkthrough for showing DevGuard Enterprise in three to five minutes, and the
recording notes for a shorter cut.

Everything in the primary path below runs **with no API key, no catalog, no
database and no network**. That is deliberate: a demonstration that depends on
live infrastructure is a demonstration that can fail in front of an audience.

---

## Setup (once, before you present)

```bash
git clone https://github.com/akashbichukale111/DevGuard-Enterprise.git
cd DevGuard-Enterprise/frontend

npm ci && npm run dev
```

Open <http://localhost:3000/command>. Leave it running.

No Python, no backend, no catalog — the replay bundles are committed. If you would
rather present from the static export (what the hosted demo serves):

```bash
cd frontend && NEXT_OUTPUT=export npm run build && npx --yes serve out
```

---

## The walkthrough

### 1 · The problem (30s)

> "An upstream column gets renamed. Downstream dbt models break, and a churn
> model quietly starts training on a stale feature table. The engineer on call
> has the error message but not the lineage, not the owner, and no record of the
> last time this happened."

### 2 · The loop that ran (60s)

Select **`d6-loop-pass2`** in the run picker.

Point at the handoff rail. Eleven nodes, left to right — the nine loop agents,
plus the **Sentinel** (the injection boundary, which produces artifacts without
owning a handoff edge, so it renders `ran_no_record`) and the **Auditor** (a
`to_agent`-only terminal). Say the number you can see on screen; a viewer
counting along is the fastest way to lose them.

> "Detection is deterministic — an LLM cannot make an exit code more true. The
> Cartographer resolves the failing artifact to real DataHub URNs. The Pathfinder
> walks column-level lineage and terminates at the ML model. Eight of these nine
> agents use no model at all — and the one that reasons holds zero tools."

Click any evidence chip.

> "Every number on this screen comes out of a proof pack. This chip opens the
> exact request and response behind its claim — the real bytes the server
> returned."

### 3 · The write-back (45s)

Scroll to the write-back panel.

> "Five artifacts landed in DataHub: the incident, a post-mortem runbook, a
> column-level tag and description, structured properties, and ownership. Nothing
> is written until recovery is verified, and the incident is marked resolved
> last. Writes are idempotent, so a second run over the same incident does not
> duplicate anything."

Then switch to **`d6-loop-pass1`** and back.

> "This is the loop actually closing. On the second pass the Archivist retrieves
> three documents — knowledge the first pass wrote. Without that, this is a
> pipeline that happens to run twice."

### 4 · The refusal — the moment that matters (60s)

Switch to **`d5-refusal`**.

> "Here the catalog was unreachable, so the evidence chain had runtime evidence
> and nothing from the graph. A root cause needs both — runtime alone is an error
> message, graph alone is a theory."

Point at `INSUFFICIENT_EVIDENCE`.

> "It refuses, and it names the evidence class it was missing: `DATAHUB_GRAPH`.
> That is enforced structurally, not by prompt wording — the Diagnostician has
> zero tools and reasons only over the typed bundle. An agent that always answers
> is easy to build and impossible to trust."

Then **`d6-fail-the-fix`**.

> "And here the proposed fix failed validation in a throwaway schema. The loop
> stopped before any human was asked, and nothing was written."

### 5 · Honesty as a feature (30s)

Point at the cost field showing **N/A**.

> "No model was reachable when these runs were captured, so cost was never
> measured — and it says so. A plausible zero would read as 'this loop was free'.
> The banner says REPLAY OF RECORDED RUN and cannot be dismissed."

### 6 · Verification (45s)

Drop to a terminal.

```bash
make test                # 1104 passed
make verify-replay-ui    # 14/14 checks passed
```

> "1104 tests in CI on every push, with no key, no collector and no network. And
> the demonstration you just watched is itself verified — that second command
> drives the built site in a real browser and asserts every guarantee, including
> that the evidence chips open real captured bytes."

### 7 · Observability (30s)

Show the SigNoz screenshots in `docs/screenshots/signoz/`, or a live instance if
you have one.

> "Every agent boundary is an OpenTelemetry span. One incident, one distributed
> trace, with logs bridged onto it. The dashboard and three alert rules ship in
> the repository and install with one script."

---

## Short cut (2 minutes)

Sections 2, 4 and 6. The loop, the refusal, and the fact that both are verified.

The refusal is the strongest single moment — lead with it if you only have one.

---

## Live variant

If you have a catalog, a substrate and a key, the same story runs live:

```bash
python scripts/reset_demo.py
python scripts/run_d6_loop.py --run-id live-demo
make replay && make replay-serve
```

Budget several minutes for the loop, and have the recorded runs open in a second
tab as a fallback. See [docs/INSTALLATION.md](docs/INSTALLATION.md) for the full
stack.

---

## What not to claim

Keeping these straight is what makes the rest credible:

- **Do not** say the agents query SigNoz telemetry via MCP. That path is designed
  and stubbed, not demonstrated — see [docs/MCP_DECISION.md](docs/MCP_DECISION.md).
- **Do not** quote a scanner accuracy figure. The benchmark has never been run to
  an artifact, and the UI correctly says "accuracy not measured".
- **Do not** quote the ablation's time-to-root-cause delta as a result. The arms
  overlap heavily; it is not distinguishable from noise, and the published
  document says so.
- **Do not** present the 7/7 evaluation score as general diagnostic accuracy. The
  fault set and the classifier share an author. The false-positive rate and the
  `silent_value_drift` case are the results worth quoting.
