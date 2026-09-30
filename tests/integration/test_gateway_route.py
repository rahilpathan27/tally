from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from libs.idempotency.store import PostgresIdempotencyStore, request_fingerprint
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


def test_gateway_releases_5xx_caches_4xx_and_takes_over_stale_reservations() -> None:
    database_url = os.environ.get("GENERAL_DATABASE_URL")
    redis_url = os.environ.get("REDIS_URL")
    if database_url is None or redis_url is None:
        pytest.skip("set general PostgreSQL and Redis URLs to run gateway route checks")

    async def exercise() -> None:
        merchant_id = f"route-err-{uuid4().hex}"
        key_id = f"key-{uuid4().hex}"
        secret = b"gateway-route-integration-secret-32bytes"
        cipher = ApiKeyCipher(b"r" * 32)
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
        redis = Redis.from_url(redis_url)
        now = datetime.now(UTC)
        calls = {"flaky": 0, "reject": 0, "stale": 0}

        async with pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO merchants(merchant_id, display_name) VALUES ($1, 'errors')",
                merchant_id,
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
        app.state.gateway_rate_limiter = MerchantRateLimiter(redis, namespace=f"it:{merchant_id}")
        app.state.gateway_idempotency = PostgresIdempotencyStore(pool)
        app.state.gateway_rate_limit_policy = lambda _: (100, 60)

        @app.post("/v1/flaky")
        @requires_scope("resources:write")
        async def flaky() -> dict[str, object]:
            calls["flaky"] += 1
            if calls["flaky"] == 1:
                raise HTTPException(503, detail={"code": "DEPENDENCY_DOWN"})
            return {"ok": True}

        @app.post("/v1/reject")
        @requires_scope("resources:write")
        async def reject() -> dict[str, object]:
            calls["reject"] += 1
            raise HTTPException(409, detail={"code": "ILLEGAL"})

        @app.post("/v1/stale")
        @requires_scope("resources:write")
        async def stale() -> dict[str, object]:
            calls["stale"] += 1
            return {"ok": True}

        async def post(client: httpx.AsyncClient, path: str, key: str) -> httpx.Response:
            timestamp = int(now.timestamp())
            nonce = f"nonce-{uuid4().hex}"
            return await client.post(
                path,
                content=b"",
                headers={
                    "idempotency-key": key,
                    "x-tally-key-id": key_id,
                    "x-tally-timestamp": str(timestamp),
                    "x-tally-nonce": nonce,
                    "x-tally-signature": sign_request(secret, "POST", path, b"", timestamp, nonce),
                },
            )

        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await post(client, "/v1/flaky", "flaky-1")).status_code == 503
                retried = await post(client, "/v1/flaky", "flaky-1")
                assert retried.status_code == 200 and calls["flaky"] == 2

                assert (await post(client, "/v1/reject", "reject-1")).status_code == 409
                replayed = await post(client, "/v1/reject", "reject-1")
                assert replayed.status_code == 409 and calls["reject"] == 1
                assert replayed.json() == {"detail": {"code": "ILLEGAL"}}

                # Simulate a request whose process died holding the reservation.
                store = PostgresIdempotencyStore(pool)
                fingerprint = request_fingerprint("POST", "/v1/stale", b"")
                assert (await store.begin(merchant_id, "stale-1", fingerprint)).outcome == (
                    "started"
                )
                blocked = await post(client, "/v1/stale", "stale-1")
                assert blocked.status_code == 409 and calls["stale"] == 0
                await pool.execute(
                    """UPDATE gateway_idempotency_requests
                       SET locked_until = clock_timestamp() - interval '1 second'
                       WHERE merchant_id = $1 AND idempotency_key = 'stale-1'""",
                    merchant_id,
                )
                taken_over = await post(client, "/v1/stale", "stale-1")
                assert taken_over.status_code == 200 and calls["stale"] == 1
        finally:
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
