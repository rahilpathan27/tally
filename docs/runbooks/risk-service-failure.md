# Runbook: risk service failure (`RiskServiceUnavailable`, `RiskLatencyHigh`)

**Severity:** page (unavailable), ticket (latency).

1. `tally_risk_unavailable_total{mode}`: fail-open merchants are accepting unscreened payments;
   fail-closed merchants are failing payments. Decide with the business whether to switch
   high-risk merchants to fail-closed (`/bff/v1/ops/risk-policies`, maker-checker).
2. Latency: Risk and Model Health dashboard. Common causes: Redis latency (online features),
   PostgreSQL decision-log writes, a new model version (roll back by promoting the previous
   champion through maker-checker).
3. Recover: restart risk pods; decisions are idempotent per payment.
4. Afterwards: review payments approved by fail-open during the window (`payment_intents.risk_outcome->>'source' = 'fail_open'`).
