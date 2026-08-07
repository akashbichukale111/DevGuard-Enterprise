# Judge walkthrough

Five minutes, no infrastructure, no API key, no account.

This page exists because the strongest evidence in this repository is not on the
landing page — it is in the recorded runs, and a reviewer with fifteen projects
left to score will not find it by browsing. Everything below is a real path or a
real command; nothing here is illustrative.

---

## 1 · Sixty seconds — see a real recorded run

```bash
git clone https://github.com/akashbichukale111/DevGuard-Enterprise
cd DevGuard-Enterprise
make demo
```

Open <http://localhost:8080/command/>.

That replays a **real incident**, recorded against a real DataHub Core instance
and committed to the repository. No DataHub, no database, no key and no backend
are involved in replaying it. A banner reads **REPLAY OF RECORDED RUN — NOT
LIVE** and cannot be dismissed.

Prefer not to run anything? The same thing is deployed at
<https://dev-guard-enterprise.vercel.app/command>.

## 2 · The four runs worth your time

Use the run picker, top right.

| Run | What it proves | Why it is unusual |
|---|---|---|
| `d6-loop-pass2` | The full loop, ending in five write-back artifacts that really landed in DataHub | The loop **closes** — this pass retrieves the runbook the previous pass wrote |
| `d5-refusal` | The Diagnostician declining to guess, naming the exact evidence class it lacked | It holds **zero tools**. Refusal is structural, not prompt discipline |
| `d6-fail-the-fix` | A patch failing validation | **Nothing is written back.** A bad fix reaches no human and touches no catalog |
| `d6-dry-run` | Every write payload recorded | And nothing sent |

Click any evidence chip. It opens the exact captured request and response behind
that claim.

## 3 · Check a claim without trusting the UI

Every number on that screen is read from a committed proof pack. Read one:

```bash
# The five write-back artifacts, as the server answered them
ls evidence/proof-pack/d6-loop-pass2/scribe/

# The least-privilege verifier's real output
cat evidence/proof-pack/security/least-privilege/summary.json

# Fault-injection evaluation: 7/7, zero false positives, with a control case
cat examples/eval/results.json
```

## 4 · The three things worth knowing before you score us

They are in [`docs/JUDGING_MATRIX.md`](JUDGING_MATRIX.md), stated plainly rather
than left for you to discover:

1. **No recorded run invoked a model.** All 49 handoff records carry
   `model=null`. `api.groq.com` is unreachable from the capture environment — [proven to be a network egress block, not a missing key](LLM_EGRESS_BLOCKED.md). The
   evidence rule, the refusal path and the chain validation are proven; the
   *quality of model reasoning* is not.
2. **The retrieval ablation is a negative result.** Retrieval made
   time-to-root-cause *slower* — 5.14 s vs 4.87 s, N=5 per arm. Published
   because it was measured.
3. **Semantic retrieval is degraded** to an embedder the code itself names
   `lexical-overlap-fallback[NOT-PROD]`.

A matrix that only listed strengths would be a marketing document. Every path in
it is checked by `tests/test_judging_matrix.py`, which fails the build if one
stops resolving.

## 5 · If you have ten minutes

```bash
make test          # 1015 tests, no API key, no network, no catalog
make scan-secrets  # working tree and full git history
```

Then, in rough order of how much they will tell you:

| Read | For |
|---|---|
| [`docs/JUDGING_MATRIX.md`](JUDGING_MATRIX.md) | Every capability mapped to the artifact that proves it, with an Honest limits column |
| [`docs/ARCHITECTURE_REVIEW.md`](ARCHITECTURE_REVIEW.md) | A 360° self-review that scores the project **81/100** and says why |
| [`SECURITY.md`](../SECURITY.md#least-privilege) | Four privilege denials proven live against a real server, and the three that are not |
| [`docs/upstream/`](upstream/) | Two real DataHub defects found here, prepared for filing and **not yet filed** |

## 6 · What we would look at if we were you

Three places where this project is easiest to disprove, so you do not have to
hunt for them:

- **`backend/v2/agents/diagnostician.py`** — the refusal is structural because
  the agent holds no tools. Check `AGENT_TOOL_ALLOWLISTS["diagnostician"]` is
  empty, and that `tests/test_mcp_contract.py` enforces it.
- **`backend/v2/evidence.py`** — `is_sufficient` requires ≥1 `RUNTIME` **and**
  ≥1 `DATAHUB_GRAPH`. It counts by source alone, which is a real weakness we
  documented rather than hid; see `test_assertion_corroboration.py`.
- **`evidence/proof-pack/*/handoff-rail.json`** — every `model` field is `null`.
  We would check that before anything else, and we are telling you where it is.

## 7 · Running it live

Needs a DataHub instance and a Groq key; neither is required for anything above.
[`docs/INSTALLATION.md`](INSTALLATION.md) has the full path, and
[`DEPLOYMENT.md`](../DEPLOYMENT.md) covers Render and Vercel.
