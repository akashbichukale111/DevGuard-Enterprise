# Why every recorded run has `model=null`

The largest limitation this project states — *no recorded run invoked a model* —
has one cause, and it is not the one most people assume.

**Conclusion: the provider is unreachable at the network layer. Credentials are
not involved.**

## The distinguishing test

A valid API key was supplied and tested. The authenticated and unauthenticated
attempts fail **identically**, while an allowlisted host succeeds seconds later
from the same shell.

```
$ curl -H "Authorization: Bearer gsk_..." https://api.groq.com/openai/v1/models
curl: (56) CONNECT tunnel failed, response 403

$ curl https://api.groq.com/openai/v1/models          # no credentials at all
curl: (56) CONNECT tunnel failed, response 403

$ curl https://pypi.org/simple/                        # allowlisted host
200
```

## Why this proves it is not authentication

`CONNECT` is the first thing a client sends a proxy, before any TLS handshake.
The tunnel is refused, so no TLS session is established, no HTTP request is
sent, and **the `Authorization` header is never transmitted**. Groq never sees
the key.

A `401`/`403` *from Groq* would arrive inside an established TLS session as a
response body. A proxy-level refusal to connect is a different layer, and only
one of the two is fixable with a key. The identical failure with and without
credentials is the clincher: if this were authentication, removing the
credential would change the error. It does not.

## The proxy confirms it independently

```
$ curl "$HTTPS_PROXY/__agentproxy/status"
```

```json
{"kind": "connect_rejected",
 "host": "api.groq.com:443",
 "detail": "gateway answered 403 to CONNECT (policy denial or upstream failure)"}
```

`api.groq.com` is absent from the proxy's `noProxy` allowlist. The hosts that
are present — `pypi.org`, `registry.npmjs.org`, `github.com` — all work, which
is why the test suite, dependency installs and every `git push` succeed here
while inference does not.

## What this does and does not invalidate

**Unaffected.** The evidence rule, the structural refusal path, chain
validation, the write-back rules, the governance gate and every test are
independent of model reasoning. Eight of the nine agents make no model call by
design.

**Affected.** The Diagnostician reports `REASONER_UNAVAILABLE` without an
injected `Reasoner`. Every handoff carries `model=null, tokens=0`, and token,
latency and cost render `N/A` with the reason attached rather than a plausible
zero.

The *quality of model reasoning* is unproven. That gap is real and is stated
wherever the runs are presented.

## What would resolve it

One run from any environment with egress to an inference endpoint. The code
path exists and is exercised by tests with an injected reasoner; only the
network is missing.

## Reproducing this

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://api.groq.com/openai/v1/models
curl -sS "$HTTPS_PROXY/__agentproxy/status" | jq '.recentRelayFailures'
```

Recorded 2026-08-07. No credential appears here or anywhere in the repository.
