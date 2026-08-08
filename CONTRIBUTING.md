# Contributing

Thanks for your interest in DevGuard Enterprise. This document covers the
development setup, the checks that must pass, and the conventions the codebase
follows.

By participating you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

---

## Development setup

```bash
git clone https://github.com/akashbichukale111/DevGuard-Enterprise.git
cd DevGuard-Enterprise

python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cd frontend && npm ci && cd ..

make doctor    # confirms the toolchain
make test      # confirms a green baseline before you change anything
```

Full setup, including the catalog and substrate, is in
[docs/INSTALLATION.md](docs/INSTALLATION.md).

---

## Before you open a pull request

```bash
make verify
```

That runs everything CI runs: tests, secret scan, OTLP verification, frontend
lint and build. All of it works with no API key, no collector and no network, so
there is no reason for a contributor to have a red local build and a green CI or
the reverse.

If your change touches the Command Center or the replay bundle:

```bash
make verify-replay-ui
```

---

## What CI enforces

| Job | Gates the build? |
|---|---|
| Backend — import without a key, tests, OTLP verification | yes |
| Frontend — lint, typecheck, build | yes |
| Secret scan — working tree and full git history | yes |
| Dependency advisories — `pip-audit`, `npm audit` | **no, report-only** |

The dependency job is deliberately non-blocking. Both trees carry advisories with
no in-range fix, so gating would leave CI red with no code change available to
fix it — which trains people to ignore CI. Read the uploaded report rather than
the tick.

---

## Conventions

**Determinism where determinism is available.** Eight of the nine agents use no
model — `backend/v2/agents/diagnostician.py` is the only agent module that
imports an inference client at all. If you are adding logic that a deterministic implementation could handle,
implement it deterministically. "We did not put a model where a model was not
needed" is a design property worth keeping.

**Refusal is a valid outcome.** An agent that cannot answer from its evidence
must say so and name what is missing. Do not add a fallback that produces a
plausible answer from insufficient evidence.

**Enforce before the wire.** Tool allowlists and mutation scope are checked in
the client, ahead of serialisation, so a violation is a Python exception with a
stack trace. Keep new controls in the same place.

**Never publish an unmeasured number.** Anything that was not measured renders
`N/A` with a reason attached. A plausible zero is worse than a blank, because a
blank cannot be quoted. If you add a value to the API or the UI, it must come
from a real artifact.

**Do not hand-edit generated documents.** `examples/eval/README.md` and
`examples/ablation/README.md` are rendered from JSON. Change the renderer and
re-render; the test suite fails if they drift.

**Evidence directory names are stable identifiers.** Directories under
`evidence/` and run IDs under `evidence/proof-pack/` are referenced by code, by
tests and by the replay manifest. Renaming one invalidates the artifacts it
labels — treat them as immutable.

**Security patterns are load-bearing.** The detection patterns in
`backend/v2/sentinel.py` include model and assistant names because those are
tokens an attacker would use. Do not remove them for tidiness. Any change there
must keep `tests/test_sentinel_fencing.py` and
`tests/test_prompt_injection_boundary.py` green.

---

## Tests

Add a test for every behaviour you would be unhappy to see silently regress. The
suite is structured around properties rather than lines:

| Area | Example |
|---|---|
| Agent boundaries | `tests/test_agent_allowlists.py` |
| Evidence rules | `tests/test_evidence_contract.py` |
| Refusal | `tests/test_diagnostician_refusal.py` |
| Write-back | `tests/test_writeback_rules.py` |
| Security | `tests/test_security_posture.py`, `tests/test_sentinel_fencing.py` |
| Telemetry fail-safety | `tests/test_telemetry_failsafe.py` |
| Replay | `tests/test_replay_bundle.py` |

Tests must not require an API key, a catalog, a collector or a network.

---

## Commit messages

Say what changed and why it mattered. A message that explains the reasoning is
worth more than one that restates the diff.

---

## Reporting bugs

Open an issue with the command you ran, what you expected, what happened, and the
output of `make doctor`. For anything security-related, see
[SECURITY.md](SECURITY.md#reporting-a-vulnerability) first.
