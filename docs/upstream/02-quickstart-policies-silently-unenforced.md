# Quickstart ships with Access Policies silently unenforced

**Target:** `datahub-project/datahub` — documentation
**Type:** documentation / security-relevant default
**Status:** verified behaviour, ready to file
**Files:** quickstart docs, Access Policies guide

---

## Summary

The DataHub quickstart ships with `METADATA_SERVICE_AUTH_ENABLED=false`. Under
that setting **Access Policies are not enforced at all** — every request is
treated as authorised.

That is defensible as a default for a local evaluation stack. The problem is
what it does to anyone *testing* a policy: a DENY case does not fail loudly, it
**silently passes**. Someone who writes a least-privilege policy, tests that
their service account cannot delete an asset, and observes the delete being
refused has learned nothing — the refusal came from somewhere else, or the test
was never exercised. A policy test suite against a quickstart instance returns
green whether the policy is correct, wrong, or absent entirely.

## How this was found

A downstream project built a verifier that asserts a service account's
privileges are both granted *and* denied correctly — four ALLOW cases, five
DENY cases. On its first run against a quickstart-based stack it reported
**9/9 passing**. The DENY half was passing because nothing was enforcing
anything.

The tell was that it passed *immediately*, before the Access Policy had been
correctly scoped. A security control that passes before you have configured it
is not passing.

## Why the current documentation does not prevent this

The flag is documented as an authentication setting. The consequence that
matters here is an **authorisation** one, and it is second-order: auth off
implies no actor identity, which implies policies cannot be evaluated, which
implies every DENY silently succeeds. A reader looking for "how do I test my
policy" has no reason to read the authentication page, and the Access Policies
guide does not say that policies are inert without it.

## Re-confirmed on v1.7.0, and it is worse than "a test passes for the wrong reason"

Re-verified against **DataHub v1.7.0** (commit `7f81ccb`), provisioned from
`datahub docker quickstart`. The default is unchanged:

```
$ grep METADATA_SERVICE_AUTH_ENABLED ~/.datahub/quickstart/docker-compose.yml
      METADATA_SERVICE_AUTH_ENABLED: 'false'
```

The second run surfaced a consequence the original write-up did not reach. A
policy test suite's DENY cases are not read-only observations — they are
**mutations**, and the assertion under test is that the server refuses them. When
nothing evaluates policy, they are not refused. **They execute.**

Running a nine-case least-privilege suite against a stock quickstart reported
`ALLOW 4/4, DENY 0/7` — correctly — and, in the same run, had already:

- soft-deleted the dataset under test (`batchUpdateSoftDeleted`),
- added a lineage edge that created a **cycle** (`updateLineage`),
- attached a glossary term that does not exist, and reassigned the dataset's
  domain,
- created an ingestion source, and
- created a policy granting the test account `MANAGE_POLICIES` — i.e. the suite
  escalated its own privileges as a side effect of testing that it could not.

Every one of those was repaired afterwards, and the run is kept verbatim as
`evidence/datahub-live/02-least-privilege-AUTH-OFF.txt` in the downstream project.

This matters for the documentation because the natural reading of "policies are
not enforced" is *"my test will pass when it should fail"*. The actual exposure is
*"my test will modify production metadata, including granting itself
permissions"* — and a reader who understood the first sentence would still not
have predicted the second.

There is also a reliable way to detect the state from a client, which is worth
documenting because it is not obvious: present a syntactically invalid token. A
server that is enforcing returns `401`; a server that is not resolves it to an
actor and answers the query.

```
# METADATA_SERVICE_AUTH_ENABLED=false
$ curl -sX POST localhost:8080/api/graphql -H 'Authorization: Bearer not.a.real.token' \
    -d '{"query":"query { me { corpUser { urn } } }"}'
{"data":{"me":{"corpUser":{"urn":"urn:li:corpuser:__datahub_system"}}}}

# METADATA_SERVICE_AUTH_ENABLED=true
$ …same request…
HTTP 401
```

## Proposed change

Add an admonition to the Access Policies documentation, and a line to the
quickstart page:

> **Access Policies are not enforced unless `METADATA_SERVICE_AUTH_ENABLED=true`.**
> The quickstart sets this to `false`. With it disabled, every request is
> authorised and **policy DENY cases silently pass** — a policy test suite will
> report success whether or not your policy is correct.
>
> Note that a DENY test case is usually a *mutation*. Because the request is
> authorised rather than refused, **it takes effect**: a suite that verifies an
> account cannot delete an asset will delete the asset. Enable metadata service
> authentication before testing or relying on Access Policies.
>
> To check whether a running instance is enforcing, send a request with a
> deliberately invalid bearer token. An enforcing instance returns `401`; a
> non-enforcing one resolves it to an actor and answers.

## Why this is worth a docs change rather than a behaviour change

Changing the default would break the quickstart's zero-configuration promise,
which is worth keeping. The failure here is entirely one of *expectation*: the
behaviour is correct and the documentation does not connect two settings a user
has no reason to connect. A four-line admonition is proportionate.

## Suggested issue title

> Docs: Access Policies are silently unenforced under the quickstart's `METADATA_SERVICE_AUTH_ENABLED=false`

## Checklist before filing

- [x] Behaviour confirmed against a running DataHub Core stack
- [x] Re-confirmed against **v1.7.0** (commit `7f81ccb`); default unchanged
- [x] Concrete failure mode described, with how it was detected
- [x] The **destructive** consequence documented, with the specific mutations that landed
- [x] A client-side detection method supplied (invalid-token probe)
- [x] Proposed wording supplied, so the issue is actionable as a docs PR
- [x] Argued against the heavier fix (changing the default) rather than assuming it
- [ ] Searched existing issues for a duplicate — **do this before filing**
- [ ] Filed
