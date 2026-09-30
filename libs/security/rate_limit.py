"""Redis-backed atomic fixed-window rate limiting for merchant requests."""

from __future__ import annotations

import time
from dataclasses import dataclass

from redis.asyncio import Redis

_FIXED_WINDOW = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local remaining = tonumber(ARGV[2]) - count
if remaining < 0 then remaining = 0 end
local allowed = 0
if count <= tonumber(ARGV[2]) then allowed = 1 end
return {allowed, remaining, count}
"""


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    reset_after_seconds: int
    observed_count: int


class MerchantRateLimiter:
    def __init__(self, redis: Redis, *, namespace: str = "tally:rate") -> None:
        if not namespace.strip(":"):
            raise ValueError("namespace must be a non-empty key prefix")
        self._redis = redis
        self._namespace = namespace.rstrip(":")

    async def check(self, merchant_id: str, *, limit: int, window_seconds: int) -> RateLimitResult:
        if not merchant_id or limit <= 0 or window_seconds <= 0:
            raise ValueError("merchant, positive limit, and positive window are required")
        bucket = int(time.time()) // window_seconds
        key = f"{self._namespace}:{merchant_id}:{window_seconds}:{bucket}"
        values = await self._redis.eval(_FIXED_WINDOW, 1, key, window_seconds, limit)
        return RateLimitResult(
            allowed=bool(int(values[0])),
            remaining=int(values[1]),
            reset_after_seconds=max(window_seconds - int(time.time()) % window_seconds, 1),
            observed_count=int(values[2]),
        )
