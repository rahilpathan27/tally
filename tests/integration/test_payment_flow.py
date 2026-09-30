from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
from libs.idempotency.store import PostgresIdempotencyStore
from libs.security.envelope_encryption import EnvelopeCipher
from libs.security.hmac_auth import sign_request
from libs.security.key_encryption import ApiKeyCipher
from libs.security.rate_limit import MerchantRateLimiter
from redis.asyncio import Redis
from services.api_gateway.auth import MerchantHmacAuth
from services.core.api import PaymentIntentRepository, create_app
from services.simulators.bank.api import BankSimulatorConfig
from services.simulators.bank.api import create_app as create_bank_app
from services.simulators.network.api import CardNetworkConfig
from services.simulators.network.api import create_app as create_network_app
from services.simulators.payer_psp.api import PayerPspConfig
from services.simulators.payer_psp.api import create_app as create_psp_app
from services.vault.api import VaultConfig
from services.vault.api import create_app as create_vault_app
from services.vault.repository import VaultRepository

TEST_PAN = "4242424242424242"
PROVISIONING_KEY = "tally-local-ledger-provisioner"
SIMULATOR_KEY = "phase5-network-simulator-key"
TOKENIZE_KEY = "phase5-tokenize-client-key"
VAULT_NETWORK_KEY = "phase5-vault-network-key"
NETWORK_KEY = b"phase5-merchant-hmac-secret-with-more-than-32-bytes"


def test_card_and_upi_happy_paths_and_illegal_transition_are_audited() -> None:
    required = ("TALLY_DATABASE_URL", "LEDGER_DATABASE_URL", "VAULT_DATABASE_URL", "REDIS_URL")
    if any(not os.environ.get(name) for name in required):
        pytest.skip("configure general, ledger, vault and Redis URLs to run payment e2e")

    async def exercise() -> None:
        general_url = os.environ["TALLY_DATABASE_URL"]
        ledger_url = os.environ["LEDGER_DATABASE_URL"]
        vault_url = os.environ["VAULT_DATABASE_URL"]
        general_pool = await asyncpg.create_pool(general_url, min_size=1, max_size=8)
        ledger_pool = await asyncpg.create_pool(ledger_url, min_size=1, max_size=8)

        async def set_vault_role(connection: asyncpg.Connection) -> None:
            await connection.execute("SET ROLE tally_vault_app")

        vault_pool = await asyncpg.create_pool(
            vault_url, min_size=1, max_size=5, setup=set_vault_role
        )
        redis = Redis.from_url(os.environ["REDIS_URL"])
        test_merchant = f"phase5-{uuid4().hex}"
        key_id = f"phase5-key-{uuid4().hex}"
        cipher = ApiKeyCipher(b"p" * 32)
        vault_app = create_vault_app()
        vault_app.state.vault_config = VaultConfig(
            vault_url, b"v" * 32, frozenset({TEST_PAN}), TOKENIZE_KEY, VAULT_NETWORK_KEY
        )
        vault_app.state.vault_repository = VaultRepository(vault_pool, EnvelopeCipher(b"v" * 32))
        vault_http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=vault_app), base_url="http://vault"
        )
        network_app = create_network_app(
            CardNetworkConfig("http://vault", VAULT_NETWORK_KEY, SIMULATOR_KEY)
        )
        network_app.state.http_client = vault_http
        bank_app = create_bank_app(BankSimulatorConfig())
        psp_app = create_psp_app(PayerPspConfig())

        from services.ledger.api import app as ledger_app

        ledger_app.state.pool = ledger_pool
        ledger_app.state.provisioning_key = PROVISIONING_KEY
        core_app = create_app()
        core_app.state.gateway_auth = MerchantHmacAuth(general_pool, cipher.decrypt)
        core_app.state.gateway_rate_limiter = MerchantRateLimiter(
            redis, namespace="integration:phase5"
        )
        core_app.state.gateway_idempotency = PostgresIdempotencyStore(general_pool)
        core_app.state.gateway_rate_limit_policy = lambda _: (300, 60)
        core_app.state.payment_repository = PaymentIntentRepository(general_pool)
        core_app.state.ledger_provisioning_key = PROVISIONING_KEY
        core_app.state.network_simulator_key = SIMULATOR_KEY
        core_app.state.ledger_http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=ledger_app), base_url="http://ledger"
        )
        core_app.state.card_network_http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=network_app), base_url="http://network"
        )
        core_app.state.bank_http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=bank_app), base_url="http://bank"
        )
        core_app.state.payer_psp_http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=psp_app), base_url="http://psp"
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=core_app), base_url="http://core"
        ) as client:
            try:
                await general_pool.execute(
                    "INSERT INTO merchants(merchant_id, display_name) VALUES ($1, $2)",
                    test_merchant,
                    "Phase 5 payment integration",
                )
                await general_pool.execute(
                    """INSERT INTO merchant_api_keys(
                           key_id, merchant_id, secret_ciphertext, scopes, mode, expires_at
                       ) VALUES ($1, $2, $3, $4, 'test', $5)""",
                    key_id,
                    test_merchant,
                    cipher.encrypt(key_id, NETWORK_KEY),
                    ["payments:write", "payments:read"],
                    datetime.now(UTC) + timedelta(hours=1),
                )
                token = (await vault_app.state.vault_repository.tokenize(TEST_PAN, 12, 2099)).token

                async def send(
                    method: str,
                    path: str,
                    body: dict[str, object] | None,
                    idempotency_key: str | None = None,
                    request_id: str | None = None,
                ) -> httpx.Response:
                    raw = (
                        json.dumps(body, separators=(",", ":")).encode()
                        if body is not None
                        else b""
                    )
                    nonce = f"phase5-{uuid4().hex}"
                    timestamp = int(datetime.now(UTC).timestamp())
                    headers = {
                        "x-tally-key-id": key_id,
                        "x-tally-timestamp": str(timestamp),
                        "x-tally-nonce": nonce,
                        "x-tally-signature": sign_request(
                            NETWORK_KEY, method, path, raw, timestamp, nonce
                        ),
                    }
                    if raw:
                        headers["content-type"] = "application/json"
                    if idempotency_key:
                        headers["idempotency-key"] = idempotency_key
                    if request_id:
                        headers["x-request-id"] = request_id
                    return await client.request(method, path, content=raw, headers=headers)

                card_create = await send(
                    "POST",
                    "/v1/payment_intents",
                    {
                        "amount_minor": 1250,
                        "currency": "INR",
                        "payment_method_type": "card",
                        "payment_method_token": token,
                    },
                    "phase5-card-create",
                    TEST_PAN,
                )
                assert card_create.status_code == 201, card_create.text
                card_id = card_create.json()["payment_id"]
                card_confirm = await send(
                    "POST",
                    f"/v1/payment_intents/{card_id}/confirm",
                    {},
                    "phase5-card-confirm",
                )
                assert card_confirm.status_code == 200, card_confirm.text
                assert card_confirm.json()["status"] == "authorized"
                capture = await send(
                    "POST",
                    f"/v1/payment_intents/{card_id}/capture",
                    {},
                    "phase5-card-capture",
                )
                assert capture.status_code == 200, capture.text
                assert capture.json()["status"] == "succeeded"

                cancel_create = await send(
                    "POST",
                    "/v1/payment_intents",
                    {
                        "amount_minor": 300,
                        "currency": "INR",
                        "payment_method_type": "card",
                        "payment_method_token": token,
                    },
                    "phase5-card-cancel-create",
                )
                assert cancel_create.status_code == 201, cancel_create.text
                cancel_id = cancel_create.json()["payment_id"]
                cancel_confirm = await send(
                    "POST",
                    f"/v1/payment_intents/{cancel_id}/confirm",
                    {},
                    "phase5-card-cancel-confirm",
                )
                assert cancel_confirm.status_code == 200
                cancelled = await send(
                    "POST",
                    f"/v1/payment_intents/{cancel_id}/cancel",
                    {},
                    "phase5-card-cancel",
                )
                assert cancelled.status_code == 200
                assert cancelled.json()["status"] == "cancelled"
                card_transition_correlation = await general_pool.fetchval(
                    """SELECT correlation_id FROM payment_transitions
                       WHERE payment_id = $1 ORDER BY transition_id LIMIT 1""",
                    UUID(card_id),
                )
                assert TEST_PAN not in card_transition_correlation
                card_hold_id = await general_pool.fetchval(
                    "SELECT ledger_hold_id FROM payment_intents WHERE payment_id = $1",
                    UUID(cancel_id),
                )
                assert (
                    await ledger_pool.fetchval(
                        "SELECT status::text FROM ledger_holds WHERE hold_id = $1", card_hold_id
                    )
                    == "void"
                )

                upi_create = await send(
                    "POST",
                    "/v1/payment_intents",
                    {
                        "amount_minor": 875,
                        "currency": "INR",
                        "payment_method_type": "upi",
                        "payer_vpa": "payer@bank-a",
                        "payee_vpa": "merchant@bank-b",
                    },
                    "phase5-upi-create",
                )
                assert upi_create.status_code == 201, upi_create.text
                upi_id = upi_create.json()["payment_id"]
                confirm_path = f"/v1/payment_intents/{upi_id}/confirm"
                upi_confirm = await send("POST", confirm_path, {}, "phase5-upi-confirm")
                assert upi_confirm.status_code == 200, upi_confirm.text
                assert upi_confirm.json()["status"] == "succeeded"
                replay = await send("POST", confirm_path, {}, "phase5-upi-confirm")
                assert replay.status_code == 200
                assert replay.json() == upi_confirm.json()

                illegal_capture = await send(
                    "POST",
                    f"/v1/payment_intents/{upi_id}/capture",
                    {},
                    "phase5-illegal-capture",
                )
                assert illegal_capture.status_code == 409
                recorded = await general_pool.fetchrow(
                    """SELECT from_state, to_state, accepted
                       FROM payment_transitions WHERE payment_id = $1 AND accepted = false
                       ORDER BY transition_id DESC LIMIT 1""",
                    UUID(upi_id),
                )
                assert recorded is not None
                assert recorded["from_state"] == "succeeded"
                assert recorded["to_state"] == "capturing"
                assert recorded["accepted"] is False

                assert (
                    await general_pool.fetchval(
                        "SELECT count(*) FROM core_outbox WHERE aggregate_id = ANY($1::uuid[])",
                        [UUID(card_id), UUID(cancel_id), UUID(upi_id)],
                    )
                    == 12
                )
                assert (
                    await ledger_pool.fetchval(
                        "SELECT count(*) FROM ledger_journal_entries WHERE idempotency_key = $1",
                        f"{upi_id}:upi:transfer:post",
                    )
                    == 1
                )
                assert await ledger_pool.fetchval(
                    "SELECT bool_and(ok) FROM ledger_verify_integrity()"
                ) is True
            finally:
                await client.aclose()
                await core_app.state.ledger_http.aclose()
                await core_app.state.card_network_http.aclose()
                await core_app.state.bank_http.aclose()
                await core_app.state.payer_psp_http.aclose()
                await vault_http.aclose()
                await redis.aclose()
                await general_pool.close()
                await ledger_pool.close()
                await vault_pool.close()

    asyncio.run(exercise())
