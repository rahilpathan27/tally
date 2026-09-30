# Service level objectives

Targets from the master plan. Measured values are published in `docs/load-test-report.md`
(Phase 15) together with the hardware used; until then the "Measured" column lists what has been
measured locally so far.

| SLI | Objective | How it is measured | Alert | Measured so far |
| --- | --- | --- | --- | --- |
| Merchant API availability | 99.9% of `/v1/*` requests non-5xx over 30 days (43 min budget) | `tally_http_requests_total{service="core"}` | `ErrorBudgetBurnFast` (14.4x, 5 m and 1 h), `ErrorBudgetBurnSlow` (6x, 6 h) | — |
| Payment latency (create → authorized/succeeded) | p50 < 300 ms, p99 < 1.5 s including risk | `tally_http_request_duration_seconds` on create + confirm | dashboard only | local dev stack: confirm p50 21 ms, p95 82 ms (analytics panel, 31 seeded payments) |
| Risk decision latency | p99 < 50 ms (hard timeout 100 ms) | `tally_risk_decision_duration_seconds` | `RiskLatencyHigh` | in-process p50 6.3 ms, p99 10.5 ms |
| Unknown-outcome resolution | resolved by the per-bank deadline (default 30 s) | `tally_pending_unknown_oldest_age_seconds` | `PendingUnknownStuck` (> 5 min) | chaos harness: all 10,000 scenarios settle |
| Recovery of crashed payments | resumed within 30 s | sweeper threshold + `tally_recovery_lag_seconds` | `RecoveryWorkerLag` | crash-recovery tests |
| Webhook delivery | 99% delivered within 15 min | `tally_webhook_oldest_pending_seconds`, delivery results | `WebhookBacklog` | — |
| Ledger integrity | zero discrepancies | `tally_ledger_integrity_ok` | `LedgerInvariantViolation` (page) | verifier green in all suites |
| Reconciliation | 100% of planted breaks found | recon evaluation | `ReconBreakSurge` | 17,280 / 17,280 |
| Throughput | 300+ payments/s end to end | load test | — | Phase 15 |
| RPO / RTO | ≤ 5 min / ≤ 1 h | restore drill | — | Phase 15 |
