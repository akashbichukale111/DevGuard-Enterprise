# Devpost submission copy

Ready to paste. Every number and claim here is verifiable in the repository, and
the sections are named to match Devpost's own form fields.

**Before submitting:** confirm the test count with `make test` — this file says
1,039 and the suite grows. Nothing else here goes stale.

---

## Title

DevGuard Enterprise

## Tagline

A governed incident agent that proves root cause from the DataHub graph — and writes what it learned back into the catalog.

## Short description

When a data pipeline breaks, the hard part is not noticing — it is *explaining*.
DevGuard is a nine-agent system that detects a real production break, proves
root cause and blast radius from a live DataHub catalog, proposes a minimal fix
under a least-privilege gate routed to the asset's registered owner, verifies
recovery, and only then writes verified incident knowledge back into DataHub as
first-class metadata. The next incident on a related asset starts from more
knowledge than the last one.

---

## Long description

### The problem

A column gets renamed upstream. Downstream dbt models break. A churn model
silently starts training on a stale feature table. The error message tells you
*what* failed; it cannot tell you which change caused it, what it touched
downstream, who owns the asset, or whether a proposed fix is safe.

That reasoning lives in the catalog. Nothing was using it.

### What DevGuard does

Nine agents, each with one responsibility and an explicit tool allowlist
enforced **before** the request reaches the MCP pipe:

**Watcher** observes the real failure — exit codes, build output.
**Cartographer** resolves the failing artifact to real DataHub URNs and pulls
schema truth. **Archivist** searches the catalog for prior runbooks.
**Pathfinder** traverses lineage for blast radius, including the ML model at the
end of it. **Diagnostician** reasons about root cause — and holds **zero tools**.
**Surgeon** proposes a minimal diff on a branch, never applies. **Referee**
validates in a throwaway schema and verifies recovery. **Magistrate** routes
approval to the owner resolved from the graph. **Scribe** is the only agent that
can write, and only after recovery is verified.

### What lands in DataHub

Five artifacts, idempotently, and only once recovery is verified:

1. Incident raised, then resolved — `raiseIncident` → `updateIncidentStatus`
2. Post-mortem runbook — `save_document` → Context Document
3. Column-level tag and description on the schema field
4. Structured incident properties
5. Ownership signal

**And the loop closes.** On the second pass, the Archivist retrieves the runbook
the first pass wrote — straight out of the catalog.

### What makes it different

**Refusal is structural, not prompt discipline.** The Diagnostician holds no
tools. An evidence chain must carry at least one `RUNTIME` item *and* at least
one `DATAHUB_GRAPH` item before a root cause is allowed: runtime alone is an
error message, graph alone is a theory about an incident that may not have
happened. When the chain cannot form, the agent declines and names the missing
evidence class. That refusal is a recorded run with its own proof pack.

**A bad patch writes nothing.** `d6-fail-the-fix` is a recorded run where
validation fails, the loop stops, no human is paged and no catalog is touched.

**Recovery needs two sources.** The Referee's local test run is one — and it is
the same system that proposed the fix. DataHub's own assertions are the second.
A contradiction from the catalog outranks a local pass.

**Unmeasured values render `N/A` with a reason.** Never a plausible zero. The
provenance system reports the *weakest* component of a mixed payload, never the
most flattering.

---

## Technical highlights

- **1,104 tests**, no API key, no network, no catalog required
- **Zero-infrastructure replay** — seven recorded runs, committed as proof packs
- **Hash-chained tamper-evident audit trail**
- **Least privilege proven, not asserted** — four privilege denials verified live
  against a real DataHub, with the three that are *not* proven stated as such
- **Prompt-injection boundary** on catalog free text, with the architectural
  backstop that the agent an injection would most want to influence has no tools
- **Circuit breaker** with real model degradation; opt-in API-key auth;
  per-client rate limiting on the endpoints that spend money
- **OpenTelemetry** → SigNoz, with log↔trace correlation

## Innovation

- A **published negative result**: retrieval made time-to-root-cause *slower*
  (5.14 s vs 4.87 s, N=5 per arm). Published because it was measured.
- **Documentation checked by tests.** `tests/test_judging_matrix.py` fails the
  build if a cited artifact path stops resolving or a quoted test count is
  overstated. `tests/test_least_privilege_claims.py` fails if a security
  document claims a privilege denial the evidence does not prove — added
  because it had.
- **MCP contract conformance**, checked offline: every DataHub tool name in the
  project is validated against the published `mcp-server-datahub` tool surface,
  so a rename upstream fails CI instead of failing against a live catalog.

## Use of DataHub

DataHub is the reasoning substrate, not a data store.

**13 of 21 published MCP tools are genuinely invoked** — `search`,
`get_entities`, `list_schema_fields`, `get_lineage`,
`get_lineage_paths_between`, `get_dataset_queries`, `search_documents`,
`grep_documents`, plus the five write tools held by the Scribe alone. The eight
unused are every `remove_*` variant, `add_terms`, `set_domains` and `get_me` —
excluded by design, not oversight.

Also used: column-level lineage for blast radius, ownership for approval
routing, Context Documents for knowledge persistence, structured properties,
incidents, Access Policies, domains, glossary terms, dataset profiles, and —
read-only — Assertions and `mlModel` metadata.

**Verified against a live DataHub v1.7.0, not asserted.** The stack was
provisioned from the official `datahub docker quickstart`, and every capability
claim was checked by executing a query against it:
`evidence/datahub-live/CAPABILITY_MATRIX.md` — **25 verified · 2 present-but-empty
· 0 absent · 0 error** across 27 probes. The prober asks two separate questions
per capability (is the field in the introspected schema; did it return data)
because collapsing those into one "supported" column is how a capability matrix
starts lying. The two empties are freshness and usage statistics, which need a
connector that reads warehouse query history; DataHub's Postgres source does not,
so they cannot be filled from this substrate at all.

The full incident loop then ran end to end against that instance, with
authentication enforced, as a least-privilege service account (**ALLOW 5/5, DENY
7/7**). All five write-back artifacts landed, and the next run's Archivist
retrieved the runbook the previous run had written. 23 screenshots of the running
instance are in `docs/screenshots/datahub/`, including the post-write-back state.

Two claims that previously carried an explicit *"not yet executed against a live
catalog"* caveat no longer do: the blast radius reaching a registered `mlModel`
(`reaches_ml_model=True` over 7 impacted assets), and ownership resolved from the
graph (`NAMED_OWNER`).

## Open source contribution

Two genuine DataHub findings, prepared and reproducible, in `docs/upstream/`:

1. **`UpdateIncidentStatusInput` is declared in `incident.graphql` and
   referenced by nothing.** The mutation takes `IncidentStatusInput`; the dead
   type is the one whose name matches the mutation, with an identical field set.
   Verified against `master`.
2. **The quickstart ships Access Policies silently unenforced** — re-confirmed on
   **v1.7.0**. Under `METADATA_SERVICE_AUTH_ENABLED=false` every DENY case
   passes, so a policy test suite returns green whether the policy is correct,
   wrong, or absent. The second run found something the first write-up missed:
   because DENY cases are *mutations*, they do not merely appear to pass — **they
   execute**. Running the suite against a stock quickstart soft-deleted the
   dataset under test, put a cycle in its lineage, and created a policy granting
   the test account `MANAGE_POLICIES`. The finding now documents that, and
   supplies a client-side way to detect the state: present an invalid bearer
   token and see whether the server returns 401 or answers.

Neither has been filed. The checklists in those documents say so.

A third candidate was **withdrawn** rather than filed, and the withdrawal is kept
in `docs/upstream/README.md`: this project had proposed that `get_lineage`
responses should carry a marker letting a client detect truncation. Reading the
live response shows `total` already there beside the results page. The information
existed; this project was not reading it.

## Known limitations

Stated plainly, because a claim a reviewer can disprove costs more than the
feature was worth:

- **No recorded run invoked a model.** All 49 handoff records carry
  `model=null`. Proven to be a network egress block rather than a credential
  problem — see `docs/LLM_EGRESS_BLOCKED.md`. The evidence rule, refusal path
  and chain validation are proven; the *quality of model reasoning* is not.
- **The ablation is a negative result**, published anyway.
- **Semantic retrieval is degraded** to an embedder the code names
  `lexical-overlap-fallback[NOT-PROD]`.
- **Assertions and ML metadata are implemented and tested but never executed
  against a live catalog.**
- **State is in-memory.** Single instance; a restart loses pending approvals.
- **Authentication is off unless configured.**

---

## Built with

`datahub` · `mcp` · `python` · `fastapi` · `nextjs` · `typescript` ·
`opentelemetry` · `signoz` · `groq` · `dbt` · `postgresql`

## Links

- **Repository:** https://github.com/akashbichukale111/DevGuard-Enterprise
- **Live Command Center:** https://dev-guard-enterprise.vercel.app/command
- **Judge walkthrough:** `docs/JUDGE_WALKTHROUGH.md`
- **Judging matrix:** `docs/JUDGING_MATRIX.md`

---

## Demo script — 3 minutes

| # | Time | Scene | Say |
|---|---|---|---|
| 1 | 0:00–0:15 | `/command`, `d6-loop-pass2` | "Nine agents, a real DataHub catalog, and every number on screen read from a captured proof pack." |
| 2 | 0:15–0:40 | Click an evidence chip | "Blast radius comes from DataHub's lineage graph, not a guess. Every chip opens the captured request and response." |
| 3 | 0:40–1:00 | Switch to `d5-refusal` | "The Diagnostician holds **zero tools**. When the evidence chain lacks a required class it refuses and names what's missing." |
| 4 | 1:00–1:20 | Policy panel | "Approval routes to the asset's registered owner, from the graph. CRITICAL has no approval path at all." |
| 5 | 1:20–1:45 | `d6-fail-the-fix` | "Validation fails. Nothing is written back. A bad fix reaches no human and touches no catalog." |
| 6 | 1:45–2:20 | Back to pass 2, write-back panel | "Incident raised then resolved, runbook as a Context Document, column-level tag, structured properties, ownership. Idempotent, all-or-nothing." |
| 7 | 2:20–2:40 | Archivist on pass 2 | "**The loop closes.** Pass 2 retrieves the runbook pass 1 wrote — straight out of the catalog." |
| 8 | 2:40–3:00 | Terminal: `make test` | "1,104 tests, no API key, no network. And the limitations are in the README — no recorded run invoked a model, and the ablation is a published negative result." |

**Lead with DataHub. Do not open with the Scanner.** Scene 7 is the one judges
remember — put it before the test count, never after.

## Submission checklist

- [ ] Test count re-confirmed with `make test`
- [ ] Video recorded and uploaded
- [ ] Repository link added
- [ ] Live Command Center link verified in a browser
- [ ] Upstream issues filed (optional, but the largest remaining score movement)
- [ ] Devpost submitted
