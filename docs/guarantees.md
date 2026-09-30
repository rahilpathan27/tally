# Guarantees and assumptions

## PostgreSQL ledger

Calls through `ledger_post_entry`, `ledger_place_hold`, `ledger_post_hold`, and `ledger_void_hold` commit atomically: related journal/hold changes and cached balances all commit, or none do. A committed posting has at least two positive integer minor-unit lines, balances in one currency, checks account currency and closed state, and rejects a non-negative account becoming negative after active holds. A repeated idempotency key and JSON payload returns the original result; changed JSON is rejected.

These guarantees apply when application requests use `tally_ledger_app` and the stored functions. The role has read-only table access and function execution rights. PostgreSQL owners and superusers can bypass these controls. The development API currently connects as the Compose superuser for local use, is bound to loopback, and has no request authentication. It is not the production security setup.

The journal and postings reject `UPDATE` and `DELETE` through triggers, and direct app-role writes are denied. Corrections require new reversing entries. SHA-256 hashes link entries by sequence; a singleton head row serializes appends. The chain is not externally anchored and cannot prevent rewriting by a sufficiently privileged database operator.

## Python reference ledger

`LedgerBook` enforces posting and hold invariants atomically inside one process. It is not durable; a process crash loses its entries and chain. It provides no cross-process locking.

## Cross-service state

The merchant payment API uses the Phase 3 gateway route wrapper. HMAC verification binds method, raw path/query, body digest, timestamp, and nonce. Once a key is found and its ciphertext is decrypted by the configured provider, successful signatures consume `(key_id, nonce)` in PostgreSQL; concurrent reuse is rejected by the primary key. The Redis rate limiter uses atomic fixed-window counters, not a rolling-window algorithm. Idempotency records are merchant-keyed and RLS-scoped, and mutation routes replay completed JSON responses. These controls apply to payment intent create/confirm/capture/cancel routes; they are not yet a guarantee for payment operations that do not exist.

RLS policies read transaction-local `app.merchant_id`, which the application sets from its authenticated principal. This is defense-in-depth against accidentally unscoped queries; the shared application database role can set custom PostgreSQL variables, so it is not a boundary against a compromised service or arbitrary SQL execution. Local API key ciphertext uses AES-GCM with a separately supplied 32-byte master key and key ID as authenticated associated data; this is local simulation encryption, not envelope encryption or managed KMS. Key provisioning/rotation endpoints and scheduled expired-record cleanup are also outstanding; a bounded cleanup function and manual `make gateway-cleanup` command exist.

## Card vault

The local vault accepts only PANs configured on its published-test allowlist that pass Luhn and expiry checks. Each PAN is AES-GCM encrypted under a fresh random data key, and that key is wrapped by a separately configured local KEK. The token is authenticated context for both layers. The database contains ciphertext and BIN/last4/expiry metadata; tokenization responses and validation errors do not echo PAN or CVV, and the API rejects CVV fields. A database audit function restricts detokenization to `network-simulator` and appends hash-chained immutable events. Tests verify these properties and that the vault database is attached only to an internal Compose network with a loopback-only published port.

This is a local simulation boundary. A static shared credential stands in for simulator mTLS, and the local KEK stands in for managed KMS. Compose networking and process-local access controls are not a production network security claim. Do not use real PAN or CVV.

The Phase 5 orchestrator and transactional outbox are implemented, but no recovery worker exists yet. Crash windows are described below and require the Phase 6 recovery and failure-matrix work.

## Payment orchestration (Phase 5)

The payment API authenticates merchant mutations through the Phase 3 HMAC route controls. Payment intents store amount in integer minor units, currency, opaque card tokens or UPI VPAs, and their current state. A separate immutable transition row records both accepted changes and rejected attempts. In one PostgreSQL transaction, an accepted state change updates the current-state projection, appends transition history and an outbox event, and persists any required ledger command. The API never edits ledger balances.

The verified card flow is `created → authorizing → authorized → capturing → succeeded`; authorization places a ledger hold, capture posts it, and cancellation voids an authorized hold. The verified UPI flow resolves registered payer/payee VPAs, receives payer PSP approval, obtains approval from both simulated banks, then submits a deterministic ledger posting. The simulators support decline, timeout, and outage controls; bank and PSP messages carry deterministic idempotency keys. State transition logs and outbox payloads contain no PAN. The core hashes caller-supplied request IDs before storing them.

| External result | Current Phase 5 behavior | Ledger effect |
| --- | --- | --- |
| Card network approves | `authorized`; merchant may capture or cancel | Place hold, then post on capture or void on cancel |
| Card network declines | `failed` | No hold |
| UPI payer PSP declines | `failed` | No posting |
| Either UPI bank declines | `failed` | No posting |
| Both UPI banks approve | `succeeded` | One idempotent transfer posting |
| Simulator or ledger request times out / returns an error | Payment remains at its last persisted in-flight state with a pending command | A deterministic ledger key permits replay; no worker resolves the command yet |

There is no distributed transaction across the general database, ledger, vault, or simulators. The durable command is written before an external ledger call. If the process stops after the ledger commits but before the core stores the response, the deterministic ledger key or immutable hold ID allows the request to be replayed without creating another entry. If a bank or PSP response is lost, the current service does not check transaction status; recovery and deemed outcomes are Phase 6. Simulator idempotency caches are in memory and do not survive simulator restarts. Outbox events remain stored but are not yet published to Redpanda.

## Verification

The versioned ledger, gateway, vault, and core migrations pass against the local PostgreSQL 16 Compose services. Gateway, vault, and payment-flow integration tests run against local PostgreSQL, Redis, and simulator apps. The full Python quality checks, unit suite, OpenAPI drift checks, ledger integrity verifier, card capture/void flows, and UPI happy path pass. No throughput, unknown-outcome recovery, chaos, or production payment claim is made.
