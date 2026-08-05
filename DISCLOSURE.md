# Development disclosure

## AI-assisted development

AI coding assistants were used as an engineering productivity tool during the
development of this project, in the same category as an IDE, a linter, or a code
generator.

All architecture, implementation decisions, testing, validation and final
verification were reviewed by the project author, who is responsible for the
contents of this repository.

## What this does not mean

The evidence, evaluation results and benchmark figures published here were
**produced by executing the code in this repository**, not generated as prose:

- Proof packs under `evidence/` are captured request and response payloads from
  real runs against a real DataHub catalog and a real PostgreSQL substrate.
- Evaluation results in `examples/eval/` come from faults really injected into a
  real database, followed by a real `dbt build`, classified from real output.
- Ablation timings in `examples/ablation/` come from real clocks over interleaved
  runs.
- The two published result documents are **rendered from JSON by scripts**, and
  the test suite fails if either drifts from its source — so a number cannot be
  edited into the documentation.

Where something was not measured, it is reported as not measured. The
[Limitations](README.md#limitations) section states what is unproven, and the UI
renders `N/A` with a reason rather than a plausible placeholder.

## Verification

Every claim in this repository can be checked independently:

```bash
make test                # 676 tests
make verify              # everything CI runs
make verify-replay-ui    # the demonstration UI, in a real browser
```

See [docs/REPRODUCIBILITY.md](docs/REPRODUCIBILITY.md) for what each command
proves and what infrastructure, if any, it requires.
