from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from libs.security.envelope_encryption import EnvelopeCipher
from services.vault.api import VaultConfig, create_app
from services.vault.repository import VaultRepository

TEST_PAN = "4242424242424242"
TOKENIZE_KEY = "local-test-tokenize-key"
NETWORK_KEY = "local-test-network-key"


def test_tokenize_response_scrubs_pan_and_detokenization_is_restricted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    database_url = os.environ.get("VAULT_DATABASE_URL")
    if not database_url:
        pytest.skip("VAULT_DATABASE_URL is not configured")

    async def exercise() -> None:
        async def set_runtime_role(connection: asyncpg.Connection) -> None:
            await connection.execute("SET ROLE tally_vault_app")

        pool = await asyncpg.create_pool(
            database_url, min_size=1, max_size=3, setup=set_runtime_role
        )
        app = create_app()
        app.state.vault_config = VaultConfig(
            database_url,
            b"v" * 32,
            frozenset({TEST_PAN}),
            TOKENIZE_KEY,
            NETWORK_KEY,
        )
        app.state.vault_repository = VaultRepository(pool, EnvelopeCipher(b"v" * 32))
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://vault.test"
            ) as client:
                payload = {"pan": TEST_PAN, "expiry_month": 12, "expiry_year": 2099}
                created = await client.post(
                    "/v1/tokens", json=payload, headers={"x-vault-client-key": TOKENIZE_KEY}
                )
                assert created.status_code == 201
                assert TEST_PAN not in created.text
                assert TEST_PAN not in caplog.text
                token_data = created.json()
                assert token_data["last4"] == "4242"
                token = token_data["payment_method_token"]

                unauthorized = await client.post(f"/internal/v1/tokens/{token}/detokenize")
                assert unauthorized.status_code == 401
                bad_credential = await client.post(
                    f"/internal/v1/tokens/{token}/detokenize",
                    headers={"x-vault-network-key": "wrong"},
                )
                assert bad_credential.status_code == 401
                revealed = await client.post(
                    f"/internal/v1/tokens/{token}/detokenize",
                    headers={
                        "x-vault-network-key": NETWORK_KEY,
                        "x-request-id": TEST_PAN,
                    },
                )
                assert revealed.status_code == 200
                assert revealed.json() == {"pan": TEST_PAN}

                rejected = await client.post(
                    "/v1/tokens",
                    json={**payload, "cvv": "987"},
                    headers={"x-vault-client-key": TOKENIZE_KEY},
                )
                assert rejected.status_code == 422
                assert TEST_PAN not in rejected.text and "987" not in rejected.text
                assert TEST_PAN not in caplog.text and "987" not in caplog.text

                async with pool.acquire() as connection:
                    row = await connection.fetchrow(
                        "SELECT encrypted_pan, last4 FROM vault_cards WHERE token = $1", token
                    )
                    assert row is not None
                    assert TEST_PAN.encode() not in bytes(row["encrypted_pan"])
                    assert row["last4"].strip() == "4242"
                audit_connection = await asyncpg.connect(database_url)
                try:
                    logged_request_id = await audit_connection.fetchval(
                        "SELECT request_id FROM vault_access_events WHERE token = $1", token
                    )
                    assert TEST_PAN not in logged_request_id
                    assert (
                        await audit_connection.fetchval("SELECT vault_verify_access_chain()")
                        is True
                    )
                finally:
                    await audit_connection.close()
        finally:
            await pool.close()

    asyncio.run(exercise())
