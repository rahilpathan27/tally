from __future__ import annotations

import asyncio
import os
import time
from uuid import uuid4

import pytest
from libs.security.rate_limit import MerchantRateLimiter
from redis.asyncio import Redis


def test_redis_rate_limit_is_atomic_and_expires() -> None:
    redis_url = os.environ.get("REDIS_URL")
    if redis_url is None:
        pytest.skip("set REDIS_URL to run Redis-backed integration checks")

    async def exercise() -> None:
        merchant = f"integration-{uuid4().hex}"
        client = Redis.from_url(redis_url)
        limiter = MerchantRateLimiter(client)
        try:
            first = await limiter.check(merchant, limit=2, window_seconds=60)
            second = await limiter.check(merchant, limit=2, window_seconds=60)
            third = await limiter.check(merchant, limit=2, window_seconds=60)
            assert first.allowed and first.remaining == 1
            assert second.allowed and second.remaining == 0
            assert not third.allowed and third.observed_count == 3
        finally:
            now_bucket = int(time.time()) // 60
            await client.delete(f"tally:rate:{merchant}:60:{now_bucket}")
            await client.aclose()

    asyncio.run(exercise())
