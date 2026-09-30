# ADR-002: Atomic account locking for ledger posting

## Context

Concurrent ledger operations must not overdraw accounts or deadlock while checking and applying a multi-account journal entry.

## Options

1. Process-local lock in a deterministic reference model.
2. PostgreSQL row locks acquired in globally sorted account-ID order.
3. Serializable transactions with bounded retry.

## Decision

The in-memory reference model uses one reentrant process lock. The PostgreSQL posting function locks participating account rows in sorted account-ID order, then serializes hash-chain append by locking one chain-head row. It commits the journal insert, postings, and cached-balance updates within one transaction.

## Consequences

Sorted account locks avoid cycles during account acquisition. The singleton chain head serializes all postings and may limit throughput; no benchmark has been run. The Python model still provides process-local guarantees only. PostgreSQL owners and superusers can bypass app-role access restrictions.
