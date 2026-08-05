# Command Center and zero-infrastructure replay

The incident UI, and the claim that it runs from committed proof packs with no
backend, no catalog, no database and no API key.

**Result: built and verified in a real browser against the real static export.
14/14 UI checks pass, 48 bundle tests pass, and the full suite stays green at
676 tests.**

## The bundle

`make replay` compiles each committed proof pack into one self-contained JSON in
`frontend/public/replay/`. Seven packs build:

```
[ok]   d6-loop-pass2         30 evidence   32 artifacts    159.5 KiB
[ok]   d6-loop-pass1         29 evidence   31 artifacts    151.9 KiB
[ok]   d6-dry-run            31 evidence   32 artifacts    165.3 KiB
[ok]   d6-fail-the-fix       24 evidence   24 artifacts    140.5 KiB
[ok]   d5-refusal             4 evidence    5 artifacts     22.5 KiB
[ok]   d5-full               12 evidence   13 artifacts     78.4 KiB
[ok]   d4-evidence-chain     12 evidence   12 artifacts     77.0 KiB
```

Implementation: `backend/v2/replay.py`, `scripts/build_replay.py`.

## Browser verification

`make verify-replay-ui` builds the static export, serves it, and drives it with
a real browser:

```
  [PASS] page renders with no backend running
  [PASS] replay banner is present and unmissable
  [PASS] all six state-machine states render
  [PASS] cost is N/A rather than a placeholder zero
  [PASS] evidence chip opens the raw payload viewer
  [PASS] the viewer shows real captured bytes
  [PASS] Escape closes the viewer
  [PASS] a rail node opens its AgentHandoff record
  [PASS] a tool call opens its recorded MCP response
  [PASS] the run picker reaches the refusal
  [PASS] the refusal names the missing evidence class
  [PASS] a refused run reports no time-to-root-cause
  [PASS] no uncaught page errors
  [PASS] no failed requests

14/14 checks passed
```

Two of those are worth calling out. **"Cost is N/A rather than a placeholder
zero"** — no model was reachable in the environment these runs were captured in,
so cost was never measured, and the UI says so rather than rendering `$0.00`,
which would read as "this loop was free". **"The viewer shows real captured
bytes"** — every evidence chip opens the actual request/response behind its
claim, so no number on screen can exist without an artifact behind it.

## Screenshots

| File | Shows |
|---|---|
| `screenshots/d6-loop-pass2.png` | The completed loop, ending in five write-back artifacts |
| `screenshots/d5-refusal.png` | The Diagnostician declining, naming the missing evidence class |

Both are produced by the verification run itself, not captured by hand.

## Reproduce

```bash
make replay-serve        # build and browse at http://localhost:8000/command/
make verify-replay-ui    # drive it in a browser and assert
```
