"""Train/serve parity: the Redis online store and the offline replay produce identical vectors."""

import asyncio
import os
from uuid import uuid4

import pytest
from ml.datagen import generate
from redis.asyncio import Redis
from services.risk.features import MemoryFeatureState, RedisFeatureState, compute_features


def test_online_and_offline_features_match_exactly() -> None:
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("set REDIS_URL to run the feature parity test")

    async def exercise() -> None:
        redis = Redis.from_url(url)
        namespace = f"parity:{uuid4().hex}"
        online = RedisFeatureState(redis, namespace=namespace)
        offline = MemoryFeatureState()
        events = generate(days=4, customers=3_000, seed=3)[:3_000]
        compared = 0
        try:
            for item in events:
                served = await compute_features(item.event, online)
                replayed = await compute_features(item.event, offline)
                assert served == replayed, (item.event.payment_id, served, replayed)
                await online.record(item.event)
                await offline.record(item.event)
                compared += 1
        finally:
            keys = [key async for key in redis.scan_iter(f"{namespace}:*")]
            if keys:
                await redis.delete(*keys)
            await redis.aclose()
        assert compared == len(events) > 2_000

    asyncio.run(exercise())
