# Highlights

## Design choices worth knowing

| Choice | Why | Where |
| --- | --- | --- |
| Integer minor units, Decimal half-even, largest-remainder allocation; floats banned by an AST check | paise must never be created or lost by rounding | [ADR-001](adr/ADR-001-money.md) |
| Ledger logic in PostgreSQL stored functions; the app role can only call them | invariants hold no matter which service, script or bug calls the database | [ADR-004](adr/ADR-004-ledger-api.md), [ledger design](ledger-design.md) |
| Idempotency by key **and** payload hash, at every layer (merchant API, bank messages, ledger) | a retry is always safe; a reused key with different content is an error, not a silent replay | [ADR-006](adr/ADR-006-gateway-auth-and-idempotency.md) |
| State change, transition log, outbox event and ledger command in one transaction; external calls after | a crash can never leave money moved without a record of why, or vice versa | [ADR-008](adr/ADR-008-payment-orchestrator.md) |
| Unknown outcomes resolved by asking, never by guessing; recovery checks the ledger first | the classic double-reversal bug cannot happen | [ADR-010](adr/ADR-010-unknown-outcome-recovery.md), [ADR-011](adr/ADR-011-crash-recovery-and-flow-chaos.md) |
| Late truth is corrected with new entries to suspense, flagged for reconciliation | history is never edited, and every correction is visible | [guarantees](guarantees.md) |
| Separate databases for the ledger and vault; RLS for tenants; maker-checker for money-moving staff actions | blast radius and the four-eyes principle | [ADR-016](adr/ADR-016-backoffice-auth.md), [ADR-017](adr/ADR-017-tenant-isolation-roles.md) |
| Canary releases gated on the ledger's own integrity metric | a release that corrupts money is rolled back automatically | [ADR-019](adr/ADR-019-deployment.md) |
| Admission control with fast, retryable 503s | under overload, latency stays bounded instead of every client timing out | [load test](load-test-report.md) |

## Defects found by testing (all fixed)

The value of the test harnesses is what they caught. In rough order of how much they would have
hurt:

| Defect | Would have caused | Found by |
| --- | --- | --- |
| Recovery reversed a UPI payment whose ledger credit had already committed (crash between ledger commit and state update) | merchant paid, customer refunded: money created | crash-recovery tests; fixed with ledger-first recovery |
| A transient database error at start-up left the ledger integrity gauge at 0 forever | a permanent false "ledger invariant violated" page, and every canary aborted | Kubernetes canary drill |
| The canary error-ratio query returned no data when there were no errors | healthy releases could never promote automatically | Kubernetes canary drill |
| Refresh-token reuse detection revoked the token family inside the transaction that then raised and rolled back | a stolen refresh token stays usable | security test suite |
| The card-network simulator interpolated an unvalidated token into the vault URL path | a crafted token reaches other vault endpoints with the network's credential | reviewing a Semgrep finding (Phase 14) |
| One circuit breaker shared by transfers and status checks: healthy status calls closed it | a failing bank keeps receiving transfers | alert drill |
| The fixed-width statement writer silently truncated long references | reconciliation mismatches that look like bank errors | reconciliation evaluation |
| Overloaded core queued every request (p99 16 s) | clients time out after the payment was made, then retry | load test (spike) |
| `httpx` was only a dev dependency | every service crashes on start in a production image | first container build |
| The BFF serialised `numeric` sums as strings | wrong totals in the console | runtime schema validation (Zod) |
| Synthetic fraud data was too easy (PR-AUC 0.996) | a model that looks perfect and learns nothing | model evaluation |

## Measured, not claimed

Every number in the README comes from a run whose report states the hardware and method: 100,000
chaos scenarios, on-cluster chaos with a money audit, 17,280 planted reconciliation breaks,
risk latency and model metrics, load at 60–150 payments/s, a canary drill and a restore drill.
Where a target was not met (300 payments/s), the report says so and shows why.
