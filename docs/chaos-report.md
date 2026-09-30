# Failure-injection report

## Initial Phase 7 run

- Result: **PASS**, 100,000 of 100,000 seeded scenarios completed with no invariant violations.
- Command: `make chaos` (`uv run python -m chaos.chaos_sim`).
- Seed: `7310026`; runtime on the local development machine: approximately 3 seconds.
- Posted journal entries created by scenarios: 49,983, excluding each scenario's opening funding entry.

| Injected condition | Scenarios |
| --- | ---: |
| Dispatch dropped before ledger commit | 16,544 |
| Commit acknowledgement lost, then idempotent replay | 16,598 |
| Duplicate command delivery | 16,646 |
| Hold placement replay followed by repeated void | 33,473 |
| Hold placement replay followed by capture replay | 16,739 |

The scenario runner builds a fresh deterministic in-memory ledger for each case. The independent invariant checker validates the journal hash chain, per-currency debit/credit totals, hold-to-journal references, posted balances, and available balances after every scenario. The harness stops on the first failed idempotency expectation or invariant and reports the scenario index.

## Scope and next work

This is reproducible model-based failure injection against the ledger and payment-command commit/retry semantics. It does not terminate PostgreSQL or service processes, inject faults across real network boundaries, or claim production distributed-systems chaos coverage. The Phase 1–6 database-backed gateway, vault, ledger, and payment integration suites run separately as part of the verification below. Add service-level process and network fault injection as the platform gains durable external simulators and operational observability.

## Regression verification

The full default suite passed (80 passed, 7 database/Redis tests skipped because the default run does not set integration-service URLs). Dedicated ledger, gateway, vault, and core payment integration targets all passed against the local Compose services. Lint, strict typing, the float-ban check, OpenAPI drift checks, and the mutation score gate passed (76.0%).
