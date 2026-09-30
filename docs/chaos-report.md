# Failure-injection report

Two harnesses run under `make chaos`:

1. `chaos/chaos_sim.py` — fast model-based ledger commit/retry scenarios (in-memory ledger).
2. `chaos/flow_sim.py` — end-to-end payment scenarios through the real FastAPI services against
   fresh PostgreSQL databases (general + ledger + vault), added during the Phase 1–7 review.

## Full-flow harness (`chaos/flow_sim.py`)

Each scenario is one card or UPI payment with seeded random faults:

- simulator outcomes: approve, decline, credit-leg failure, lost response, unknown status, outage,
  lost reversal response; payer PSP decline/outage; a bank whose policy is deemed success;
- transport faults on the ledger, bank and card-network hops (request dropped, response lost after
  the service committed);
- a process crash (`SimulatedCrash`, a `BaseException`) at any named step boundary in
  `services/core/faults.py`, including inside the recovery worker;
- concurrent duplicate confirms (same and different idempotency keys) and capture/cancel races;
- external truth hidden until after the decision deadline, then revealed (late success).

Time is advanced by rewriting only the scenario payment's recovery schedule. After every scenario
the checker verifies: terminal state reached; accepted transitions are legal and form one chain;
the ledger effect matches simulator truth (UPI transfer iff bank approved, or suspense correction
for late success; card hold posted iff captured and approved, otherwise void or absent). Every 1,000
scenarios, and at the end, it runs `ledger_verify_integrity()` (per-entry balance, cached balances
vs postings, hash chain), a global per-currency debit = credit query, a non-negative-account check,
and a "one terminal transition per payment" check.

| Run | Seed | Scenarios | Crashes injected | Late successes corrected | Late card approvals voided | Deemed-success mismatches (expected recon breaks) | Result | Time |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| default | 20260930 | 5,000 | 902 | 113 | 68 | 2 | PASS | 359 s |
| alternate | 7 | 5,000 | 779 | 77 | 59 | 6 | PASS (before provisioning cache) | 429 s |

Outcome mix for the default run: card succeeded 1,202 / cancelled 368 / failed 188 / reversed 714;
UPI succeeded 714 / failed 773 / reversed 1,041.

### Bugs found and fixed

1. **Crash windows left payments stuck** (review finding, reproduced by the harness): a process
   killed after committing `authorizing`/`capturing` or `cancelled` with a pending void was never
   resumed. Fixed with the stalled-payment sweeper (ADR-011).
2. **Reversal of a payment whose ledger credit had already committed**: bank approved, ledger
   posted, process crashed before recording the result, and bank status was unavailable until after
   the deadline; recovery reversed the payment although the merchant had been credited (scenario
   1211, seed 20260930). Fixed by the ledger-first lookup: recovery never decides an outcome before
   `GET /v1/entries/by-key` or `/v1/holds/by-key` answers.
3. **Merchant retries after 5xx were locked out** for 24 h by an `in_progress` idempotency record.
   Fixed with release-on-5xx and a 60-second reservation lease.
4. **Harness finding, not a product bug**: re-provisioning ledger accounts on every create meant
   injected ledger faults mostly hit provisioning. The core now caches provisioned merchants.

Reproduce a failing scenario with `uv run python -m chaos.flow_sim --seed <seed> --only <index>`.

The target of 100,000 full-flow scenarios is not yet met: sequential runs take about 70 s per
1,000 scenarios locally. The model-based harness below covers 100,000 ledger scenarios.

## Initial Phase 7 run (model-based ledger harness)

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
