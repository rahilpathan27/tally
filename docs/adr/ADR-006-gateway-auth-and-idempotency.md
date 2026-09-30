# ADR-006: Gateway authentication, request replay, and idempotency storage

## Context

Merchant requests need authenticated tenant identity, replay rejection, per-merchant rate limits, safe retries, and an audit trail that can be checked for tampering. The design must work across multiple gateway instances.

## Options

- Process-local nonce, idempotency, and rate-limit state: simple but loses protection on restart and does not coordinate replicas.
- Shared PostgreSQL and Redis state: durable request outcomes and tenant context in PostgreSQL; atomic high-volume counters in Redis.

## Decision

Use the HMAC canonical request from ADR-005, resolve active API key records through a security-definer lookup, and decrypt key material with local AES-GCM using a separately managed 32-byte key. Bind ciphertext to its key ID as associated data. The gateway role cannot select the whole key table. Consume `(key_id, nonce)` atomically in PostgreSQL only after verifying the signature. Keep per-merchant API state and idempotency responses in PostgreSQL with row-level tenant policies and transaction-local `app.merchant_id`. Use Redis Lua `INCR` plus expiry for atomic fixed-window limits. Append gateway audit events through a hash-chain function and deny direct update/delete of the audit table.

## Consequences

Nonce uniqueness and idempotency survive process restarts and coordinate replicas. Rate limits are bounded by Redis key expiry and do not claim a globally precise rolling-window limit. The local key-management CLI displays a generated secret once and supports revocation. A production KMS/envelope-encryption provider and atomic key-rotation workflow are still required. Expired rows have a bounded cleanup function and `make gateway-cleanup` command, but scheduling and complete gateway route integration remain outstanding.
