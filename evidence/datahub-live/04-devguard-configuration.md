# DevGuard → DataHub: the verified connection configuration

Produced during the live session recorded in this directory. Every value below
was read back from the running server or from the module that consumes it —
nothing here is copied from documentation.

## Preflight against the live instance

```
$ DATAHUB_GMS_URL=http://localhost:8080 \
  DATAHUB_TOKEN_FILE=/etc/devguard/datahub-agent.token \
  python -c "from backend.v2.datahub_preflight import preflight; ..."
OK: GMS reachable and GraphQL authenticated (DataHub v1.7.0)
  [ok] DATAHUB_GMS_URL: http://localhost:8080
  [ok] token: from DATAHUB_TOKEN_FILE (<token file>)
  [ok] /config: HTTP 200
  [ok] /api/graphql: HTTP 200
```

## Resolved endpoints

| What | Value | How it was determined |
|---|---|---|
| GMS | `http://localhost:8080` | `GET /config` → HTTP 200, `versions."acryldata/datahub".version = v1.7.0` |
| GraphQL | `http://localhost:8080/api/graphql` | derived from GMS; `POST {"query":"query { __typename }"}` → HTTP 200 |
| Frontend | `http://localhost:9002` | `GET /` → HTTP 200; `POST /logIn` issued a PLAY_SESSION cookie |
| OpenSearch | `http://localhost:9200` | opensearch 2.19.3, cluster `docker-cluster` |
| Kafka | `localhost:9092` (external), `broker:29092` (in-network) | 9 topics listed, incl. `MetadataChangeProposal_v1` |
| MySQL | `localhost:3306` | MySQL 8.2.0, schema `datahub` |
| MCP server | `uvx mcp-server-datahub@0.6.0` over stdio | `initialize` → server `datahub` **3.4.6**, protocol `2024-11-05` |

> The MCP server reports version **3.4.6** while the PyPI package pinned and
> installed is **0.6.0**. Recorded as observed; the disagreement is the
> server's, and normalising it would hide a fact about the deployment.

## Authentication, as enforced

```
# forged token
Authorization: Bearer not.a.real.token   -> HTTP 401
# no token
(no Authorization header)                -> HTTP 401
# the service-account token
Authorization: Bearer <agent token>       -> {"data":{"me":{"corpUser":{"urn":"urn:li:corpuser:devguard_agent"},"platformPrivileges":{"managePolicies":false,"manageIngestion":false,"manageDomains":false}}},"extensions":{}}
```

This required setting `METADATA_SERVICE_AUTH_ENABLED=true` on the GMS
container. The v1.7.0 quickstart still ships it as `false`, under which
**every request is treated as authorised and Access Policies are not
evaluated at all** — see `02-least-privilege-AUTH-OFF.txt` in this directory
for what that does to a policy test suite, and
[`docs/upstream/02`](../../docs/upstream/02-quickstart-policies-silently-unenforced.md)
for the write-up.
