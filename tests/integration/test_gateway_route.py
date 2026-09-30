from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from libs.idempotency.store import PostgresIdempotencyStore
from libs.security.hmac_auth import sign_request
from libs.security.key_encryption import ApiKeyCipher
from libs.security.rate_limit import MerchantRateLimiter
from redis.asyncio import Redis
from services.api_gateway.auth import MerchantHmacAuth
from services.api_gateway.route import GatewayRoute, requires_scope


def test_gateway_route_composes_auth_rate_limit_scope_and_idempotency() -> None:
    database_url = os.environ.get("GENERAL_DATABASE_URL")
    redis_url = os.environ.get("REDIS_URL")
    if database_url is None or redis_url is None:
        pytest.skip("set general PostgreSQL and Redis URLs to run gateway route checks")

    async def exercise() -> None:
        merchant_id = f"route-{uuid4().hex}"
        key_id = f"key-{uuid4().hex}"
        secret = b"gateway-route-integration-secret-32bytes"
        cipher = ApiKeyCipher(b"r" * 32)
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
        redis = Redis.from_url(redis_url)
        now = datetime.now(UTC)
        limiter = MerchantRateLimiter(redis, namespace="integration:gateway-route")
        execution_count = 0

        async with pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO merchants(merchant_id, display_name) VALUES ($1, $2)",
                merchant_id,
                "Gateway route integration",
            )
            await connection.execute(
                """INSERT INTO merchant_api_keys(
                       key_id, merchant_id, secret_ciphertext, scopes, mode, expires_at
                   ) VALUES ($1, $2, $3, $4, 'test', $5)""",
                key_id,
                merchant_id,
                cipher.encrypt(key_id, secret),
                ["resources:write"],
                now + timedelta(hours=1),
            )

        app = FastAPI()
        app.router.route_class = GatewayRoute
        app.state.gateway_auth = MerchantHmacAuth(pool, cipher.decrypt, clock=lambda: now)
        app.state.gateway_rate_limiter = limiter
        app.state.gateway_idempotency = PostgresIdempotencyStore(pool)
        app.state.gateway_rate_limit_policy = lambda _: (100, 60)

        @app.post("/v1/resources")
        @requires_scope("resources:write")
        async def create_resource(payload: dict[str, int]) -> dict[str, object]:
            nonlocal execution_count
            execution_count += 1
            return {"resource_id": "resource-1", "amount_minor": payload["amount_minor"]}

        def request_headers(path: str, body: bytes, nonce: str) -> dict[str, str]:
            timestamp = int(now.timestamp())
            return {
                "content-type": "application/json",
                "idempotency-key": "resource-create-001",
                "x-tally-key-id": key_id,
                "x-tally-timestamp": str(timestamp),
                "x-tally-nonce": nonce,
                "x-tally-signature": sign_request(secret, "POST", path, body, timestamp, nonce),
            }

        merchant_bucket = int(time.time()) // 60
        limiter_key = f"integration:gateway-route:{merchant_id}:60:{merchant_bucket}"
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                raw_body = b'{"amount_minor":875}'
                path = "/v1/resources"
                first = await client.post(
                    path,
                    content=raw_body,
                    headers=request_headers(path, raw_body, f"nonce-{uuid4().hex}"),
                )
                replay = await client.post(
                    path,
                    content=raw_body,
                    headers=request_headers(path, raw_body, f"nonce-{uuid4().hex}"),
                )
                changed = b'{"amount_minor":876}'
                conflict = await client.post(
                    path,
                    content=changed,
                    headers=request_headers(path, changed, f"nonce-{uuid4().hex}"),
                )
                assert first.status_code == 200, first.text
                assert replay.status_code == 200
                assert replay.json() == first.json()
                assert conflict.status_code == 422
                assert execution_count == 1
        finally:
            await redis.delete(limiter_key)
            async with pool.acquire() as connection:
                await connection.execute(
                    "DELETE FROM gateway_idempotency_requests WHERE merchant_id = $1", merchant_id
                )
                await connection.execute(
                    "DELETE FROM gateway_request_nonces WHERE key_id = $1", key_id
                )
                await connection.execute("DELETE FROM merchant_api_keys WHERE key_id = $1", key_id)
                await connection.execute(
                    "DELETE FROM merchants WHERE merchant_id = $1", merchant_id
                )
            await redis.aclose()
            await pool.close()

    asyncio.run(exercise())
