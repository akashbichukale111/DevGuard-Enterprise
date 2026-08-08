# Upstream contributions

Findings from building against DataHub that belong to DataHub rather than here.

**Nothing in this directory has been filed.** Each document is prepared to the
point where filing is a copy-paste, and each ends with a checklist whose last
two items — *search for a duplicate* and *file* — are deliberately unticked.
Claiming a contribution that does not exist would be worse than making none.

Finding 01 now also carries a **complete patch**, in
[`01-patch/`](01-patch/): the diff against `master` @ `f4fda77c` (verified to
apply cleanly to a pristine checkout) and a written-out
[pull request](01-patch/PULL_REQUEST.md) following DataHub's own PR title
format. It is unfiled for two concrete reasons, both stated there: the
duplicate search needs the upstream issues API, and the Java half has not been
compiled — `./gradlew :datahub-graphql-core:compileJava` cannot resolve
`com.linkedin.pegasus` from this sandbox.

| # | Finding | Target | Type | Verified |
|---|---|---|---|---|
| [01](01-incident-status-input-duplicate.md) | `updateIncidentStatus` declares one input type and binds another | `datahub-project/datahub` | Correctness + docs | Against `master` @ `f4fda77c` |
| [02](02-quickstart-policies-silently-unenforced.md) | Access Policies silently unenforced under the quickstart default | `datahub-project/datahub` | Documentation | Against a running stack; **re-confirmed on v1.7.0** |

## Why these two and not more

Both cost real debugging time on this project, and both are reproducible by
someone who has never seen this repository. That is the bar. A list of
speculative improvements would be longer and worth less — an upstream
maintainer's time is the scarce resource, and an issue that cannot be
reproduced from its own text consumes it without repaying it.

**Finding 01 was re-verified against a full clone and changed as a result.** It
had been checked by grepping `datahub-graphql-core/src/main/resources/` only,
which showed `UpdateIncidentStatusInput` referenced nowhere and led to a
proposed fix of *delete it*. A repository-wide search shows a Java resolver
binds that type, so the original patch would not have compiled. The real defect
is larger — schema, resolver and published docs each name a different input type
— and the corrected document says so, keeps the superseded reasoning visible,
and states plainly that the new patch has not been compiled either. Filing a
confidently wrong patch costs a maintainer more than filing nothing.

Two candidates were considered and rejected:

- **`mcp-server-datahub` has no assertion tool.** Assertions are reachable only
  over GraphQL, which forced a second transport in this project
  (`backend/v2/assertions.py`). That is a genuine gap, but it is a *feature
  request* against a young server, not a defect, and it is likely already on a
  roadmap this project cannot see. Filing it as a bug would be noise.
- **`get_lineage` defaults to `max_results=30`.** This silently truncated every
  blast radius here until `34639d4`. But the default is documented in the
  signature and the parameter is right there — the bug was ours, not
  DataHub's.

  This entry previously went further and proposed that the response should carry
  a `hasMore`/`total` marker so a client could *detect* truncation rather than
  infer it from a short page, deferred until the response shape had been seen
  against a live catalog. **It has now been seen, and the proposal is withdrawn:
  the marker already exists.** `total` is a top-level field of the
  `downstreams` object, alongside the `searchResults` page:

  ```json
  {"downstreams": {"total": 7, "facets": [...], "searchResults": [...]}}
  ```

  Captured at
  [`d6-live-v170/pathfinder/get_lineage-downstream.json`](../../evidence/proof-pack/d6-live-v170/pathfinder/get_lineage-downstream.json).
  A client can compare `total` against the number of results it received and know
  it was truncated. There is nothing to ask upstream for; the information was
  there and this project was not reading it. Recorded rather than deleted,
  because a withdrawn proposal is a result.

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
