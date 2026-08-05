# SigNoz Alert Rules — DevGuard AI

**Status: these three rules SHIP AS APPLIABLE JSON and are verified against a
running SigNoz (v0.135.0).** They live in `signoz/alerts/` and go in with:

```bash
./scripts/apply_signoz_assets.sh      # dashboard + all three rules, then verifies
```

Verified end to end — the assets were deleted from the instance and re-applied
from the committed files:

```
dashboard imported (HTTP 201)
alert rule applied: circuit-breaker-flapping.json (HTTP 201)
alert rule applied: llm-cost-budget.json (HTTP 201)
alert rule applied: llm-error-burst.json (HTTP 201)
count: 3
   devguard-circuit-breaker-flapping      state=inactive  severity=critical
   devguard-llm-cost-budget               state=inactive  severity=warning
   devguard-llm-error-burst               state=inactive  severity=warning
PASSED
```

(`state=inactive` means the rule is loaded and evaluating but not currently
firing — which is correct for a system that is not in an error burst.)

**Every rule targets a metric `backend/core/telemetry.py` actually emits.** That
was not true before: the previous version of this file specified three metrics
(`devguard_slo_compliance_pct`, `devguard_circuit_breaker_state`,
`devguard_llm_cost_usd_total`) that **do not exist anywhere in the codebase**, and
said so only in a soft aside ("assumes one corresponding OTel metric is emitted …
a few lines added to `telemetry.py`, not shown here"). Anyone pasting those in
would have got three alerts that never fire.

## The metrics that actually exist

Read straight out of `backend/core/telemetry.py`. **Note the dots** — SigNoz
stores OTel metric names verbatim, so `devguard_scan_latency` matches nothing:

| metric | type | unit |
|---|---|---|
| `devguard.scan.latency` | histogram | ms |
| `devguard.llm.cost_per_request` | histogram | USD |
| `devguard.llm.tokens_per_sec` | histogram | tokens/s |
| `devguard.llm.tokens_total` | counter | tokens |
| `devguard.llm.exceptions_total` | counter | 1 |
| `devguard.cache.hit_total` | counter | 1 |
| `devguard.cache.miss_total` | counter | 1 |
| `devguard.circuit_breaker.state_changes_total` | counter | 1 |
| `devguard.threats_blocked` | counter | 1 |
| `devguard.llm.cost_saved` | counter | USD |
| `devguard.llm.total_tokens` | counter | tokens |
| `devguard.llm.cost_total` | counter | USD |

**There is no SLO-compliance metric and no circuit-breaker *state* gauge.** Those
two absences change what can honestly be alerted on, and the rules below are
written around them rather than pretending otherwise.

---

## 1. LLM error burst

| | |
|---|---|
| **Name** | `devguard-llm-error-burst` |
| **Metric** | `devguard.llm.exceptions_total` (counter) |
| **Condition** | `increase` over 5m, **above 5**, at least once |
| **Severity** | warning |

Replaces the old "SLO Compliance Degradation" rule. That rule watched
`devguard_slo_compliance_pct`, which does not exist — nothing in the codebase
exports the calculation behind `/slo-status` as a metric. The error counter is
the closest signal that is genuinely emitted, and it catches the same class of
problem (the pipeline is failing repeatedly) without inventing a metric.

**UI:** Alerts → New Alert → Metric based Alert → metric
`devguard.llm.exceptions_total` → *aggregate within time series* `Increase`,
*across* `Sum` → condition `ABOVE` `5` `AT LEAST ONCE` during `Last 5 minutes`.

---

## 2. Circuit breaker flapping

| | |
|---|---|
| **Name** | `devguard-circuit-breaker-flapping` |
| **Metric** | `devguard.circuit_breaker.state_changes_total` (counter) |
| **Condition** | `increase` over 5m, **above 3**, at least once |
| **Severity** | critical |

Replaces the old "Circuit Breaker Stuck Open" rule, and the change is not
cosmetic. That rule needed `devguard_circuit_breaker_state` as a **gauge**
(`0=closed, 1=open, 2=half_open`) so it could assert "held at OPEN for 60
continuous seconds". **No such gauge is emitted.** What exists is a *counter of
transitions*, and a counter cannot express "is currently open" — only "changed
state N times".

So the honest rule is a different one: a breaker that transitions repeatedly is
failing to settle, which is the same incident from the other side. If you want
the original "stuck open" semantics, `telemetry.py` needs an
`observable_gauge` for breaker state first; until it has one, do not write a rule
that claims to detect it.

---

## 3. LLM cost budget

| | |
|---|---|
| **Name** | `devguard-llm-cost-budget` |
| **Metric** | `devguard.llm.cost_total` (counter, USD) |
| **Condition** | `increase` over 30m, **above your budget**, at least once |
| **Severity** | warning |

The only one of the three that survives largely intact. The old file named
`devguard_llm_cost_usd_total`; the real counter is **`devguard.llm.cost_total`**.
Set the threshold to whatever `COST_BUDGET_USD_PER_30MIN` is set to, so the alert
and the router's own conservation logic agree on the number.

Note the caveat carried over from `local_telemetry.py`: recorded cost is
provider-reported where the SDK supplies usage and a chars/4 estimate otherwise,
so this alert inherits that accuracy.

---

## The rule schema, and how it was worked out

Worth recording, because the API is unforgiving and the docs for it are not in
the repo.

**Use `POST /api/v2/rules`, not v1.** The v1 endpoint accepts the request and
then rejects it with a single opaque line — `{"errorType":"bad_data","error":
"alert rule is not valid"}` — with no indication of which field is wrong. Every
builder-style payload, with and without a `version` field, and a `promql_rule`
variant, were all refused that way. **v2 returns field-level errors**, which is
what made this solvable:

```
"errors": [
  {"message": "condition.compositeQuery.queries: must have at least one query"},
  {"message": "notificationSettings: field is required for schemaVersion \"v2alpha1\""}
]
```

The shape itself came from SigNoz's own frontend: the container ships
**source maps** (`/etc/signoz/web/assets/*.js.map`), and
`src/types/api/alerts/alertTypesV2.ts` defines `PostableAlertRuleV2` exactly —
`schemaVersion: "v2alpha1"`, `condition.thresholds.spec[]` as `BasicThreshold`,
and `evaluation.kind` / `evaluation.spec`.

Two things that are easy to get wrong:

* `condition.compositeQuery` takes a **`queries` envelope array**
  (`[{"type":"builder_query","spec":{…}}]`), *not* the dashboard's
  `builder.queryData` shape. The two are not interchangeable.
* `notificationSettings` is **required**. These rules set `usePolicy: true`, which
  routes through SigNoz's routing policies instead of naming a channel. That is
  what lets them apply on a fresh instance with **no notification channel
  configured** — otherwise the UI's own validator (`validateCreateAlertState` in
  `CreateAlertV2/Footer/utils.tsx`) demands at least one channel per threshold,
  which is exactly why **Save Alert Rule stays disabled** on a clean install until
  you add a channel.

## Notifications

The rules apply with no channel, but they cannot page anyone until one exists.
Add it under **Alerts → Notification Channels** (Slack webhook, email, PagerDuty,
webhook), then either attach it to each threshold's `channels: []` or leave
`usePolicy: true` and define a routing policy. Until then a rule still evaluates
and shows its state in the UI — it just notifies nobody.
