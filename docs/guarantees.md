# Guarantees and assumptions

## Summary

What Tally guarantees, how, the evidence, and where each guarantee stops. "Guarantee" here means
an invariant the code enforces and the tests attack; it is not a certification, and it holds only
for this simulation's components and assumptions.

| Guarantee | Enforced by | Evidence | Holds unless |
| --- | --- | --- | --- |
| No money created or destroyed: every entry balances per currency, amounts are positive integers | `ledger_post_entry` checks inside PostgreSQL; float-ban on money code | ledger tests, mutation testing, 100k chaos, integrity verifier after every load and chaos run | a database owner/superuser bypasses the functions and triggers |
| History is append-only and tamper-evident | update/delete triggers; SHA-256 chain over entries; continuous verifier and `LedgerInvariantViolation` alert | alert drill (tampering detected in 35 s), restore drill (hash-identical) | a privileged operator rewrites rows *and* the chain consistently; the ledger chain is not externally anchored (the staff audit log is) |
| Balances never go below zero on non-negative accounts, counting active holds | per-account row locks in deterministic order | concurrency tests (200 opposite postings, no deadlock) | — |
| A payment step's effect happens at most once, however often it is retried or replayed | idempotency by key and payload at the API, bank messages and ledger; deterministic keys | idempotency tests, crash-recovery tests, 100k chaos | a caller reuses a key for a genuinely different operation (rejected, not merged) |
| A crash never leaves money moved without a record, or a record without the money | state change + transition log + outbox + ledger command in one transaction; sweeper and replay | crash at every named step boundary; on-cluster pod kills with a money audit | — |
| An unknown external outcome is never guessed: recovery asks, ledger first; after the deadline the bank's configured policy applies and late truth is corrected visibly | recovery worker, per-bank policies, suspense corrections, reconciliation | 100k chaos (1,909 late successes corrected, 154 deemed-success mismatches flagged) | the deemed-success policy is chosen: then a mismatch is possible by design and reconciliation must catch it |
| Refunds and disputes never exceed the payment | row lock on the payment under the merchant money lock | eight concurrent 60% refunds: exactly one succeeds | — |
| Each settlement item is settled once; payouts match the ledger | idempotent settlement by date; netting proved by property tests | money-movement suite | — |
| Every bank-side discrepancy surfaces as a reconciliation break | three-way matching with deterministic break IDs | 100% of 17,280 planted breaks found and classified | a discrepancy is invisible in all three sources |
| Card numbers never reach the merchant or core | browser → vault tokenisation; only the network simulator may detokenise | vault tests, log scrubbing tests | — (only published test cards are accepted at all) |
| One tenant cannot read another's data through the console | RLS on merchant roles, deny-by-default RBAC | tenant-isolation attacks via API and SQL | the core API and workers use the owner role (ADR-017) |
| Money-moving staff actions need a second person | generic maker-checker | bypass attempts (self-approval, wrong role, double approval) | — |
| Merchant requests are authentic and fresh | HMAC over method, path, body hash, timestamp, nonce; nonces consumed once | security tests | the merchant's secret leaks |
| Under overload, admitted payments keep bounded latency | admission control, fast retryable 503 | spike test: p99 16.2 s → 2.1 s | — |

Not guaranteed: real-rail behaviour, alternate-bank failover, throughput beyond ~120 payments/s
on the test hardware, availability targets in a real cloud, regulatory compliance.

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

The local vault accepts only PANs configured on its published-test allowlist that pass Luhn and expiry checks. Each PAN is AES-GCM encrypted under a fresh random data key, and that key is wrapped by a separately configured local KEK. The token is authenticated context for both layers. The database contains ciphertext and BIN/last4/expiry metadata; tokenization responses and validation errors do not echo PAN or CVV, and the API rejects CVV fields. A database audit function restricts detokenization to `network-simulator` and appends hash-chained immutable events. Tests verify these properties and that no other Compose service shares a network with the vault database, whose published port is loopback-only (it joins a second, otherwise empty network because Docker Engine 29 no longer publishes ports for containers only on internal networks).

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

## Crash windows (orchestrator process killed between durable effects)

Every row is exercised by `tests/integration/test_crash_recovery.py` (crash at the named
`services/core/faults.py` point, with the external party's status both visible and hidden until
after the decision deadline) and by the randomized `chaos/flow_sim.py` harness.

| Crash point | Durable state at crash | Recovery | Result |
| --- | --- | --- | --- |
| `confirm.after_authorizing_committed` | `authorizing` + pending command; nothing sent | Stalled sweeper (default 30 s) moves to `pending_unknown`; status is `not_found`; deadline reverses | `reversed`, no ledger effect |
| `card.after_network_approved` | Network approved; no hold | Sweeper → `pending_unknown`; ledger lookup finds no hold; network status `approved` places the stored hold | `authorized`; if status was unavailable until after the deadline, `reversed` and the late approval's hold is placed then voided with an incident |
| `card.after_hold_placed` | Hold exists; core does not know | Ledger lookup-by-key finds the pending hold | `authorized` with that hold |
| `upi.after_psp_approved` | PSP approved; no bank leg sent | Sweeper → `pending_unknown`; bank `not_found` | `reversed`, no ledger effect |
| `upi.after_bank_approved` | Bank moved money; no ledger entry | Bank status `approved` posts the stored command; if status is hidden past the deadline, the payment reverses and the later success is posted to suspense with an incident | `succeeded`, or `reversed` + suspense correction for reconciliation |
| `upi.after_ledger_posted` | Ledger entry committed; core does not know | Ledger lookup-by-key finds the entry before any status decision | `succeeded` (never reversed) |
| `capture.after_capturing_committed` / `capture.after_hold_posted` | `capturing`; hold pending or posted | Sweeper replays idempotent `POST /v1/holds/{id}/post` | `succeeded`, one capture journal |
| `cancel.after_cancelled_committed` | `cancelled`; hold pending | Sweeper replays idempotent hold void | Hold `void` |
| `recovery.after_ledger_call` | Worker died after a ledger call | Lease expires; the next worker repeats the same deterministic key | Same entry/hold returned; single effect |

Two rules make these windows safe: (1) recovery never decides an outcome before asking the
ledger whether the payment's deterministic key already committed (a lookup failure postpones the
decision), and (2) every external and ledger call is replayable with a deterministic key.

Merchant-facing idempotency: a handler that fails with 5xx releases its reservation so the merchant
can retry; 4xx results are stored and replayed; a reservation held by a crashed request expires
after a 60-second lease and the next retry takes it over. Re-running a handler is safe because each
effect below it is guarded by state-machine transitions and deterministic keys.

## Refunds, disputes, settlement and payouts (Phase 8)

| Operation | Ledger effect | Guarantee | Test |
| --- | --- | --- | --- |
| Refund created | Dr payable (covered part) + Dr receivable (shortfall), Cr refunds clearing | Sum of non-failed, non-cancelled refunds plus non-won disputes never exceeds the payment amount (row lock on the payment under the merchant lock). Concurrent refunds serialize; exactly one of eight 60% refunds succeeds. | `test_refunds_settlement_payouts_disputes_and_webhooks` |
| Refund sent to bank | Bank success: Dr refunds clearing, Cr nostro | Each attempt has its own idempotency key; unknown outcomes retry the same key; five declines escalate to `requires_action` | same |
| Refund cancelled by ops | Dr refunds clearing, Cr receivable (up to balance) then payable | Funds return to the merchant; next settlement counts the payable credit once | same |
| Chargeback opened / won / lost | Open: Dr payable/receivable, Cr nostro. Won: reverse. Lost: none | Fraud chargebacks and lost disputes write `risk_labels` | same |
| Settlement | Fee, GST, reserve, recovery, shortfall and payout lines netted per account | Each item settled once; payable after posting equals not-yet-eligible sales; re-running the date returns the same settlement | same |
| Payout | Paid: Dr payout in transit, Cr nostro. Returned: Dr in transit, Cr payable | Returned funds are re-settled once as `payout_return` | same |
| Crash after command commit or ledger post | — | The next merchant operation or the sweeper replays the stored command; exactly one ledger entry per key | `test_money_operation_crashes_replay_to_a_single_ledger_effect` |

The relay publishes outbox rows to Kafka and creates webhook deliveries in one transaction, so
delivery is at least once (a crash after publishing re-publishes; consumers deduplicate on the
event ID). Webhooks are signed with a per-endpoint secret stored AES-GCM encrypted. URLs must be
HTTPS on 443/8443 to a public address; every delivery re-resolves DNS, rejects any non-public
answer, and connects to the vetted IP with the original host in `Host`/SNI (DNS-rebinding
defence); redirects are not followed. Local development can trust explicit hosts through
`TALLY_WEBHOOK_TRUSTED_HOSTS`.

## Verification

The tables above are exercised by: ledger SQL integration tests and mutation testing; gateway,
vault and payment-flow integration tests (card, PSP and bank declines; lost responses; late
authorisation voiding; auto-reversal; late success to suspense; deemed success; credit-leg failure
with a lost reversal); crash-recovery tests at every named step boundary; the money-movement,
reconciliation, risk and security suites; 100,000 full-flow chaos scenarios; and, on a local
Kubernetes cluster, the canary drill, on-cluster chaos with a money audit, load tests with
post-run correctness checks and a backup/PITR drill. Results and hardware are in the
[reports](../README.md#documentation) and [PROGRESS](PROGRESS.md).
