# Upstream contributions

Findings from building against DataHub that belong to DataHub rather than here.

**Nothing in this directory has been filed.** Each document is prepared to the
point where filing is a copy-paste, and each ends with a checklist whose last
two items — *search for a duplicate* and *file* — are deliberately unticked.
Claiming a contribution that does not exist would be worse than making none.

| # | Finding | Target | Type | Verified |
|---|---|---|---|---|
| [01](01-incident-status-input-duplicate.md) | `UpdateIncidentStatusInput` declared but never referenced | `datahub-project/datahub` | Schema cleanup | Against `master` |
| [02](02-quickstart-policies-silently-unenforced.md) | Access Policies silently unenforced under the quickstart default | `datahub-project/datahub` | Documentation | Against a running stack |

## Why these two and not more

Both cost real debugging time on this project, and both are reproducible by
someone who has never seen this repository. That is the bar. A list of
speculative improvements would be longer and worth less — an upstream
maintainer's time is the scarce resource, and an issue that cannot be
reproduced from its own text consumes it without repaying it.

Two candidates were considered and rejected:

- **`mcp-server-datahub` has no assertion tool.** Assertions are reachable only
  over GraphQL, which forced a second transport in this project
  (`backend/v2/assertions.py`). That is a genuine gap, but it is a *feature
  request* against a young server, not a defect, and it is likely already on a
  roadmap this project cannot see. Filing it as a bug would be noise.
- **`get_lineage` defaults to `max_results=30`.** This silently truncated every
  blast radius here until `34639d4`. But the default is documented in the
  signature and the parameter is right there — the bug was ours, not
  DataHub's. A more defensible upstream suggestion would be to have the
  response carry a `hasMore`/`total` marker so a client can *detect*
  truncation rather than infer it from a short page. That is worth proposing
  once this project has run it against a live catalog and can show the
  response shape, which it has not yet done.

## Reusable components

Two modules here were written to be liftable, and would need only their
`backend.v2` imports removed:

- **`backend/v2/mcp_contract.py`** — the published `mcp-server-datahub` tool
  surface as data, with provenance, plus the read/mutation/user/document
  grouping and the gating flags each group needs. Any project wiring an agent
  to that server needs this and will otherwise hard-code tool-name strings.
- **`backend/v2/assertions.py`** — reading a dataset's assertions and their run
  events as a health signal, with the "no assertions is not a pass" rule that
  is easy to get wrong in the flattering direction.

Neither is proposed for upstreaming as-is. They are noted here because
"reusable" is a claim worth being concrete about.
