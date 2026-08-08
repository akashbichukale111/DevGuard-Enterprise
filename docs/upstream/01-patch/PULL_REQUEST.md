# Pull request — ready to file, not filed

**Repository:** `datahub-project/datahub`
**Base:** `master` @ `f4fda77c`
**Branch:** `fix/update-incident-status-input-mismatch`
**Patch:** [`0001-fix-graphql-updateIncidentStatus-input-type.patch`](0001-fix-graphql-updateIncidentStatus-input-type.patch) — verified to apply cleanly to a pristine `master`
**Full analysis:** [`../01-incident-status-input-duplicate.md`](../01-incident-status-input-duplicate.md)

Nothing here has been submitted. Filing requires a fork, a duplicate search, and
a successful `./gradlew :datahub-graphql-core:build`, which this environment
could not run — see *Verification* below.

---

## PR title

Their [PR Title Format](https://github.com/datahub-project/datahub/blob/master/docs/CONTRIBUTING.md#pr-title-format)
is `<type>[optional scope]: <description>`, and PRs are squashed using the title
as the commit message.

```
fix(graphql): bind updateIncidentStatus to the input type its schema declares
```

## PR body

### What

`updateIncidentStatus` declares one input type in the schema and binds a
different one in its resolver:

| Layer | Location | Says the input is |
|---|---|---|
| Schema | `incident.graphql:24` | `IncidentStatusInput!` |
| Resolver | `UpdateIncidentStatusResolver.java:47` | `UpdateIncidentStatusInput` |
| Docs | `docs/incidents/incidents.md:317` | `UpdateIncidentStatusInput!` |

This PR makes the resolver agree with the schema, removes the type that is left
unreferenced once it does, and corrects the documentation.

### Why this is worth fixing

It works today only because the two input types are structurally identical —
`bindArgument` deserializes the raw argument map into whichever class it is
handed, so it cannot tell them apart. The coupling is coincidental. **The moment
either type gains a field the other lacks, `updateIncidentStatus` silently drops
or mis-binds it**, with no compile error and no schema validation error, because
each half stays independently valid.

The rest of the codebase already agrees on `IncidentStatusInput`: it is the
mutation's declared argument type, the type of `RaiseIncidentInput.status` and
`UpdateIncidentInput.status`, the parameter of `IncidentUtils.mapIncidentStatus`,
and the type used by `UpdateIncidentResolverTest`, `IncidentUtilsTest` and
`RaiseIncidentResolverTest`. `UpdateIncidentStatusInput` was referenced from
exactly one file — the resolver changed here.

The published docs were wrong in three ways at once: the input type name (the
documented type is rejected by the server), the return type (`String`
documented, `Boolean` implemented), and a missing `stage` field that the real
type carries and the resolver reads. A user following the documentation writes a
mutation that fails validation naming a type the documentation told them to use.

### Why this direction

The schema is the wire contract. Clients already have to send
`IncidentStatusInput`, so aligning the resolver to the schema is invisible to
every working client. Changing the schema to `UpdateIncidentStatusInput` instead
would break any client that names the type in a variable definition.

Ordering within the change matters: `graphql-java-codegen` generates a class for
every declared type, `graphqlSchemaPaths` is the whole `resources/**/*.graphql`
tree, and the output is built into `src/mainGeneratedGraphQL/java` rather than
committed. Deleting the schema type before switching the resolver would delete
the generated class the resolver imports and break `compileJava`.

### Changes

1. `UpdateIncidentStatusResolver.java` — bind `IncidentStatusInput`, matching the
   schema and the sibling `IncidentUtils.mapIncidentStatus`.
2. `incident.graphql` — remove the now-unreferenced `UpdateIncidentStatusInput`.
3. `docs/incidents/incidents.md` — correct the input type, the return type, and
   the missing `stage` field.
4. `docs/how/updating-datahub.md` — record the introspection change under
   **Next → Breaking Changes**, per CONTRIBUTING.
5. One typo in the surviving type's docstring: `stage ofthe incident` → `of the`.
   It was the only occurrence in the file.

### Compatibility

- **Wire contract: unchanged.** The mutation still accepts exactly
  `IncidentStatusInput`, which the schema has always required.
- **Introspection: changes.** One unreferenced input type disappears, so
  generators that emit a class per declared type emit one fewer. No valid
  operation can reference it — naming it in a variable definition already fails
  validation — so no working query breaks.
- If removing it from introspection is too aggressive for a patch release,
  changes 1, 3, 4 and 5 stand alone and still remove the defect; change 2 can
  follow on any schedule.

### Verification

**Schema — verified.** All 35 `.graphql` files under
`datahub-graphql-core/src/main/resources` parse and build into a single schema
with `graphql-js` and no `assumeValidSDL`: 991 definitions, **0 validation
errors, no dangling references** after the removal. The check was mutation-tested
(a deliberately nonexistent type makes it fail with `Unknown type`) because its
first version had `assumeValidSDL: true` and passed everything.

**Java — not verified.** `./gradlew :datahub-graphql-core:compileJava` could not
run here: it fails at dependency resolution with 403 from
`packages.confluent.io` and `linkedin.jfrog.io`, before reaching any changed
file. That is a sandbox egress limitation, not a signal about the patch — but
**this PR should not be merged without a green build**, and the author has not
seen one. `IncidentStatusInput` already exposes the three accessors the resolver
calls (`getState`, `getStage`, `getMessage`), as `IncidentUtils` demonstrates.

### Checklist for whoever files this

- [ ] Fork and push the branch
- [ ] **Search open and closed issues/PRs for a duplicate** — required by
      CONTRIBUTING and not done here; this environment cannot reach the
      `datahub-project/datahub` issues API
- [ ] Run `./gradlew :datahub-graphql-core:build` and confirm it is green
- [ ] Consider whether change 2 belongs in this PR or a follow-up
- [ ] Sign off / DCO as the project requires at filing time
