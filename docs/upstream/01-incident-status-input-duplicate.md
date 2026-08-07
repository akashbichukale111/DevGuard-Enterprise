# `UpdateIncidentStatusInput` is declared but never referenced

**Target:** `datahub-project/datahub`
**Type:** GraphQL schema cleanup / developer experience
**Status:** verified against `master`, ready to file
**Files:** `datahub-graphql-core/src/main/resources/incident.graphql`

---

## Summary

`incident.graphql` declares two input types with **identical field sets**:

| Type | Line (master) | Referenced by |
|---|---|---|
| `IncidentStatusInput` | 435 | `updateIncidentStatus`, and two other inputs (lines 368, 402) |
| `UpdateIncidentStatusInput` | 455 | **nothing** |

The mutation is:

```graphql
updateIncidentStatus(
  urn: String!
  input: IncidentStatusInput!
): Boolean
```

The unreferenced type is the one whose name matches the mutation, so it is the
name a client author naturally reaches for. Both carry the same three fields
(`state: IncidentState!`, `stage: IncidentStage`, `message: String`) and
near-identical docstrings, so the mistake produces no type error a reader can
reason about — only a server-side rejection.

## Reproduction

```bash
git clone https://github.com/datahub-project/datahub
cd datahub

# Declared once:
grep -n 'input UpdateIncidentStatusInput' \
  datahub-graphql-core/src/main/resources/incident.graphql
# 455:input UpdateIncidentStatusInput {

# Referenced nowhere:
grep -rn 'UpdateIncidentStatusInput' \
  datahub-graphql-core/src/main/resources/ | grep -v '^.*:455:input'
# (no output)
```

Against a running GMS, the natural-but-wrong call fails:

```graphql
mutation upd($urn: String!, $input: UpdateIncidentStatusInput!) {
  updateIncidentStatus(urn: $urn, input: $input)
}
```

```
Validation error: argument 'input' with type 'UpdateIncidentStatusInput!'
is not a valid type
```

The correct call differs only in the type name:

```graphql
mutation upd($urn: String!, $input: IncidentStatusInput!) {
  updateIncidentStatus(urn: $urn, input: $input)
}
```

## Why this is worth fixing

This is not hypothetical. It cost real debugging time in a downstream project
(recorded at `backend/v2/agents/scribe.py`, *"the live signature is
`IncidentStatusInput!`, not `UpdateIncidentStatusInput!`"*), and the failure
mode is unusually unhelpful:

- The dead type **matches the mutation name**, so it is the better guess.
- The field sets are identical, so no amount of reading the type reveals the
  problem.
- Schema-introspecting clients and codegen tools emit the dead type as part of
  the public API surface, so it looks supported.

An unreferenced input type is also a small ongoing maintenance cost: it will be
kept in sync by reflex, or drift silently, and neither is useful.

## Proposed fix

Remove the unreferenced type:

```diff
-"""
-Input required to update status of an existing incident
-"""
-input UpdateIncidentStatusInput {
-  """
-  The new state of the incident
-  """
-  state: IncidentState!
-  """
-  Optional - The new lifecycle stage of the incident
-  """
-  stage: IncidentStage
-  """
-  An optional message associated with the new state
-  """
-  message: String
-}
```

## Compatibility

Removing an input type that no field, argument or other input references cannot
break a valid query — no operation can name it in a variable definition without
already failing validation. It **is** a visible change to introspection output
and to generated client types, so it is a breaking change for anyone whose
codegen emits every declared type, even unused ones.

If that risk is judged too high for a patch release, the conservative
alternative is to keep the type and mark it:

```graphql
input UpdateIncidentStatusInput @deprecated(
  reason: "Unused. `updateIncidentStatus` takes `IncidentStatusInput`."
) { ... }
```

`@deprecated` is not valid on input object *type definitions* in the GraphQL
spec (only on fields, arguments and enum values), so if that is rejected the
fallback is a docstring making the redirect explicit. Either resolves the
guessing problem without touching introspection shape.

## Suggested issue title

> `UpdateIncidentStatusInput` is declared in `incident.graphql` but never referenced

## Checklist before filing

- [x] Verified against `master`, not a pinned release
- [x] Confirmed zero references across `datahub-graphql-core/src/main/resources/`
- [x] Reproduction is copy-pasteable and needs no running server
- [x] Compatibility impact stated, including the case against the fix
- [ ] Searched existing issues for a duplicate — **do this before filing**
- [ ] Filed
