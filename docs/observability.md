# Observability

## Signals

- **Metrics** (`libs/observability/metrics.py`): RED metrics on every service keyed by route
  template (bounded cardinality), plus domain metrics — payment transitions and states, oldest
  `pending_unknown`, recovery lag, bank outcomes, circuit breakers, idempotent replays, ledger
  posting latency/results and integrity checks, recon match rate/open breaks/value, risk decision
  mix/latency/fail-policy use/drift, review queue, webhook and outbox backlogs, returned payouts,
  auth events, rate limiting, SSRF blocks and per-merchant usage. Each service exposes `/metrics`.
- **Traces** (`libs/observability/tracing.py`): OpenTelemetry for inbound FastAPI requests,
  outbound httpx calls and asyncpg queries, exported over OTLP when
  `OTEL_EXPORTER_OTLP_ENDPOINT` is set. `bind(payment_id=..., merchant_id=...)` adds business
  IDs to logs and span attributes.
- **Logs** (`libs/observability/logging.py`): JSON with redaction of card numbers and secrets,
  enriched with `trace_id`/`span_id` and bound business IDs.

## Local stack

`make observability-up` starts Prometheus (:9090), Alertmanager (:9093), Tempo (:3200), Loki
(:3100), an OpenTelemetry Collector (:4318; OTLP traces → Tempo, file logs from `.data/*.log` →
Loki's OTLP endpoint) and Grafana (:3001, anonymous viewer). Promtail could not be pulled in this
environment, so the collector's file-log receiver ships logs instead. Run the dev stack with
`OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 make dev`. Because the dev stack hosts every
service in one process, it has one Prometheus registry and one trace provider (all spans report as
`core`); in Kubernetes each service is its own process and scrape target.

## Dashboards (as code)

`scripts/build_dashboards.py` generates Payments Overview, Switch and Bank Health, Ledger Health,
Reconciliation, Risk and Model Health, Security Events and Per-Merchant Usage into
`infra/observability/grafana/dashboards/`; Grafana provisions them with the Prometheus, Tempo
and Loki data sources (Loki log lines link to Tempo traces by `trace_id`).

## Alerts

`infra/observability/prometheus/rules/tally-alerts.yml` defines 19 rules, each with a
`runbook_url` under `docs/runbooks/`. `promtool test rules` unit-tests the key alerts
(ledger invariant, stuck unknown outcomes, bank success drop, fast error-budget burn, risk
unavailable, refresh-token reuse, circuit breaker) with synthetic series; CI runs it.

## Alert drill (induced failures on the running stack)

`make alert-drill` induces real failures and waits for Prometheus to report the alert firing.
Each drill first waits until its alert is inactive, so timings measure the induced failure.

| Induced failure | Expected alert | Time to firing | Notes |
| --- | --- | ---: | --- |
| Ledger posting altered by a privileged user | LedgerInvariantViolation (page) | 35 s | restored the posting; alert resolved |
| bank-a returns HTTP 500 under live traffic | BankSuccessRateDrop (page) | 160 s | CircuitBreakerOpen fired after 0 s |
| Payment left in pending_unknown (worker stalled) | PendingUnknownStuck (page) | 155 s | reference payment a36c2e35-2298-4604-871f-03b2e50e0939; stuck row removed afterwards |

Timings include the 15 s scrape interval, rule evaluation, and each rule's `for:` window (2 min
for the bank and pending-unknown alerts).

## Findings from building this

- The gauge collector's SQL placed `FILTER` outside the aggregate and failed every cycle; because
  the ledger verifier ran after it in the same loop, integrity checks were silently skipped.
  Worker steps now run independently and failures are logged per step.
- The first bank-outage drill never reached the bank: the risk engine (correctly) held a payer
  making 60 payments a minute for review, which also raised `FraudWave` to pending.
- **The transfer circuit breaker never opened during a bank outage**: the recovery worker's
  status checks shared the breaker, and the bank's healthy status API reset it after every
  failed transfer. Dispatch and status checks now use separate breakers, and
  `CircuitBreakerOpen` alerts on short-circuited calls rather than a gauge that flaps with the
  breaker's 5-second half-open cycle.
