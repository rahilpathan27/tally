# Tally

Tally is a local simulation of a ledger-backed payments platform. It does not move real money, accept real card data, or claim PCI DSS or RBI certification.

## Status

Phases 1–4 from Section 17 of the master build prompt are implemented and locally verified: foundation, money/ledger, gateway controls, and the isolated test-card vault. Payment orchestration, reconciliation, risk, web interfaces, chaos demos, cloud deployment, and later-phase features are not implemented yet. See [docs/PROGRESS.md](docs/PROGRESS.md) for verified status and gaps.

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
make vault-migrate
make vault-test-integration
```

The local gateway key CLI creates a merchant and prints the generated secret once. Set `TALLY_DATABASE_URL` to the general PostgreSQL database and `TALLY_API_KEY_ENCRYPTION_KEY` to a separately managed base64-encoded 32-byte key before use:

```sh
uv run python -m scripts.manage_gateway_keys create \
  --merchant-id demo-merchant --display-name "Demo Merchant" \
  --scope payments:write --mode test
```

Revoke a key with `uv run python -m scripts.manage_gateway_keys revoke --key-id <key_id>` or replace it atomically with `uv run python -m scripts.manage_gateway_keys rotate --key-id <key_id>`. These commands print a new secret once. Local AES-GCM is for simulation use; production requires managed KMS/envelope encryption.

Run the internal ledger HTTP service in its own terminal with `make ledger-api`. Stop it with Ctrl+C. When finished with the local dependencies, run `make down` in another terminal.

`make up` starts three isolated PostgreSQL instances (general, ledger, vault), Redis, Redpanda, and a local S3-compatible SeaweedFS service. This validates local infrastructure startup only; it does not launch application services.

All published ports bind to loopback. PostgreSQL host ports are 55432 (general), 55433 (ledger), and 55434 (vault). The SeaweedFS S3 endpoint is `http://127.0.0.1:8333` and is unauthenticated in this local simulation. Ledger integration checks run after schema migration.

The ledger API runs on `http://127.0.0.1:8001` via `make ledger-api`. It is an internal development interface with no network authentication yet; keep it on loopback. The vault API runs on `http://127.0.0.1:8002` via `make vault-api`; set `VAULT_DATABASE_URL`, base64 `TALLY_VAULT_KEK_B64`, `TALLY_VAULT_TEST_PANS`, `TALLY_VAULT_TOKENIZE_KEY`, and `TALLY_VAULT_NETWORK_KEY` first. The API accepts only configured published test PANs. Vault detokenization uses a local shared credential; production mTLS and managed KMS are not implemented. The ledger's generated OpenAPI contract is at [contracts/openapi/ledger-v1.json](contracts/openapi/ledger-v1.json).

## Repository guide

- `services/` deployable service boundaries
- `libs/` shared libraries
- `contracts/` API and event contracts
- `docs/` architecture, guarantees, decisions, and progress
- `tests/` unit, property, integration, security, and end-to-end test areas

## Safety

Use synthetic data only. Never put secrets, real cardholder data, or production credentials in this repository.
