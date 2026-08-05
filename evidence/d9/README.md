# Security posture, verified against the live system

Three things: a least-privilege service account, a live prompt-injection
demonstration, and the remaining posture requirements — threat model, the
mutation allowlist's entity and scope axes, and a secret scan wired into
`make verify`.

**Result: all three delivered and verified. The verification found that DataHub
was not enforcing authorization at all, which is the most important thing in
this directory.**

## The finding that matters most

`scripts/verify_least_privilege.py` runs nine checks as the service account:
four things DevGuard must be able to do, and **five things it must not**.

First run:

```
  [PASS] ALLOW -> ALLOW  read in-scope dataset
  [PASS] ALLOW -> ALLOW  artifact 1 — raise incident
  [PASS] ALLOW -> ALLOW  artifact 3 — column tag
  [PASS] ALLOW -> ALLOW  artifact 5 — add owner
  [FAIL] DENY  -> ALLOW  delete a scoped dataset
  [FAIL] DENY  -> ALLOW  edit lineage
  [FAIL] DENY  -> ALLOW  write to an OUT-OF-SCOPE dataset
  [FAIL] DENY  -> ALLOW  create a policy (widen its own grants)
  [FAIL] DENY  -> ALLOW  create an ingestion source

ALLOW: 4/4    DENY: 0/5
```

Every ALLOW passed. **Every DENY also passed.** The account could delete
datasets, rewrite lineage, write anywhere in the catalog, and grant itself more
privileges. Nothing errored, and the policies existed and looked correct in the
UI.

**Cause:** the DataHub quickstart ships with
`METADATA_SERVICE_AUTH_ENABLED=false`, under which **Access Policies are not
enforced at all**. Until this check existed, the server-side authorization
control was silently absent and creating policies gave a false sense of
security.

Fixed by setting it to `true` on the GMS container — preserving the token
signing key so existing tokens stay valid — and recreating it:

```
ALLOW: 4/4 behaved as required
DENY : 5/5 correctly refused
```

This is why the deployment guide states enabling it as a hard prerequisite, and
why the client-side mutation allowlist exists as well as the server-side policy.
A control you have not tried to violate is a control you have not verified.

## Prompt injection

`scripts/run_injection_demo.py` plants adversarial instructions in catalog
free-text and shows the Sentinel boundary fencing them before they reach any
prompt. Proof pack: `evidence/proof-pack/security/injection-demo/`.

The detection patterns are in `backend/v2/sentinel.py` and are pinned by
`tests/test_sentinel_fencing.py` and
`tests/test_prompt_injection_boundary.py`.

## Least-privilege checks

`evidence/proof-pack/security/least-privilege/checks/` — one captured payload
per ALLOW case, showing exactly what the account was permitted to do.

## Reproduce

```bash
python scripts/setup_service_account.py
python scripts/verify_least_privilege.py
python scripts/run_injection_demo.py
```
