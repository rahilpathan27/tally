"""Durable, serialized ledger commands for merchant money operations.

Refund debits, dispute debits/credits and settlements read a merchant's ledger balance to decide
how to split an amount (payable vs receivable). They run under a per-merchant PostgreSQL advisory
lock, and before a new operation starts, any command left ``pending`` by a crash is replayed with
its stored request and deterministic key. The ledger returns the original result for a replay, so
replays never duplicate money movement.

A ledger 4xx is a definitive rejection (no effect was committed); a transport error or 5xx is an
unknown outcome that must be retried with the same key before anything else is decided.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import asyncpg
import httpx

from services.core.faults import fault_point


class LedgerRejected(Exception):
    """The ledger definitively refused the command; nothing was committed."""

    def __init__(self, status_code: int, detail: object) -> None:
        super().__init__(f"ledger rejected command with {status_code}")
        self.status_code = status_code
        self.detail = detail


class LedgerUnavailable(Exception):
    """The ledger outcome is unknown; retry with the same idempotency key."""


class MerchantBusy(Exception):
    """Another money operation holds the merchant lock or an earlier one is unresolved."""


async def post_ledger(
    ledger_http: httpx.AsyncClient, path: str, body: dict[str, object] | None = None
) -> dict[str, object]:
    try:
        response = await ledger_http.post(path, json=body)
    except httpx.HTTPError as exc:
        raise LedgerUnavailable(str(exc)) from exc
    if 400 <= response.status_code < 500:
        try:
            detail: object = response.json()
        except ValueError:
            detail = response.text
        raise LedgerRejected(response.status_code, detail)
    if response.status_code >= 500:
        raise LedgerUnavailable(f"ledger returned {response.status_code}")
    value = response.json()
    if not isinstance(value, dict):
        raise LedgerUnavailable("ledger returned a non-object response")
    return cast(dict[str, object], value)


async def ledger_available(ledger_http: httpx.AsyncClient, account_id: str) -> int:
    try:
        response = await ledger_http.get(f"/v1/accounts/{account_id}/balance")
        response.raise_for_status()
        return int(response.json()["available_minor"])
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise LedgerUnavailable("balance lookup failed") from exc


def entry_request(key: str, lines: list[dict[str, object]]) -> dict[str, object]:
    return {"idempotency_key": key, "postings": lines}


@asynccontextmanager
async def merchant_money_lock(
    pool: asyncpg.Pool, merchant_id: str, *, wait_seconds: float = 10.0
) -> AsyncIterator[None]:
    """Session advisory lock; released on exit or automatically if the process dies."""
    connection = await pool.acquire()
    key = f"merchant-money:{merchant_id}"
    acquired = False
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds
        while True:
            acquired = bool(
                await connection.fetchval(
                    "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", key
                )
            )
            if acquired:
                break
            if loop.time() >= deadline:
                raise MerchantBusy("merchant money lock is busy")
            await asyncio.sleep(0.02)
        yield
    finally:
        try:
            if acquired:
                await connection.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0))", key)
        finally:
            await pool.release(connection)


async def insert_command(
    connection: asyncpg.Connection,
    *,
    subject_type: str,
    subject_id: UUID,
    merchant_id: str,
    kind: str,
    key: str,
    request: dict[str, object],
    operation: str = "post_entry",
    payment_id: UUID | None = None,
) -> None:
    await connection.execute(
        """INSERT INTO core_ledger_commands(
               payment_id, merchant_id, operation, idempotency_key, request,
               subject_type, subject_id, kind
           ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)""",
        payment_id,
        merchant_id,
        operation,
        key,
        json.dumps(request, separators=(",", ":")),
        subject_type,
        subject_id,
        kind,
    )


async def finish_command(
    connection: asyncpg.Connection, key: str, result: object, *, state: str = "completed"
) -> bool:
    """Mark a pending command finished; False if another worker already did."""
    updated = await connection.fetchval(
        """UPDATE core_ledger_commands SET state = $2, result = $3::jsonb,
               completed_at = clock_timestamp()
           WHERE idempotency_key = $1 AND state = 'pending' RETURNING true""",
        key,
        state,
        json.dumps(result, separators=(",", ":"), default=str),
    )
    return bool(updated)


async def append_outbox(
    connection: asyncpg.Connection,
    aggregate_type: str,
    aggregate_id: UUID,
    merchant_id: str,
    event_type: str,
    payload: dict[str, object],
) -> None:
    await connection.execute(
        """INSERT INTO core_outbox(aggregate_id, aggregate_type, merchant_id, event_type, payload)
           VALUES ($1, $2, $3, $4, $5::jsonb)""",
        aggregate_id,
        aggregate_type,
        merchant_id,
        event_type,
        json.dumps(payload, separators=(",", ":"), default=str),
    )


CompletionHandler = Callable[
    [asyncpg.Connection, asyncpg.Record, dict[str, object] | None, bool], Awaitable[None]
]


@dataclass(slots=True)
class MoneyContext:
    pool: asyncpg.Pool
    ledger_http: httpx.AsyncClient
    handlers: dict[str, CompletionHandler]
    faults: Any = None


async def execute_command(
    ctx: MoneyContext, command: asyncpg.Record | dict[str, Any]
) -> dict[str, object] | None:
    """Post one stored command and apply its completion; returns None if it was rejected."""
    request = command["request"]
    if isinstance(request, str):
        request = json.loads(request)
    handler = ctx.handlers[str(command["kind"])]
    try:
        result = await post_ledger(ctx.ledger_http, "/v1/entries", cast(dict[str, object], request))
    except LedgerRejected as exc:
        async with ctx.pool.acquire() as connection, connection.transaction():
            if await finish_command(
                connection,
                command["idempotency_key"],
                {"rejected": exc.status_code, "detail": exc.detail},
                state="rejected",
            ):
                await handler(connection, cast(asyncpg.Record, command), None, True)
        return None
    fault_point(ctx.faults, f"{command['kind']}.after_ledger_posted")
    async with ctx.pool.acquire() as connection, connection.transaction():
        if await finish_command(connection, command["idempotency_key"], result):
            await handler(connection, cast(asyncpg.Record, command), result, False)
    return result


async def resolve_pending(ctx: MoneyContext, merchant_id: str) -> int:
    """Replay this merchant's unresolved non-payment commands in creation order."""
    rows = await ctx.pool.fetch(
        """SELECT * FROM core_ledger_commands
           WHERE merchant_id = $1 AND state = 'pending' AND subject_type <> 'payment'
           ORDER BY created_at""",
        merchant_id,
    )
    for row in rows:
        await execute_command(ctx, row)
    return len(rows)


async def merchants_with_stale_commands(pool: asyncpg.Pool, stale_seconds: float) -> list[str]:
    rows = await pool.fetch(
        """SELECT DISTINCT merchant_id FROM core_ledger_commands
           WHERE state = 'pending' AND subject_type <> 'payment'
             AND created_at <= clock_timestamp() - make_interval(secs => $1)""",
        stale_seconds,
    )
    return [str(row["merchant_id"]) for row in rows]


async def sweep_money_commands(ctx: MoneyContext, *, stale_seconds: float = 30) -> int:
    """Resume money commands abandoned by a crashed request (lock-protected per merchant)."""
    resolved = 0
    for merchant_id in await merchants_with_stale_commands(ctx.pool, stale_seconds):
        try:
            async with merchant_money_lock(ctx.pool, merchant_id, wait_seconds=0.5):
                resolved += await resolve_pending(ctx, merchant_id)
        except (MerchantBusy, LedgerUnavailable):
            continue
    return resolved
