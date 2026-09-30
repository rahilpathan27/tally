# Tally

Tally is a local simulation of a ledger-backed payments platform. It does not move real money, accept real card data, or claim PCI DSS or RBI certification.

## Status

The Phase 1 local dependency stack and Phase 2 money/ledger foundation are implemented. Phase 3 gateway authentication and idempotency storage are in progress. Payment orchestration, reconciliation, risk, web interfaces, chaos demos, cloud deployment, and later-phase features are not implemented yet. See [docs/PROGRESS.md](docs/PROGRESS.md) for verified status and gaps.

## Local foundation

Requirements: Docker Compose v2 and Python 3.12 with `uv` for Python tooling.

```sh
make up
make seed
make gateway-migrate
make lint
make test
make mutation
make gateway-test-integration
```

The local gateway key CLI creates a merchant and prints the generated secret once. Set `TALLY_DATABASE_URL` to the general PostgreSQL database and `TALLY_API_KEY_ENCRYPTION_KEY` to a separately managed base64-encoded 32-byte key before use:

```sh
uv run python -m scripts.manage_gateway_keys create \
  --merchant-id demo-merchant --display-name "Demo Merchant" \
  --scope payments:write --mode test
```

Revoke a key with `uv run python -m scripts.manage_gateway_keys revoke --key-id <key_id>`. This local AES-GCM key wrapper is for simulation use; production requires managed KMS/envelope encryption and secure key rotation.

Run the internal ledger HTTP service in its own terminal with `make ledger-api`. Stop it with Ctrl+C. When finished with the local dependencies, run `make down` in another terminal.

`make up` starts three isolated PostgreSQL instances (general, ledger, vault), Redis, Redpanda, and a local S3-compatible SeaweedFS service. This validates local infrastructure startup only; it does not launch application services.

All published ports bind to loopback. PostgreSQL host ports are 55432 (general), 55433 (ledger), and 55434 (vault). The SeaweedFS S3 endpoint is `http://127.0.0.1:8333` and is unauthenticated in this local simulation. Ledger integration checks run after schema migration.

The ledger API runs on `http://127.0.0.1:8001` via `make ledger-api`. It is an internal development interface with no network authentication yet; keep it on loopback. Its generated OpenAPI contract is at [contracts/openapi/ledger-v1.json](contracts/openapi/ledger-v1.json).

## Repository guide

- `services/` deployable service boundaries
- `libs/` shared libraries
- `contracts/` API and event contracts
- `docs/` architecture, guarantees, decisions, and progress
- `tests/` unit, property, integration, security, and end-to-end test areas

## Safety

Use synthetic data only. Never put secrets, real cardholder data, or production credentials in this repository.
