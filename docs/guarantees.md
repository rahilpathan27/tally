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

The Phase 5 orchestrator and transactional outbox are implemented. Phase 6 adds a database-backed recovery loop for card-network authorization and the UPI simulator path. Crash windows and the simulator assumptions are described below.

## Payment orchestration (Phase 5)

The payment API authenticates merchant mutations through the Phase 3 HMAC route controls. Payment intents store amount in integer minor units, currency, opaque card tokens or UPI VPAs, and their current state. A separate immutable transition row records both accepted changes and rejected attempts. In one PostgreSQL transaction, an accepted state change updates the current-state projection, appends transition history and an outbox event, and persists any required ledger command. The API never edits ledger balances.

The verified card flow is `created → authorizing → authorized → capturing → succeeded`; authorization places a ledger hold, capture posts it, and cancellation voids an authorized hold. The verified UPI flow resolves registered payer/payee VPAs, receives payer PSP approval, obtains approval from both simulated banks, then submits a deterministic ledger posting. The simulators support decline, timeout, and outage controls; bank and PSP messages carry deterministic idempotency keys. State transition logs and outbox payloads contain no PAN. The core hashes caller-supplied request IDs before storing them.

| External result | Phase 6 behavior | Ledger effect |
| --- | --- | --- |
| Card network approves | `authorized`; merchant may capture or cancel | Place hold, then post on capture or void on cancel |
| Card network declines | `failed` | No hold |
| Card authorization response is lost | Worker checks network status; on approval it places the stored deterministic hold and returns to `authorized` | A retry reuses the same hold idempotency key |
| Card authorization is reported after the payment was deemed reversed | Worker creates the hold, immediately voids it, and records a recovery incident | The hold lifecycle is recorded as void; no capture journal is posted |
| UPI payer PSP declines | `failed` | No posting |
| Either UPI bank declines | `failed` | No posting |
| Both UPI banks approve | `succeeded` | One idempotent transfer posting |
| Bank response is lost after approval | Payment enters `pending_unknown`; a leased recovery worker checks bank status with exponential backoff and posts using the stored deterministic command when approved | Repeated ledger requests return the original journal entry |
| Bank status is `not_found` after the captured deadline | Payment advances through `reversal_pending` to `reversed`; the command is not posted | No ledger entry, because the simulator reports no debit |
| Bank status stays unknown at deadline | Per-bank, amount-tier policy applies `auto_reverse` or `deemed_success`; default is `auto_reverse` | Either no posting, or the same deterministic UPI transfer posting under the opted-in deemed-success policy |
| Debit succeeds, credit fails | Bank simulator reverses the debit. If its response is lost, recovery checks status and completes the payment as failed once reversal is confirmed | No payment entry is posted for a confirmed reversal |
| Bank reports success during the configured late-success window after reversal | Recovery posts the bank movement to `platform:suspense:INR`, writes an immutable recovery incident and outbox event, and keeps the payment reversed for reconciliation | A deterministic correction entry records the observed bank movement without re-crediting the merchant |
| Bank or ledger status check is unavailable | Circuit breaker limits calls; leased command remains eligible for retry after exponential delay | No new ledger effect until a response is resolved |

There is no distributed transaction across the general database, ledger, vault, or simulators. The durable command is written before an external ledger call. If the process stops after the ledger commits but before the core stores the response, the deterministic ledger key permits replay without creating another entry. Recovery leases expire after 30 seconds if a worker crashes. Status retries use exponential delays capped at five minutes; the worker polls every second. The default UPI policy checks status for 30 seconds and watches for late success for 10 minutes. Both windows and the deemed outcome can be configured per remitter bank and amount tier in `core_bank_recovery_policies`. Card unknown outcomes use the same recovery worker and their configured decision window. A deemed-success policy accepts the risk that external status remains unknown at the deadline. Simulator transaction state and idempotency caches are process-local, so simulator restarts can lose status evidence. Circuit breakers are process-local and protect the bank and card simulator endpoints. UPI routing uses the registered payer and payee VPAs to choose the remitter and beneficiary banks; failover to a different bank is intentionally not attempted because it would change the payment route. Outbox events remain stored but are not yet published to Redpanda.

## Verification

The versioned ledger, gateway, vault, and core migrations pass against the local PostgreSQL 16 Compose services. Gateway, vault, and payment-flow integration tests run against local PostgreSQL, Redis, and simulator apps. `test_card_and_upi_happy_paths_and_illegal_transition_are_audited` covers direct card, payer PSP and bank declines; card response loss followed by hold recovery; late card authorization voiding; UPI lost response followed by success; auto-reversal after status deadline; late success corrected to suspense; configured deemed success; and debit-success/credit-failure with a lost reversal response. Unit tests cover simulator outage modes, idempotency, and circuit-breaker behavior. These checks cover the implemented local failure matrix; they do not claim alternate-bank failover, throughput, chaos safety, real-rail behavior, or production payment guarantees.
