# ADR-008: Modular payment orchestrator and simulator boundaries

## Status

Accepted for the local simulation.

## Context

Merchant payment state must remain auditable while money is posted through the independent ledger. Calling simulators or the ledger inside a database transaction would create a distributed transaction that PostgreSQL cannot make atomic. The platform also needs first-class configurable bank, payer PSP, and card-network simulators.

## Decision

- Keep payment orchestration in the modular core, with the ledger, vault, and simulators behind HTTP interfaces.
- Store payment intent state, immutable transition history, a transactional outbox event, and any required ledger command together in the general PostgreSQL transaction.
- Use a table-driven state machine. Log rejected transitions as immutable rows while leaving current payment state unchanged.
- Use deterministic ledger keys derived from payment ID, rail/leg, and operation. Card authorization uses a ledger hold; capture posts that hold and cancellation voids it. UPI posts only after payer PSP and both simulated banks approve.
- Resolve UPI bank IDs from the VPA registry. Use separate loopback HTTP APIs for bank, payer PSP, and card-network simulation, with configurable approve/decline/timeout/outage behavior and idempotency keys.
- Keep PAN access inside vault and network-simulator services. The core sends opaque tokens only, and hashes request IDs before including them in payment history.

## Consequences

The happy path and illegal transition behavior can be verified end to end against PostgreSQL, Redis, the vault, the independent ledger API, and simulator APIs. Database state remains recoverable from transition history, commands, and outbox rows. Cross-service crash windows remain until Phase 6 adds status checks, command recovery, late-outcome reconciliation, and reversals. Outbox publication is not implemented yet; simulator idempotency caches are process-local. UPI messages are simplified examples, not NPCI formats.
