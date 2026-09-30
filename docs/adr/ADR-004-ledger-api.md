# ADR-004: Internal HTTP boundary for ledger operations

## Context

The independent ledger database must not be called directly by payment and back-office services. It needs a narrow operation surface that preserves database transaction and idempotency rules.

## Options

1. Let callers connect directly to PostgreSQL.
2. Expose a ledger service that maps typed HTTP requests to permissioned posting functions.

## Decision

Choose option 2. The FastAPI ledger service validates integer minor units and structured postings, calls the PostgreSQL functions, exposes balance and integrity reads, and publishes a generated OpenAPI contract. It does not perform money mutations outside database transactions.

## Consequences

The service gives callers one controlled boundary and keeps the database transaction responsible for concurrency and idempotency. The local launcher still uses an unauthenticated loopback connection and a development superuser. Production authentication, service identity, account provisioning, and network policies remain unimplemented.
