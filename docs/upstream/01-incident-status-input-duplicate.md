# `updateIncidentStatus` is declared with one input type and implemented with another

**Target:** `datahub-project/datahub`
**Type:** Correctness (schema/resolver mismatch) + documentation
**Status:** verified against `master` @ `f4fda77c`, ready to file
**Files:** `datahub-graphql-core/src/main/resources/incident.graphql` ·
`datahub-graphql-core/src/main/java/com/linkedin/datahub/graphql/resolvers/incident/UpdateIncidentStatusResolver.java` ·
`docs/incidents/incidents.md`

> **This document previously claimed something weaker and proposed a fix that
> would not compile.** The earlier version said `UpdateIncidentStatusInput` was
> declared and referenced by nothing, and proposed deleting it. That conclusion
> came from grepping only `datahub-graphql-core/src/main/resources/`. A
> repository-wide search shows the type *is* referenced — from Java, not from
> the schema — and deleting it alone would break the build. The corrected
> finding below is a better one, and the record of the error is kept because a
> reproduction that was too narrow is worth knowing about.

---

## Summary

`updateIncidentStatus` declares one input type in the schema and binds a
**different** one in its resolver:

| Layer | File | Says the input is |
|---|---|---|
| Schema | `incident.graphql:24` | `IncidentStatusInput!` |
| Resolver | `UpdateIncidentStatusResolver.java:47` | `UpdateIncidentStatusInput` |
| Public docs | `docs/incidents/incidents.md:317` | `UpdateIncidentStatusInput!` |

All three disagree with at least one of the others, and the schema — the only
one clients validate against — is outvoted two to one.

It works today for one reason: the two input types are **structurally
identical**, so `bindArgument`, which deserializes the raw argument map into a
POJO, does not care which class it is handed.

```java
// UpdateIncidentStatusResolver.java:47
final UpdateIncidentStatusInput input =
    bindArgument(environment.getArgument("input"), UpdateIncidentStatusInput.class);
```

```graphql
# incident.graphql:435 and :455 — same three fields, twice
input IncidentStatusInput       { state: IncidentState!  stage: IncidentStage  message: String }
input UpdateIncidentStatusInput { state: IncidentState!  stage: IncidentStage  message: String }
```

## Why this is worth fixing

**It is a latent correctness bug, not a cosmetic one.** The coupling between the
declared type and the bound type is currently *coincidental*. The moment either
type gains a field the other lacks, `updateIncidentStatus` silently starts
dropping or mis-binding it — with no compile error and no schema validation
error, because each half is independently valid. That is the worst shape a bug
can have.

**The rest of the codebase already agrees on the other type.**
`IncidentStatusInput` is the load-bearing one:

| Referenced by | Where |
|---|---|
| `updateIncidentStatus` argument | `incident.graphql:24` |
| `RaiseIncidentInput.status` | `incident.graphql:368` |
| `UpdateIncidentInput.status` | `incident.graphql:402` |
| `IncidentUtils.mapIncidentStatus(...)` | `IncidentUtils.java:88` |
| Three resolver tests | `UpdateIncidentResolverTest`, `IncidentUtilsTest`, `RaiseIncidentResolverTest` |

`UpdateIncidentStatusInput` is referenced by exactly one file —
`UpdateIncidentStatusResolver.java` — which is the outlier. Its sibling
`IncidentUtils.mapIncidentStatus` performs the same state/stage/message mapping
and already takes `IncidentStatusInput`.

**The published documentation is wrong in three separate ways.**
`docs/incidents/incidents.md` documents the mutation as:

```graphql
updateIncidentStatus(urn: String!, input: UpdateIncidentStatusInput!): String
```

against a schema that says:

```graphql
updateIncidentStatus(urn: String!, input: IncidentStatusInput!): Boolean
```

- the input type name is wrong — the documented one is not accepted by the server;
- the return type is wrong — `String` documented, `Boolean` implemented;
- the documented input omits `stage`, which the real type has and the resolver
  reads (`input.getStage()`).

A user following the documentation writes a mutation the server rejects at
validation, and the error names a type they were told to use.

**It cost real debugging time downstream.** Recorded at
`backend/v2/agents/scribe.py` in this project: *"the live signature is
`IncidentStatusInput!`, not `UpdateIncidentStatusInput!`"*. The dead type
matches the mutation name, so it is the better guess, and introspecting clients
and codegen emit it as part of the public API surface.

## Reproduction

No running server required:

```bash
git clone --depth 1 https://github.com/datahub-project/datahub && cd datahub

# The schema declares IncidentStatusInput for the mutation:
sed -n '15,25p' datahub-graphql-core/src/main/resources/incident.graphql

# The resolver binds UpdateIncidentStatusInput:
grep -n 'UpdateIncidentStatusInput' \
  datahub-graphql-core/src/main/java/com/linkedin/datahub/graphql/resolvers/incident/UpdateIncidentStatusResolver.java

# The docs claim a third thing:
grep -n 'UpdateIncidentStatusInput\|): String' docs/incidents/incidents.md
```

Against a running GMS, the documented call fails:

```graphql
mutation upd($urn: String!, $input: UpdateIncidentStatusInput!) {
  updateIncidentStatus(urn: $urn, input: $input)
}
```

```
Validation error: argument 'input' with type 'UpdateIncidentStatusInput!'
is not a valid type
```

## Proposed fix

Make the resolver agree with the schema, then remove the now-genuinely-dead
type. This is chosen over the alternative — changing the schema to
`UpdateIncidentStatusInput` — because **the schema is the wire contract**.
Clients already have to send `IncidentStatusInput`, so this direction is
invisible to every working client, whereas renaming the argument's type in the
schema would break any client that names it in a variable definition.

**1 · Align the resolver** (`UpdateIncidentStatusResolver.java`):

```diff
-import com.linkedin.datahub.graphql.generated.UpdateIncidentStatusInput;
+import com.linkedin.datahub.graphql.generated.IncidentStatusInput;
@@
-    final UpdateIncidentStatusInput input =
-        bindArgument(environment.getArgument("input"), UpdateIncidentStatusInput.class);
+    final IncidentStatusInput input =
+        bindArgument(environment.getArgument("input"), IncidentStatusInput.class);
```

Safe because the generated classes have identical field sets, so
`getState()`, `getStage()` and `getMessage()` all resolve unchanged.

**2 · Remove the dead type** (`incident.graphql`), which is only correct *after*
step 1:

```diff
-"""
-Input required to update status of an existing incident
-"""
-input UpdateIncidentStatusInput {
-  ...
-}
```

**3 · Correct the documentation** (`docs/incidents/incidents.md`) — input type,
return type, and the missing `stage` field.

**4 · One-character typo**, same file, in the type that *is* used
(`incident.graphql:442`): `The lifecycle stage ofthe incident` → `of the`. It is
the only `ofthe` in the file; every sibling docstring has the space.

### Ordering matters

Step 2 must not land without step 1. `graphql-java-codegen` generates a class
for **every** declared type — `graphqlSchemaPaths` is the whole
`resources/**/*.graphql` tree, and the output is built into
`src/mainGeneratedGraphQL/java` rather than committed. Deleting the schema type
first therefore deletes the generated
`com.linkedin.datahub.graphql.generated.UpdateIncidentStatusInput` and breaks
the resolver's import at `compileJava`.

## Compatibility

- **Wire contract: unchanged.** The mutation still accepts exactly
  `IncidentStatusInput`, which is what the schema has always required.
- **Introspection: changes.** One unreferenced input type disappears from the
  introspected schema, so codegen that emits every declared type will emit one
  class fewer. No valid operation can reference it — naming it in a variable
  definition already fails validation — so no working query breaks.
- If dropping it from introspection is judged too aggressive for a patch
  release, steps 1, 3 and 4 stand alone and still remove the actual defect;
  step 2 can follow on any schedule.

## What was verified, and what was not

**Verified — the schema half.** All 35 `.graphql` files under
`datahub-graphql-core/src/main/resources` were parsed and built into a single
schema with `graphql-js`, *without* `assumeValidSDL`, after the type was
removed: 991 definitions, **0 validation errors, no dangling type references**.
The check was itself mutation-tested — pointing the mutation at a nonexistent
`TotallyMissingInput` makes it fail with `Unknown type` — because the first
version of this check silently passed everything. It had `assumeValidSDL: true`
set, which skips precisely the validation being claimed.

**Not verified — the Java half.** The patch has **not been compiled**.
`./gradlew :datahub-graphql-core:compileJava` fails in this environment at
dependency resolution, before reaching any changed file: the sandbox's egress
policy returns 403 for `packages.confluent.io` and `linkedin.jfrog.io`, so
`com.linkedin.pegasus:gradle-plugins` cannot be fetched. That is an environment
limitation and says nothing about the patch, but it also means *"it compiles"*
is a claim this document does not make. The change is mechanical — an import
swap between two generated classes with identical field sets — and
`IncidentStatusInput` already exposes the three accessors the resolver calls,
via `IncidentUtils.mapIncidentStatus`. Run `./gradlew :datahub-graphql-core:build`
before filing.

## Suggested issue title

> `updateIncidentStatus` binds `UpdateIncidentStatusInput` while the schema declares `IncidentStatusInput`

## Checklist before filing

- [x] Verified against `master` @ `f4fda77c`, not a pinned release
- [x] Repository-wide reference search, not one directory — this is what
      corrected the original conclusion
- [x] Reproduction is copy-pasteable and needs no running server
- [x] Compatibility impact stated, including the case against step 2
- [x] Fix ordering stated, with the codegen reason it matters
- [x] Explicit about what was not verified (no compile)
- [ ] Searched existing issues for a duplicate — **do this before filing**
- [ ] Filed
