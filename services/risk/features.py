"""Shared feature definitions: the same code computes features online (Redis) and offline.

Train/serve parity comes from one implementation of the feature logic operating on a
``FeatureState`` interface. ``MemoryFeatureState`` replays history for training;
``RedisFeatureState`` serves live decisions. ``tests/unit/test_risk_features.py`` feeds the same
event stream through both and requires identical vectors.

Features are computed from state *before* the current event is recorded; recording happens after
the decision, exactly as the offline replay does.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from libs.common.business_time import IST

HOUR = 3_600
DAY = 86_400
FEATURE_NAMES: tuple[str, ...] = (
    "amount_log",
    "is_card",
    "hour_ist",
    "instrument_count_1h",
    "instrument_count_24h",
    "instrument_amount_log_24h",
    "instrument_distinct_devices_24h",
    "instrument_distinct_payees_24h",
    "device_count_1h",
    "device_distinct_instruments_24h",
    "ip_count_1h",
    "payee_distinct_payers_24h",
    "merchant_count_1h",
    "new_payee",
    "new_device_for_instrument",
    "geo_mismatch",
    "amount_to_instrument_avg",
    "log_seconds_since_last",
    "account_age_days",
)


@dataclass(frozen=True, slots=True)
class RiskEvent:
    payment_id: str
    merchant_id: str
    occurred_at: datetime
    amount_minor: int
    method: str  # card | upi
    instrument_id: str  # card token or payer VPA
    payee_id: str  # payee VPA or merchant
    device_id: str
    ip_address: str
    ip_country: str
    instrument_country: str
    account_age_days: int


class FeatureState(Protocol):
    """Windowed history store. Timestamps are integer epoch seconds."""

    async def count(self, key: str, since: int) -> int: ...

    async def total(self, key: str, since: int) -> int: ...

    async def distinct(self, key: str, since: int) -> int: ...

    async def seen(self, key: str, member: str) -> bool: ...

    async def last_time(self, key: str) -> int | None: ...

    async def record(self, event: RiskEvent) -> None: ...


def _keys(event: RiskEvent) -> dict[str, str]:
    return {
        "instrument": f"i:{event.instrument_id}",
        "device": f"d:{event.device_id}",
        "ip": f"ip:{event.ip_address}",
        "payee": f"p:{event.payee_id}",
        "merchant": f"m:{event.merchant_id}",
        "instrument_devices": f"id:{event.instrument_id}",
        "instrument_payees": f"ip2:{event.instrument_id}",
        "device_instruments": f"di:{event.device_id}",
        "payee_payers": f"pp:{event.payee_id}",
        "instrument_payees_ever": f"ipe:{event.instrument_id}",
        "instrument_devices_ever": f"ide:{event.instrument_id}",
        "instrument_amounts_30d": f"ia:{event.instrument_id}",
    }


def epoch(moment: datetime) -> int:
    return int(moment.timestamp())


async def compute_features(event: RiskEvent, state: FeatureState) -> dict[str, float]:
    now = epoch(event.occurred_at)
    k = _keys(event)
    count_30d = await state.count(k["instrument_amounts_30d"], now - 30 * DAY)
    total_30d = await state.total(k["instrument_amounts_30d"], now - 30 * DAY)
    last = await state.last_time(k["instrument"])
    average = total_30d / count_30d if count_30d else 0.0
    values: dict[str, float] = {
        "amount_log": math.log1p(event.amount_minor),
        "is_card": 1.0 if event.method == "card" else 0.0,
        "hour_ist": float(event.occurred_at.astimezone(IST).hour),
        "instrument_count_1h": float(await state.count(k["instrument"], now - HOUR)),
        "instrument_count_24h": float(await state.count(k["instrument"], now - DAY)),
        "instrument_amount_log_24h": math.log1p(await state.total(k["instrument"], now - DAY)),
        "instrument_distinct_devices_24h": float(
            await state.distinct(k["instrument_devices"], now - DAY)
        ),
        "instrument_distinct_payees_24h": float(
            await state.distinct(k["instrument_payees"], now - DAY)
        ),
        "device_count_1h": float(await state.count(k["device"], now - HOUR)),
        "device_distinct_instruments_24h": float(
            await state.distinct(k["device_instruments"], now - DAY)
        ),
        "ip_count_1h": float(await state.count(k["ip"], now - HOUR)),
        "payee_distinct_payers_24h": float(await state.distinct(k["payee_payers"], now - DAY)),
        "merchant_count_1h": float(await state.count(k["merchant"], now - HOUR)),
        "new_payee": 0.0 if await state.seen(k["instrument_payees_ever"], event.payee_id) else 1.0,
        "new_device_for_instrument": 0.0
        if await state.seen(k["instrument_devices_ever"], event.device_id)
        else 1.0,
        "geo_mismatch": 1.0 if event.ip_country != event.instrument_country else 0.0,
        "amount_to_instrument_avg": min(50.0, event.amount_minor / average) if average else 1.0,
        "log_seconds_since_last": math.log1p(now - last) if last is not None else 15.0,
        "account_age_days": float(min(event.account_age_days, 3_650)),
    }
    return {name: round(values[name], 6) for name in FEATURE_NAMES}


def vector(features: dict[str, float]) -> list[float]:
    return [features[name] for name in FEATURE_NAMES]


WINDOWED = ("instrument", "device", "ip", "merchant", "instrument_amounts_30d")
DISTINCT = (
    ("instrument_devices", "device_id"),
    ("instrument_payees", "payee_id"),
    ("device_instruments", "instrument_id"),
    ("payee_payers", "instrument_id"),
)
EVER = (("instrument_payees_ever", "payee_id"), ("instrument_devices_ever", "device_id"))


class MemoryFeatureState:
    """Exact in-memory implementation for offline replay (events arrive in time order)."""

    def __init__(self) -> None:
        self._ts: dict[str, list[int]] = defaultdict(list)
        self._cumulative: dict[str, list[int]] = defaultdict(list)
        self._latest: dict[str, dict[str, int]] = defaultdict(dict)
        self._members: dict[str, set[str]] = defaultdict(set)
        self._last: dict[str, int] = {}

    async def count(self, key: str, since: int) -> int:
        stamps = self._ts[key]
        return len(stamps) - bisect_left(stamps, since)

    async def total(self, key: str, since: int) -> int:
        stamps = self._ts[key]
        index = bisect_left(stamps, since)
        if index >= len(stamps):
            return 0
        sums = self._cumulative[key]
        return sums[-1] - (sums[index - 1] if index else 0)

    async def distinct(self, key: str, since: int) -> int:
        return sum(1 for ts in self._latest[key].values() if ts >= since)

    async def seen(self, key: str, member: str) -> bool:
        return member in self._members[key]

    async def last_time(self, key: str) -> int | None:
        return self._last.get(key)

    async def record(self, event: RiskEvent) -> None:
        now = epoch(event.occurred_at)
        k = _keys(event)
        for name in WINDOWED:
            stamps = self._ts[k[name]]
            if stamps and now < stamps[-1]:
                raise ValueError("offline replay must be in time order")
            stamps.append(now)
            sums = self._cumulative[k[name]]
            sums.append((sums[-1] if sums else 0) + event.amount_minor)
        for name, attribute in DISTINCT:
            member = str(getattr(event, attribute))
            latest = self._latest[k[name]]
            latest[member] = max(now, latest.get(member, now))
        for name, attribute in EVER:
            self._members[k[name]].add(str(getattr(event, attribute)))
        self._last[k["instrument"]] = now


class RedisFeatureState:
    """Online store: sorted sets for windows, a set per "ever seen" relation."""

    def __init__(self, redis: Any, namespace: str = "rf") -> None:
        self.redis = redis
        self.ns = namespace

    async def count(self, key: str, since: int) -> int:
        return int(await self.redis.zcount(f"{self.ns}:w:{key}", since, "+inf"))

    async def total(self, key: str, since: int) -> int:
        members = await self.redis.zrangebyscore(f"{self.ns}:w:{key}", since, "+inf")
        return sum(int(member.rsplit(b":", 1)[1]) for member in members)

    async def distinct(self, key: str, since: int) -> int:
        return int(await self.redis.zcount(f"{self.ns}:d:{key}", since, "+inf"))

    async def seen(self, key: str, member: str) -> bool:
        return bool(await self.redis.sismember(f"{self.ns}:s:{key}", member))

    async def last_time(self, key: str) -> int | None:
        value = await self.redis.get(f"{self.ns}:l:{key}")
        return None if value is None else int(value)

    async def record(self, event: RiskEvent) -> None:
        now = epoch(event.occurred_at)
        k = _keys(event)
        pipe = self.redis.pipeline(transaction=False)
        for name in WINDOWED:
            key = f"{self.ns}:w:{k[name]}"
            pipe.zadd(key, {f"{event.payment_id}:{event.amount_minor}": now})
            pipe.zremrangebyscore(key, "-inf", now - 31 * DAY)
            pipe.expire(key, 32 * DAY)
        for name, attribute in DISTINCT:
            key = f"{self.ns}:d:{k[name]}"
            pipe.zadd(key, {str(getattr(event, attribute)): now}, gt=True)
            pipe.zremrangebyscore(key, "-inf", now - 2 * DAY)
            pipe.expire(key, 3 * DAY)
        for name, attribute in EVER:
            key = f"{self.ns}:s:{k[name]}"
            pipe.sadd(key, str(getattr(event, attribute)))
            pipe.expire(key, 180 * DAY)
        pipe.set(f"{self.ns}:l:{k['instrument']}", now, ex=180 * DAY)
        await pipe.execute()
