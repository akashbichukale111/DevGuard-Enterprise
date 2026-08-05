# Deployment guide

How to run DevGuard Enterprise, from a laptop to a hosted deployment.

- [Topologies](#topologies)
- [Local — one command](#local--one-command)
- [Observability: SigNoz](#observability-signoz)
- [The DataHub catalog](#the-datahub-catalog)
- [The data substrate](#the-data-substrate)
- [Backend hosting](#backend-hosting)
- [Frontend hosting](#frontend-hosting)
- [Replay-only deployment](#replay-only-deployment)
- [Configuration reference](#configuration-reference)
- [Pre-flight checklist](#pre-flight-checklist)

---

## Topologies

Pick the smallest one that does what you need.

**A — Replay only.** A static site. No backend, no database, no catalog, no key.
This is enough to explore recorded runs in the Command Center.

**B — Application.** Frontend plus API, with a model provider for scanning.

```
Frontend  ──►  FastAPI backend  ──►  Model provider
                     │
                     ├──►  Redis (cache + scan state)
                     └──►  OTLP collector  ──►  SigNoz
```

**C — Full platform.** Adds the catalog and the data substrate, which is what the
nine-agent loop needs.

```
Frontend  ──►  FastAPI backend  ──►  Model provider
                     │
                     ├──►  DataHub Core  (MCP, read + write)
                     ├──►  PostgreSQL substrate + dbt
                     ├──►  Redis
                     └──►  OTLP collector  ──►  SigNoz
```

Run `make doctor` at any point. It reports exactly what is present, what is
missing, and what each missing piece costs you.

---

## Local — one command

```bash
cp .env.example .env      # add GROQ_API_KEY if you have one
docker compose up
```

Brings up:

| Service | URL |
|---|---|
| Frontend | <http://localhost:3000> |
| Backend | <http://localhost:8000> |
| Redis | internal |

Add the optional observability profile for a local OTLP collector:

```bash
docker compose --profile obs up
```

`GROQ_API_KEY` is deliberately **not** declared with Compose's `:?` form. An
unset key must not abort startup, because the backend is designed to boot
without one — scans then fail at the model call with a clear error, which is the
intended keyless behaviour.

### Without containers

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
make backend        # or: python -m uvicorn backend.main:app --port 8000

cd frontend && npm ci && npm run dev
```

> **Container builds require network access to the Debian and PyPI mirrors.** In
> restricted environments the image build fails on `apt-get` or on wheel
> downloads. That is an environment constraint, not a defect in the Dockerfiles —
> run the services directly if you hit it.

---

## Observability: SigNoz

### Option A — SigNoz Cloud

1. Sign up and open **Settings → Ingestion**.
2. Copy the ingestion endpoint and key.
3. Backend environment:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://ingest.<region>.signoz.cloud:443
OTEL_EXPORTER_OTLP_HEADERS=signoz-access-token=<your-key>
```

4. Set `NEXT_PUBLIC_SIGNOZ_URL` on the frontend so the "Investigate in SigNoz"
   link deep-links correctly. Without it the link does not render — by design,
   rather than falling back to a URL that 404s.

### Option B — Self-hosted

This is the path this repository has verified. Compose files are included, so
there is no second clone:

```bash
# 1. Core services (ClickHouse + Keeper + PostgreSQL + schema migrator + app)
docker compose -f signoz/deploy/docker-compose.yaml up -d signoz-signoz-0

# 2. First-run setup — REQUIRED, and not optional in the way it looks.
#    The OTLP collector fetches its pipeline config from the SigNoz server over
#    OpAMP, and the server will not register it until an organisation exists
#    ("cannot create agent without orgId"). Skip this and the collector comes up
#    "healthy" but never opens 4317/4318 and silently drops every span.
curl -X POST http://localhost:3301/api/v1/register \
  -H 'Content-Type: application/json' \
  -d '{"name":"DevGuard","orgName":"devguard","email":"you@example.com","password":"<choose-one>"}'

# 3. Now start the ingester
docker compose -f signoz/deploy/docker-compose.yaml up -d
```

SigNoz UI at <http://localhost:3301>; OTLP at `localhost:4317` (gRPC) and
`localhost:4318` (HTTP). Then set:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://<host>:4317
```

> **If your shell has an HTTPS proxy configured, also set
> `no_grpc_proxy=localhost,127.0.0.1`.** gRPC reads its own proxy variables and
> `NO_PROXY` alone does not cover it — the backend will report
> `exporter_configured: true`, log nothing, and export nothing.

Verify the whole path end to end, including that spans are genuinely stored:

```bash
./scripts/verify_signoz.sh   # tears down with -v, rebuilds, asserts, non-zero on failure
```

**Pinned versions:** `signoz/signoz:v0.135.0`,
`signoz/signoz-otel-collector:v0.144.6`,
`signoz/signoz-schema-migrator:v0.144.6`,
`clickhouse/clickhouse-server:25.12.5`, `clickhouse/clickhouse-keeper:25.12.5`,
`postgres:16`.

### Dashboard and alerts

```bash
./scripts/apply_signoz_assets.sh
```

Installs `signoz/dashboard.json` and the three rules in `signoz/alerts/`, then
verifies they are loaded. Each rule targets a metric the application genuinely
emits — SigNoz stores OpenTelemetry names verbatim, so a dashboard written with
underscores where the emitter uses dots matches nothing.

The rules evaluate but cannot page anyone until you add a notification channel.

> **Which option to choose:** self-hosting pulls several GB of images and runs
> seven containers. For a laptop demo, Cloud is lighter. Option B is what has
> been verified here and needs no account.

---

## The DataHub catalog

Required only for topology C.

```bash
datahub docker quickstart
```

> **Then enable metadata service authentication.** The quickstart ships with
> `METADATA_SERVICE_AUTH_ENABLED=false`, under which **Access Policies are not
> enforced at all** — every deny rule silently passes. Set it to `true` on the
> GMS container, preserving the token signing key so existing tokens stay valid,
> and recreate the container. See [SECURITY.md](SECURITY.md#least-privilege).

Create the least-privilege service account and verify it:

```bash
python scripts/setup_service_account.py
python scripts/verify_least_privilege.py     # expects ALLOW 4/4, DENY 5/5
```

Store the token in a file outside the repository, `chmod 600`, and point
`DATAHUB_TOKEN_FILE` at it. Tokens are read at runtime and never logged.

Pinned versions are in [`versions.env`](versions.env): DataHub Core `v1.6.0`,
MCP server `0.6.0`, SDK/CLI `1.6.0.16`.

---

## The data substrate

The demonstration data platform — PostgreSQL, dbt models, and a churn model.

```bash
docker compose -f substrate/docker-compose.yml up -d
psql -f substrate/seed/01_raw.sql
cd substrate/dbt && dbt build
python substrate/ml/train_churn_model.py
```

Ingest it into DataHub with the recipes in `recipes/`:

```bash
datahub ingest -c recipes/postgres.yml
datahub ingest -c recipes/dbt.yml           # this is what produces column-level lineage
datahub ingest -c recipes/structured_properties.yaml
```

Put the substrate into the broken demo state with:

```bash
python scripts/reset_demo.py
```

---

## Backend hosting

The image is defined by [`backend/Dockerfile`](backend/Dockerfile). **Build from
the repository root, not from `backend/`** — `requirements.txt` lives at the root
and the application must be imported as the package-qualified
`backend.main:app`:

```bash
docker build -f backend/Dockerfile -t devguard-backend .
```

The image runs as a non-root user and declares a healthcheck against
`/slo-status`.

### Railway

1. **New Project → Deploy from GitHub repo**.
2. Point the service at `backend/Dockerfile` with the repository root as context.
3. Environment:
   ```
   GROQ_API_KEY=<key>
   REDIS_URL=<rediss:// url>
   OTEL_EXPORTER_OTLP_ENDPOINT=<signoz endpoint>
   OTEL_EXPORTER_OTLP_HEADERS=signoz-access-token=<key>   # cloud only
   PORT=8000
   ```
4. Confirm health: `curl https://<your-app>/slo-status`.

### Render

**New → Web Service** → connect the repository → environment **Docker**, same
variables.

> **CORS.** The backend ships with `allow_origins=["*"]`. Restrict it to your
> frontend origin before exposing the API. There is also **no authentication on
> any endpoint** — see [SECURITY.md](SECURITY.md#known-gaps) before deploying
> anywhere public.

---

## Frontend hosting

### Vercel

1. **Add New → Project** → import the repository.
2. Set **Root Directory** to `frontend`.
3. Environment (Production + Preview):
   ```
   NEXT_PUBLIC_API_URL=https://<your-backend>
   NEXT_PUBLIC_SIGNOZ_URL=https://<your-signoz>
   ```

No `.env.local` is committed, so a clean clone never bakes a dead host into the
bundle. CI builds without one, which is exactly the clean-clone case.

---

## Replay-only deployment

The Command Center is a static export and needs no infrastructure at all:

```bash
make replay-build          # bundles + static export into frontend/out/
```

Serve `frontend/out/` from any static host. The entry point is `/command/`.

To verify the deployment does what it claims:

```bash
make verify-replay-ui      # drives the built site in a real browser
```

---

## Configuration reference

See [`.env.example`](.env.example) for the annotated list.

| Variable | Scope | Required | Unset behaviour |
|---|---|---|---|
| `GROQ_API_KEY` | backend | for `POST /scan` | Server runs; scans return a clear error |
| `REDIS_URL` | backend | no | In-process cache |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | backend | no | Defaults to `http://localhost:4317`; no-ops if nothing listens |
| `OTEL_EXPORTER_OTLP_HEADERS` | backend | cloud only | — |
| `SIGNOZ_MCP_URL` | backend | no | Self-observation uses the in-process shadow |
| `DATAHUB_TOKEN_FILE` | backend | topology C | Catalog operations unavailable |
| `AUDIT_LOG_PATH` | backend | no | `data/audit_log.jsonl`, created on first write |
| `COST_BUDGET_USD_PER_30MIN` | backend | no | Built-in default |
| `NEXT_PUBLIC_API_URL` | frontend | yes | Requests fail |
| `NEXT_PUBLIC_SIGNOZ_URL` | frontend | no | The SigNoz link does not render |

---

## Pre-flight checklist

- [ ] `make doctor` reports all required checks passing
- [ ] `curl $API/slo-status` returns 200
- [ ] `curl $API/audit-log/verify` returns `{"valid": true, "entries_checked": N, "broken_at": null, "reason": "chain intact"}`
- [ ] `curl $API/telemetry-status` shows the exporter and MCP path you intended, rather than what you meant to configure
- [ ] A scan completes end to end against the deployed URL
- [ ] A trace for that scan appears in SigNoz
- [ ] `NEXT_PUBLIC_SIGNOZ_URL` is set, so the SigNoz link renders
- [ ] CORS is restricted to your frontend origin
- [ ] `METADATA_SERVICE_AUTH_ENABLED=true`, verified by `scripts/verify_least_privilege.py`
- [ ] `make replay-serve` works, as an offline fallback that needs nothing

### A note on the accuracy strip

In a fresh deployment it reads **"accuracy not measured"**. Figures reach the UI
only from an artifact a real benchmark run wrote:

```bash
python -m backend.core.benchmark --json data/benchmark_report.json   # needs GROQ_API_KEY
```

Point `DEVGUARD_BENCHMARK_ARTIFACT` at that file if you mount it elsewhere. The
harness refuses to write the artifact if any scan errored, so a run during a
provider outage cannot publish its depressed rates as the scanner's accuracy.

---

Related: [Installation](docs/INSTALLATION.md) · [API](docs/API.md) · [Security](SECURITY.md) · [Reproducibility](docs/REPRODUCIBILITY.md)
