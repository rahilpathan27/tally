.PHONY: up down lint test mutation openapi ledger-api ledger-migrate ledger-test-integration gateway-migrate gateway-test-integration gateway-cleanup vault-api vault-migrate vault-test-integration core-api core-migrate core-test-integration bank-sim payer-psp-sim card-network-sim seed simulate train loadtest chaos demo deploy-dev destroy-dev

LEDGER_DB ?= tally_ledger_v1

up:
	docker compose up -d --wait

down:
	docker compose down

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy libs services tests
	uv run python -m libs.money.float_ban

test:
	uv run pytest

mutation:
	uv run mutmut run
	uv run python scripts/check_mutation_score.py

openapi:
	uv run python -m scripts.export_openapi
	uv run python -m scripts.export_core_openapi

ledger-migrate:
	@version=$$(docker compose exec -T postgres-ledger psql -U tally -d $(LEDGER_DB) -tAc "SELECT version FROM ledger_schema_migrations WHERE version = 1" 2>/dev/null || true); \
	if [ "$$version" = "1" ]; then echo "Ledger schema version 1 is already applied"; \
	else docker compose exec -T postgres-ledger psql -U tally -d $(LEDGER_DB) -v ON_ERROR_STOP=1 < services/ledger/migrations/0001_initial.sql; fi
	@version=$$(docker compose exec -T postgres-ledger psql -U tally -d $(LEDGER_DB) -tAc "SELECT version FROM ledger_schema_migrations WHERE version = 2" 2>/dev/null || true); \
	if [ "$$version" = "2" ]; then echo "Ledger schema version 2 is already applied"; \
	else docker compose exec -T postgres-ledger psql -U tally -d $(LEDGER_DB) -v ON_ERROR_STOP=1 < services/ledger/migrations/0002_hold_reservation_per_account.sql; fi

ledger-test-integration: ledger-migrate
	docker compose exec -T postgres-ledger psql -U tally -d $(LEDGER_DB) -v ON_ERROR_STOP=1 < tests/integration/ledger_posting.sql

gateway-migrate:
	@version=$$(docker compose exec -T postgres-general psql -U tally -d tally -tAc "SELECT version FROM gateway_schema_migrations WHERE version = 1" 2>/dev/null || true); \
	if [ "$$version" = "1" ]; then echo "Gateway schema version 1 is already applied"; \
	else docker compose exec -T postgres-general psql -U tally -d tally -v ON_ERROR_STOP=1 < services/api_gateway/migrations/0001_gateway_auth.sql; fi; \
	version=$$(docker compose exec -T postgres-general psql -U tally -d tally -tAc "SELECT version FROM gateway_schema_migrations WHERE version = 2" 2>/dev/null || true); \
	if [ "$$version" = "2" ]; then echo "Gateway schema version 2 is already applied"; \
	else docker compose exec -T postgres-general psql -U tally -d tally -v ON_ERROR_STOP=1 < services/api_gateway/migrations/0002_expired_idempotency_reuse.sql; fi; \
	version=$$(docker compose exec -T postgres-general psql -U tally -d tally -tAc "SELECT version FROM gateway_schema_migrations WHERE version = 3" 2>/dev/null || true); \
	if [ "$$version" = "3" ]; then echo "Gateway schema version 3 is already applied"; \
	else docker compose exec -T postgres-general psql -U tally -d tally -v ON_ERROR_STOP=1 < services/api_gateway/migrations/0003_gateway_state_cleanup.sql; fi; \
	version=$$(docker compose exec -T postgres-general psql -U tally -d tally -tAc "SELECT version FROM gateway_schema_migrations WHERE version = 4" 2>/dev/null || true); \
	if [ "$$version" = "4" ]; then echo "Gateway schema version 4 is already applied"; \
	else docker compose exec -T postgres-general psql -U tally -d tally -v ON_ERROR_STOP=1 < services/api_gateway/migrations/0004_restrict_api_key_reads.sql; fi

gateway-test-integration: gateway-migrate
	docker compose exec -T postgres-general psql -U tally -d tally -v ON_ERROR_STOP=1 < tests/integration/gateway_auth.sql
	GENERAL_DATABASE_URL=postgresql://tally:tally-local-only@127.0.0.1:55432/tally REDIS_URL=redis://127.0.0.1:6379/0 uv run pytest tests/integration/test_gateway_auth_dependency.py tests/integration/test_gateway_route.py tests/integration/test_idempotency_postgres.py tests/integration/test_rate_limit_redis.py

gateway-cleanup:
	docker compose exec -T postgres-general psql -U tally -d tally -v ON_ERROR_STOP=1 -c "SELECT * FROM gateway_cleanup_expired_state(10000)"

vault-migrate:
	@version=$$(docker compose exec -T postgres-vault psql -U tally -d tally_vault -tAc "SELECT version FROM vault_schema_migrations WHERE version = 1" 2>/dev/null || true); \
	if [ "$$version" = "1" ]; then echo "Vault schema version 1 is already applied"; \
	else docker compose exec -T postgres-vault psql -U tally -d tally_vault -v ON_ERROR_STOP=1 < services/vault/migrations/0001_vault.sql; fi

vault-test-integration: vault-migrate
	docker compose exec -T postgres-vault psql -U tally -d tally_vault -v ON_ERROR_STOP=1 < tests/integration/vault_access.sql
	VAULT_DATABASE_URL=postgresql://tally:tally-local-only@127.0.0.1:55434/tally_vault uv run pytest tests/integration/test_vault_api.py tests/integration/test_vault_network.py

core-migrate:
	@version=$$(docker compose exec -T postgres-general psql -U tally -d tally -tAc "SELECT version FROM core_schema_migrations WHERE version = 1" 2>/dev/null || true); \
	if [ "$$version" = "1" ]; then echo "Core schema version 1 is already applied"; \
	else docker compose exec -T postgres-general psql -U tally -d tally -v ON_ERROR_STOP=1 < services/core/migrations/0001_payment_orchestration.sql; fi; \
	version=$$(docker compose exec -T postgres-general psql -U tally -d tally -tAc "SELECT version FROM core_schema_migrations WHERE version = 2" 2>/dev/null || true); \
	if [ "$$version" = "2" ]; then echo "Core schema version 2 is already applied"; \
	else docker compose exec -T postgres-general psql -U tally -d tally -v ON_ERROR_STOP=1 < services/core/migrations/0002_payment_recovery.sql; fi; \
	version=$$(docker compose exec -T postgres-general psql -U tally -d tally -tAc "SELECT version FROM core_schema_migrations WHERE version = 3" 2>/dev/null || true); \
	if [ "$$version" = "3" ]; then echo "Core schema version 3 is already applied"; \
	else docker compose exec -T postgres-general psql -U tally -d tally -v ON_ERROR_STOP=1 < services/core/migrations/0003_late_success_incidents.sql; fi; \
	version=$$(docker compose exec -T postgres-general psql -U tally -d tally -tAc "SELECT version FROM core_schema_migrations WHERE version = 4" 2>/dev/null || true); \
	if [ "$$version" = "4" ]; then echo "Core schema version 4 is already applied"; \
	else docker compose exec -T postgres-general psql -U tally -d tally -v ON_ERROR_STOP=1 < services/core/migrations/0004_bank_recovery_policies.sql; fi; \
	version=$$(docker compose exec -T postgres-general psql -U tally -d tally -tAc "SELECT version FROM core_schema_migrations WHERE version = 5" 2>/dev/null || true); \
	if [ "$$version" = "5" ]; then echo "Core schema version 5 is already applied"; \
	else docker compose exec -T postgres-general psql -U tally -d tally -v ON_ERROR_STOP=1 < services/core/migrations/0005_card_late_success_incidents.sql; fi

core-test-integration: core-migrate gateway-migrate vault-migrate seed
	REDIS_URL=redis://127.0.0.1:6379/0 TALLY_DATABASE_URL=postgresql://tally:tally-local-only@127.0.0.1:55432/tally VAULT_DATABASE_URL=postgresql://tally:tally-local-only@127.0.0.1:55434/tally_vault LEDGER_DATABASE_URL=postgresql://tally:tally-local-only@127.0.0.1:55433/tally_ledger_v1 uv run pytest tests/integration/test_payment_flow.py

core-api:
	TALLY_DATABASE_URL=postgresql://tally:tally-local-only@127.0.0.1:55432/tally REDIS_URL=redis://127.0.0.1:6379/0 TALLY_LEDGER_INTERNAL_KEY=tally-local-ledger-provisioner uv run uvicorn services.core.api:app --host 127.0.0.1 --port 8000

bank-sim:
	uv run uvicorn services.simulators.bank.api:app --host 127.0.0.1 --port 8010

payer-psp-sim:
	uv run uvicorn services.simulators.payer_psp.api:app --host 127.0.0.1 --port 8011

card-network-sim:
	uv run uvicorn services.simulators.network.api:app --host 127.0.0.1 --port 8012

vault-api:
	uv run uvicorn services.vault.api:app --host 127.0.0.1 --port 8002

ledger-api:
	LEDGER_DATABASE_URL=postgresql://tally:tally-local-only@127.0.0.1:55433/$(LEDGER_DB) LEDGER_PROVISIONING_KEY=tally-local-ledger-provisioner uv run uvicorn services.ledger.api:app --host 127.0.0.1 --port 8001

seed: ledger-migrate core-migrate
	docker compose exec -T postgres-ledger psql -U tally -d $(LEDGER_DB) -v ON_ERROR_STOP=1 < services/ledger/seeds/001_local_chart.sql
	docker compose exec -T postgres-general psql -U tally -d tally -v ON_ERROR_STOP=1 < services/core/seeds/001_local_vpas.sql

chaos:
	uv run python -m chaos.chaos_sim

simulate train loadtest demo deploy-dev destroy-dev:
	@echo "$@ is not available yet. See docs/PROGRESS.md for implementation status."
