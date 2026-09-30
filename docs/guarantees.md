# Guarantees and assumptions

## PostgreSQL ledger

Calls through `ledger_post_entry`, `ledger_place_hold`, `ledger_post_hold`, and `ledger_void_hold` commit atomically: related journal/hold changes and cached balances all commit, or none do. A committed posting has at least two positive integer minor-unit lines, balances in one currency, checks account currency and closed state, and rejects a non-negative account becoming negative after active holds. A repeated idempotency key and JSON payload returns the original result; changed JSON is rejected.

These guarantees apply when application requests use `tally_ledger_app` and the stored functions. The role has read-only table access and function execution rights. PostgreSQL owners and superusers can bypass these controls. The development API currently connects as the Compose superuser for local use, is bound to loopback, and has no request authentication. It is not the production security setup.

The journal and postings reject `UPDATE` and `DELETE` through triggers, and direct app-role writes are denied. Corrections require new reversing entries. SHA-256 hashes link entries by sequence; a singleton head row serializes appends. The chain is not externally anchored and cannot prevent rewriting by a sufficiently privileged database operator.

## Python reference ledger

`LedgerBook` enforces posting and hold invariants atomically inside one process. It is not durable; a process crash loses its entries and chain. It provides no cross-process locking.

## Cross-service state

The gateway now has a partial authentication and persistence foundation, but it is not yet wired to merchant payment routes. HMAC verification binds method, raw path/query, body digest, timestamp, and nonce. Once a key is found and its ciphertext is decrypted by the configured provider, successful signatures consume `(key_id, nonce)` in PostgreSQL; concurrent reuse is rejected by the primary key. The Redis rate limiter uses atomic fixed-window counters, not a rolling-window algorithm. Idempotency records are merchant-keyed and RLS-scoped; final-response replay is available through the store API. These controls are not yet an end-to-end merchant API guarantee because no public gateway route composes them.

RLS policies read transaction-local `app.merchant_id`, which the application sets from its authenticated principal. This is defense-in-depth against accidentally unscoped queries; the shared application database role can set custom PostgreSQL variables, so it is not a boundary against a compromised service or arbitrary SQL execution. Local API key ciphertext uses AES-GCM with a separately supplied 32-byte master key and key ID as authenticated associated data; this is local simulation encryption, not envelope encryption or managed KMS. Key provisioning/rotation endpoints and scheduled expired-record cleanup are also outstanding; a bounded cleanup function and manual `make gateway-cleanup` command exist.

There is still no payment orchestrator, outbox, or recovery worker. No guarantee is claimed for crash windows between payment state and a ledger call. Those require later phases and failure-injection verification.

## Verification

The versioned ledger and gateway migrations and their PostgreSQL integration assertions pass against the local PostgreSQL 16 Compose services. Redis rate-limit integration, Python quality checks, and the available test suite pass. No throughput, crash-recovery, chaos, or end-to-end payment claim is made.
