# APIs

Contracts are generated from code with `make openapi` and checked in CI:
`contracts/openapi/merchant-v1.json` (merchant API) and `contracts/openapi/ledger-v1.json`
(internal ledger). Amounts are JSON integers in minor units, bounded by JavaScript's safe-integer
maximum; money is never sent as a JSON float. Errors use `{"detail": {"code", "message"}}`.

## Merchant API (`services/core`, HMAC-signed, idempotent)

Every request carries `x-tally-key-id`, `x-tally-timestamp`, `x-tally-nonce` and
`x-tally-signature` (HMAC-SHA256 over method, raw path+query, body SHA-256, timestamp, nonce).
Mutations require `Idempotency-Key`; 2xx and 4xx responses are replayed for the same key and
payload, 5xx releases the key for retry, and a changed payload returns `422`.
Rate limits (per merchant, default 120 requests/minute) return `429 RATE_LIMITED`. When a core
pod is at its in-flight cap it returns `503 OVERLOADED` with `Retry-After`; retry with the same
idempotency key.

| Method and path | Scope | Purpose |
| --- | --- | --- |
| `POST /v1/payment_intents` | payments:write | Create a card-token or UPI payment intent |
| `POST /v1/payment_intents/{id}/confirm` | payments:write | Authorize (card) or transfer (UPI) |
| `POST /v1/payment_intents/{id}/capture` / `cancel` | payments:write | Capture or void a card authorization |
| `GET /v1/payment_intents/{id}` | payments:read | Current state |
| `POST /v1/refunds` | payments:write | Full (omit amount) or partial refund; over-refund returns `422 REFUND_EXCEEDS_REFUNDABLE` |
| `GET /v1/refunds/{id}`, `GET /v1/payment_intents/{id}/refunds` | payments:read | Refund status |
| `GET /v1/settlements`, `GET /v1/settlements/{id}` | settlements:read | Statements with items and payout |
| `GET /v1/disputes`, `GET /v1/disputes/{id}` | disputes:read | Chargebacks |
| `POST /v1/disputes/{id}/evidence` | disputes:write | Text plus optional base64 document (≤ 1 MB, stored with SHA-256) |
| `POST /v1/webhook_endpoints` | webhooks:write | Register an HTTPS endpoint; the signing secret is returned once |
| `GET /v1/webhook_endpoints`, `.../{id}/deliveries` | webhooks:read | Endpoints and delivery log |
| `POST /v1/webhook_endpoints/{id}/disable`, `/test` | webhooks:write | Disable; send a signed `webhook.test` |
| `POST /v1/webhook_deliveries/{id}/redeliver` | webhooks:write | Queue a delivery again |

Webhooks carry `Tally-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "t.body")>` and
`Tally-Event-Id`. Verify with `libs/security/webhook_signing.verify_signature` (constant-time,
five-minute replay window) and deduplicate on the event ID: delivery is at least once.

## Internal routes (`x-internal-key`, not exposed through the gateway)

`POST /internal/v1/settlements/run`, `POST /internal/v1/disputes` (network simulator opens a
chargeback), `POST /internal/v1/disputes/{id}/resolve`, `POST /internal/v1/refunds/{id}/retry` and
`/cancel` (back-office actions; maker-checker is added in Phase 11), `POST /internal/v1/workers/run`
(one pass of every background worker) and `POST /internal/v1/recovery/run`.

## Ledger API (`services/ledger`, internal)

| Method and path | Purpose |
| --- | --- |
| `POST /v1/entries` | Post a balanced journal entry (idempotent by key and payload) |
| `POST /v1/holds`, `/v1/holds/{id}/post`, `/v1/holds/{id}/void` | Two-phase holds |
| `GET /v1/entries/by-key`, `GET /v1/holds/by-key` | Did a deterministic key commit? (used by recovery) |
| `GET /v1/entries/{id}` | Entry drill-down with hashes |
| `GET /v1/accounts`, `GET /v1/accounts/{id}/balance`, `/statement` | Chart, balances, keyset-paginated statement |
| `GET /v1/trial-balance?as_of=` | Trial balance; fee-income shards roll up to their parent |
| `GET /v1/integrity` | Per-entry balance, cached balance, and hash-chain checks |
| `POST /internal/v1/integrity-report` | Daily report: checks, trial balance, snapshot and recomputation |
| `POST /internal/v1/merchant-accounts` | Provision merchant payable, reserve, payout-in-transit and receivable |

The ledger API is unauthenticated apart from the internal provisioning/report key and is intended
only for loopback development; it connects as the Compose superuser locally.
