# ADR-010: Recover unknown UPI outcomes from status, then apply a bank policy

- Status: Accepted
- Date: 2026-09-30

## Context

A bank may commit a transfer but lose its response, or debit the payer while the beneficiary credit fails. The core and ledger do not share a transaction, so treating a timeout as a decline can lose track of money, while blindly retrying can duplicate it.

## Options

1. Mark every timeout failed and ask an operator to repair it.
2. Retry the original transfer until it returns success.
3. Persist the command, query bank status, apply a snapshotted deemed-outcome policy at its deadline, and retain a late-success watch.

## Decision

Use option 3 for the UPI simulator flow. A recovery lease prevents concurrent workers from claiming the same payment. Status checks back off exponentially to five minutes. Bank and amount-tier policy is stored in PostgreSQL and snapshotted when the payment first enters `pending_unknown`; default policy is `auto_reverse`, with a 30-second status deadline and a 10-minute late-success window. `deemed_success` may be configured for an unknown status and explicitly accepts the risk that status evidence is unavailable.

If status later reports success after a payment was deemed reversed, the core records the observed bank movement as a deterministic ledger entry to suspense, creates an immutable recovery incident, and emits an outbox event. A debit-success/credit-failure response invokes the simulator's idempotent reversal operation; a lost reversal response is resolved by status.

## Consequences

- Recovery commands and deadlines survive core restarts; ledger retries use deterministic keys.
- A late success is visible for reconciliation without silently re-crediting the merchant.
- Simulator status records and the circuit breaker are process-local. Restarting the simulator loses evidence, and the breaker resets on core restart.
- The same durable status-check and lease approach resumes card-network approvals and places the corresponding ledger hold. Card late-success correction after a deemed reversal, alternate-bank failover, real bank protocols, and exhaustive failure-matrix coverage remain future work.
