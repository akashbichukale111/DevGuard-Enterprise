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

## Proposed change

Add an admonition to the Access Policies documentation, and a line to the
quickstart page:

> **Access Policies are not enforced unless `METADATA_SERVICE_AUTH_ENABLED=true`.**
> The quickstart sets this to `false`. With it disabled, every request is
> authorised and **policy DENY cases silently pass** — a policy test suite will
> report success whether or not your policy is correct. Enable metadata service
> authentication before testing or relying on Access Policies.

## Why this is worth a docs change rather than a behaviour change

Changing the default would break the quickstart's zero-configuration promise,
which is worth keeping. The failure here is entirely one of *expectation*: the
behaviour is correct and the documentation does not connect two settings a user
has no reason to connect. A four-line admonition is proportionate.

## Suggested issue title

> Docs: Access Policies are silently unenforced under the quickstart's `METADATA_SERVICE_AUTH_ENABLED=false`

## Checklist before filing

- [x] Behaviour confirmed against a running DataHub Core stack
- [x] Concrete failure mode described, with how it was detected
- [x] Proposed wording supplied, so the issue is actionable as a docs PR
- [x] Argued against the heavier fix (changing the default) rather than assuming it
- [ ] Searched existing issues for a duplicate — **do this before filing**
- [ ] Filed
