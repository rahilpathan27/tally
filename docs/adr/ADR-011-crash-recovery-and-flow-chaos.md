# ADR-011: Crash-window recovery and full-flow failure injection

## Context

A review of Phases 5–7 found three gaps. The recovery worker only scanned `pending_unknown`,
`reversal_pending` and `reversed`, so a process killed after committing `authorizing` or
`capturing` left the payment stuck. A handler that failed with 5xx (or a crashed process) left its
merchant idempotency reservation `in_progress` for 24 hours, blocking retries. The Phase 7 chaos
harness exercised only the in-memory ledger model, not the payment flow through the services.

## Options

1. Distributed transactions across the general and ledger databases.
2. Persist intent before every external effect, make every effect replayable by a deterministic
   key, and let a sweeper resume anything a live request would have finished by now.
3. Keep request-path-only progress and rely on operators for stuck payments.

## Decision

Option 2. A stalled-payment sweeper resumes `authorizing` (to `pending_unknown`, letting external
status decide), `capturing` (replay hold post) and `cancelled` with a pending void (replay void)
once they are older than 30 seconds, well above the 5–8 second outbound timeouts. Before deciding
any unknown outcome, recovery asks the ledger (`GET /v1/entries/by-key`, `/v1/holds/by-key`) whether
the deterministic key already committed; if that lookup fails the decision is postponed. Merchant
idempotency reservations carry a 60-second lease; 5xx releases the reservation and 4xx is stored.

`services/core/faults.py` defines named crash points. Production code calls `fault_point` (a no-op
unless an injector is installed). The harness raises a `BaseException` subclass so no
`except Exception` handler runs, matching a killed process. `chaos/flow_sim.py` drives real
FastAPI services against fresh PostgreSQL databases with simulator outcomes, transport faults
(dropped request, lost response), crash points, duplicate/concurrent requests and hidden-then-
revealed external truth, and checks every payment against simulator truth.

## Consequences

The flow harness found a real bug on its first large run: after a crash between the ledger post and
the core's acknowledgement, with bank status temporarily unavailable, recovery reversed a payment
whose merchant credit was already in the ledger. The ledger-first lookup fixes it. The harness runs
scenarios sequentially (about 12–14 per second locally) because simulator state is process-global;
time is advanced by rewriting the scenario payment's schedule columns rather than sleeping. A live
request slower than the stall threshold can race the sweeper; both paths use state-machine guards
and the same deterministic keys, so the race cannot duplicate money movement.
