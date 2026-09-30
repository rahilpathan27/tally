# Runbook: error budget burn (`ErrorBudgetBurnFast`, `ErrorBudgetBurnSlow`)

SLO: 99.9% of merchant API requests succeed (non-5xx) over 30 days (`docs/slo.md`).

1. Payments Overview dashboard: which route and status code? Correlate with deploys (canary
   analysis should have rolled back; check Argo Rollouts).
2. Dependency failures show as 503 with codes such as `LEDGER_UNAVAILABLE` or
   `MERCHANT_OPERATION_IN_PROGRESS`; follow the matching runbook.
3. Freeze non-urgent deploys while the fast-burn alert is active.
