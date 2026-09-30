from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest
from libs.idempotency.store import (
    IdempotencyPayloadConflict,
    PostgresIdempotencyStore,
    request_fingerprint,
)


def test_postgres_idempotency_concurrent_reservation_and_replay() -> None:
    database_url = os.environ.get("GENERAL_DATABASE_URL")
    if database_url is None:
        pytest.skip("set GENERAL_DATABASE_URL to run PostgreSQL gateway-store checks")

    async def exercise() -> None:
        merchant_id = f"integration-{uuid4().hex}"
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=20)
        try:
            async with pool.acquire() as connection:
                await connection.execute(
                    "INSERT INTO merchants(merchant_id, display_name) VALUES ($1, $2)",
                    merchant_id,
                    "Gateway store integration",
                )
            store = PostgresIdempotencyStore(pool)
            fingerprint = request_fingerprint("POST", "/v1/payments", b'{"amount_minor":99}')
            results = await asyncio.gather(
                *(store.begin(merchant_id, "same-key", fingerprint) for _ in range(20))
            )
            assert sum(result.outcome == "started" for result in results) == 1
            assert sum(result.outcome == "in_progress" for result in results) == 19

            await store.complete(merchant_id, "same-key", fingerprint, 201, {"id": "pay-1"})
            replay = await store.begin(merchant_id, "same-key", fingerprint)
            assert replay.outcome == "replay"
            assert replay.response_status == 201
            assert replay.response_body == {"id": "pay-1"}
            with pytest.raises(IdempotencyPayloadConflict):
                await store.begin(
                    merchant_id,
                    "same-key",
                    request_fingerprint("POST", "/v1/payments", b'{"amount_minor":100}'),
                )
        finally:
            async with pool.acquire() as connection:
                await connection.execute(
                    "DELETE FROM gateway_idempotency_requests WHERE merchant_id = $1", merchant_id
                )
                await connection.execute(
                    "DELETE FROM gateway_audit_events WHERE merchant_id = $1", merchant_id
                )
                await connection.execute(
                    "DELETE FROM merchants WHERE merchant_id = $1", merchant_id
                )
            await pool.close()

    asyncio.run(exercise())
