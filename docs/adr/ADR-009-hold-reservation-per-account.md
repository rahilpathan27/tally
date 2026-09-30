# ADR-009: Validate each hold against available account balance

- Status: Accepted
- Date: 2026-09-30

## Context

The ledger integration check exposed an older applied `ledger_place_hold` implementation that aggregated active holds. A pending incoming hold could therefore offset a separate outgoing reservation, allowing the outgoing hold to exceed the balance available when it was requested. Updating migration 0001 alone would not fix databases where that migration was already recorded as applied.

## Decision

Keep migration 0001 immutable and add a forward-only migration that replaces `ledger_place_hold` with the per-hold reservation rule. Each outgoing hold must be covered by the account balance minus existing active outgoing reservations; pending incoming holds do not increase available funds. The migration runner applies the new version idempotently after version 1.

## Consequences

- Existing local databases receive the corrected function without destructive schema changes.
- New installations apply both migrations and end with the same function definition.
- The integration scenario verifies that a pending incoming hold cannot make an oversized outgoing hold pass.
