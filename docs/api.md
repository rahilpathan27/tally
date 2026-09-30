# Ledger API (internal development interface)

The current ledger API is implemented in `services/ledger/api.py`; its OpenAPI contract is generated to `contracts/openapi/ledger-v1.json` with `make openapi`. CI verifies that the checked-in contract matches the code.

Endpoints:

- `POST /v1/entries` posts a balanced journal entry through the database transaction function.
- `POST /v1/holds` creates a funds hold.
- `POST /v1/holds/{hold_id}/post` posts a hold as an immutable journal entry.
- `POST /v1/holds/{hold_id}/void` releases a pending hold.
- `GET /v1/accounts/{account_id}/balance` reads posted, pending, available, currency, and version fields.
- `GET /v1/integrity` runs the database integrity verifier.
- `GET /health/live` and `GET /health/ready` provide process and database health checks.

Amounts are JSON integers in minor units, validated as strict positive integers and bounded to JavaScript's safe-integer maximum. Account currency is read from the ledger chart. The service returns machine-readable error codes and never returns raw PostgreSQL errors.

The ledger API is unauthenticated and intended only for loopback development. The local launcher connects using the Compose superuser. Do not expose it to a network or treat it as the merchant-facing gateway. The partial merchant HMAC authentication and idempotency foundations live under `services/api_gateway/` and are not wired into these ledger routes.
