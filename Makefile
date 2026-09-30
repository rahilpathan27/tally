.PHONY: dev web web-check e2e backoffice-api train retrain risk-api recon-api recon-eval migrate stack-test-integration up down lint test mutation openapi ledger-api ledger-migrate ledger-test-integration gateway-migrate gateway-test-integration gateway-cleanup vault-api vault-migrate vault-test-integration core-api core-migrate core-test-integration bank-sim payer-psp-sim card-network-sim seed simulate train loadtest chaos demo deploy-dev destroy-dev

LEDGER_DB ?= tally_ledger_v1
CHAOS_FLOW_SCENARIOS ?= 2000

up:
	docker compose up -d --wait

down:
	docker compose down

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy libs services tests chaos ml scripts
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
	uv run python -m scripts.migrate ledger

ledger-test-integration: ledger-migrate
	docker compose exec -T postgres-ledger psql -U tally -d $(LEDGER_DB) -v ON_ERROR_STOP=1 < tests/integration/ledger_posting.sql

gateway-migrate:
	uv run python -m scripts.migrate gateway

gateway-test-integration: gateway-migrate
	docker compose exec -T postgres-general psql -U tally -d tally -v ON_ERROR_STOP=1 < tests/integration/gateway_auth.sql
	GENERAL_DATABASE_URL=postgresql://tally:tally-local-only@127.0.0.1:55432/tally REDIS_URL=redis://127.0.0.1:6379/0 uv run pytest tests/integration/test_gateway_auth_dependency.py tests/integration/test_gateway_route.py tests/integration/test_idempotency_postgres.py tests/integration/test_rate_limit_redis.py

gateway-cleanup:
	docker compose exec -T postgres-general psql -U tally -d tally -v ON_ERROR_STOP=1 -c "SELECT * FROM gateway_cleanup_expired_state(10000)"

vault-migrate:
	uv run python -m scripts.migrate vault

vault-test-integration: vault-migrate
	docker compose exec -T postgres-vault psql -U tally -d tally_vault -v ON_ERROR_STOP=1 < tests/integration/vault_access.sql
	VAULT_DATABASE_URL=postgresql://tally:tally-local-only@127.0.0.1:55434/tally_vault uv run pytest tests/integration/test_vault_api.py tests/integration/test_vault_network.py

core-migrate: gateway-migrate
	uv run python -m scripts.migrate core

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

migrate:
	uv run python -m scripts.migrate gateway core ledger vault recon risk backoffice

stack-test-integration:
	TALLY_STACK_TESTS=1 TALLY_KAFKA_BOOTSTRAP=127.0.0.1:19092 REDIS_URL=redis://127.0.0.1:6379/0 uv run pytest tests/integration/test_crash_recovery.py tests/integration/test_money_movement.py tests/integration/test_outbox_kafka.py tests/integration/test_reconciliation.py tests/integration/test_risk_flow.py tests/integration/test_risk_retrain.py tests/integration/test_risk_feature_parity.py tests/security

backoffice-api:
	TALLY_DATABASE_URL=postgresql://tally:tally-local-only@127.0.0.1:55432/tally TALLY_INTERNAL_KEY=tally-local-internal TALLY_RECOVERY_KEY=tally-local-recovery TALLY_API_KEY_ENCRYPTION_KEY=$${TALLY_API_KEY_ENCRYPTION_KEY:?set it} TALLY_BACKOFFICE_SECRET_KEY=$${TALLY_BACKOFFICE_SECRET_KEY:?set it} uv run uvicorn services.backoffice_api.app:app --host 127.0.0.1 --port 8040

recon-api:
	TALLY_DATABASE_URL=postgresql://tally:tally-local-only@127.0.0.1:55432/tally TALLY_INTERNAL_KEY=tally-local-internal TALLY_S3_ENDPOINT=http://127.0.0.1:8333 uv run uvicorn services.recon.api:app --host 127.0.0.1 --port 8020

recon-eval:
	uv run python -m scripts.recon_eval

chaos:
	uv run python -m chaos.chaos_sim
	uv run python -m chaos.flow_sim --scenarios $(CHAOS_FLOW_SCENARIOS)

train:
	uv run python -m ml.train
	uv run python -m ml.model_card

retrain:
	uv run python -m ml.retrain --version fraud-gbm-$$(date +%Y%m%d%H%M)

risk-api:
	TALLY_DATABASE_URL=postgresql://tally:tally-local-only@127.0.0.1:55432/tally REDIS_URL=redis://127.0.0.1:6379/0 TALLY_INTERNAL_KEY=tally-local-internal TALLY_RECOVERY_KEY=tally-local-recovery uv run uvicorn services.risk.api:app --host 127.0.0.1 --port 8030

dev:
	uv run python -m scripts.dev_stack

web:
	cd web/console && npm run dev

web-check:
	cd web/console && npm run typecheck && npx eslint src e2e && npm test && npm run build

e2e:
	cd web/console && CI=1 npx playwright test

simulate loadtest demo deploy-dev destroy-dev:
	@echo "$@ is not available yet. See docs/PROGRESS.md for implementation status."
