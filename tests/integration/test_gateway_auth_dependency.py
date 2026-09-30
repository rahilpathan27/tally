from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest
from fastapi import HTTPException
from libs.security.hmac_auth import sign_request
from libs.security.key_encryption import ApiKeyCipher
from services.api_gateway.auth import MerchantHmacAuth, MerchantPrincipal, require_scope
from starlette.requests import Request


def test_merchant_hmac_dependency_checks_ciphertext_and_consumes_nonce() -> None:
    database_url = os.environ.get("GENERAL_DATABASE_URL")
    if database_url is None:
        pytest.skip("set GENERAL_DATABASE_URL to run gateway authentication checks")

    async def exercise() -> None:
        merchant_id = f"auth-{uuid4().hex}"
        key_id = f"key-{uuid4().hex}"
        secret = b"auth-integration-secret-material-32bytes"
        cipher = ApiKeyCipher(b"k" * 32)
        now = datetime.now(UTC)
        timestamp = int(now.timestamp())
        path = "/v1/payments?mode=test"
        body = b'{"amount_minor":87}'
        nonce = "auth-integration-nonce-0001"
        signature = sign_request(secret, "POST", path, body, timestamp, nonce)
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)

        def make_request(request_nonce: str = nonce, request_signature: str = signature) -> Request:
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/v1/payments",
                "raw_path": b"/v1/payments",
                "query_string": b"mode=test",
                "root_path": "",
                "headers": [
                    (b"x-tally-key-id", key_id.encode()),
                    (b"x-tally-timestamp", str(timestamp).encode()),
                    (b"x-tally-nonce", request_nonce.encode()),
                    (b"x-tally-signature", request_signature.encode()),
                ],
                "client": ("127.0.0.1", 12345),
                "server": ("127.0.0.1", 8000),
            }

            async def receive() -> dict[str, object]:
                return {"type": "http.request", "body": body, "more_body": False}

            return Request(scope, receive)

        try:
            async with pool.acquire() as connection:
                await connection.execute(
                    "INSERT INTO merchants(merchant_id, display_name) VALUES ($1, $2)",
                    merchant_id,
                    "HMAC integration",
                )
                await connection.execute(
                    """INSERT INTO merchant_api_keys(
                           key_id, merchant_id, secret_ciphertext, scopes, mode, expires_at
                       ) VALUES ($1, $2, $3, $4, 'test', $5)""",
                    key_id,
                    merchant_id,
                    cipher.encrypt(key_id, secret),
                    ["payments:write"],
                    now + timedelta(hours=1),
                )
            auth = MerchantHmacAuth(pool, cipher.decrypt, clock=lambda: now)
            principal = await auth(make_request())
            assert principal == MerchantPrincipal(
                merchant_id, key_id, frozenset({"payments:write"}), "test"
            )
            require_scope(principal, "payments:write")
            with pytest.raises(HTTPException) as denied:
                require_scope(principal, "refunds:write")
            assert denied.value.status_code == 403

            with pytest.raises(HTTPException) as replay:
                await auth(make_request())
            assert replay.value.status_code == 409
            with pytest.raises(HTTPException) as tampered:
                await auth(make_request("auth-integration-nonce-0002", "0" * 64))
            assert tampered.value.status_code == 401
        finally:
            async with pool.acquire() as connection:
                await connection.execute(
                    "DELETE FROM gateway_request_nonces WHERE key_id = $1", key_id
                )
                await connection.execute("DELETE FROM merchant_api_keys WHERE key_id = $1", key_id)
                await connection.execute(
                    "DELETE FROM merchants WHERE merchant_id = $1", merchant_id
                )
            await pool.close()

    asyncio.run(exercise())
