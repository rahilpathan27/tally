"""PostgreSQL-backed, merchant-scoped idempotency reservations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import asyncpg


@dataclass(frozen=True, slots=True)
class IdempotencyResult:
    outcome: Literal["started", "in_progress", "replay"]
    response_status: int | None = None
    response_body: dict[str, object] | list[object] | None = None


class IdempotencyPayloadConflict(ValueError):
    """The same merchant key was reused with a different request fingerprint."""


def request_fingerprint(method: str, path: str, body: bytes) -> bytes:
    if not method or not path.startswith("/"):
        raise ValueError("method and absolute path are required")
    material = b"\n".join((method.upper().encode(), path.encode(), hashlib.sha256(body).digest()))
    return hashlib.sha256(material).digest()


class PostgresIdempotencyStore:
    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        retention: timedelta = timedelta(hours=24),
        lease: timedelta = timedelta(seconds=60),
    ) -> None:
        if retention <= timedelta(0):
            raise ValueError("retention must be positive")
        if not timedelta(seconds=1) <= lease <= timedelta(hours=1):
            raise ValueError("lease must be between one second and one hour")
        self._pool = pool
        self._retention = retention
        self._lease_seconds = int(lease.total_seconds())

    async def begin(self, merchant_id: str, key: str, fingerprint: bytes) -> IdempotencyResult:
        if len(fingerprint) != 32:
            raise ValueError("fingerprint must be a SHA-256 digest")
        try:
            async with self._pool.acquire() as connection, connection.transaction():
                await connection.execute(
                    "SELECT set_config('app.merchant_id', $1, true)", merchant_id
                )
                row = await connection.fetchrow(
                    """SELECT outcome, response_status, response_body::text AS response_body
                         FROM gateway_begin_idempotency($1, $2, $3, $4, $5)""",
                    merchant_id,
                    key,
                    fingerprint,
                    datetime.now(UTC) + self._retention,
                    self._lease_seconds,
                )
        except asyncpg.UniqueViolationError as exc:
            raise IdempotencyPayloadConflict("idempotency key payload mismatch") from exc
        outcome = str(row["outcome"])
        if outcome == "replay":
            body = json.loads(row["response_body"])
            if not isinstance(body, (dict, list)):
                raise ValueError("stored idempotency response must be a JSON object or array")
            return IdempotencyResult("replay", int(row["response_status"]), body)
        if outcome == "started":
            return IdempotencyResult("started")
        if outcome == "in_progress":
            return IdempotencyResult("in_progress")
        raise RuntimeError("database returned an unknown idempotency outcome")

    async def complete(
        self,
        merchant_id: str,
        key: str,
        fingerprint: bytes,
        response_status: int,
        response_body: dict[str, object] | list[object],
    ) -> None:
        if not 100 <= response_status <= 599:
            raise ValueError("response status must be an HTTP status code")
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT set_config('app.merchant_id', $1, true)", merchant_id)
            await connection.execute(
                "SELECT gateway_complete_idempotency($1, $2, $3, $4, $5::jsonb)",
                merchant_id,
                key,
                fingerprint,
                response_status,
                json.dumps(response_body, separators=(",", ":")),
            )

    async def release(self, merchant_id: str, key: str, fingerprint: bytes) -> None:
        """Drop an in-progress reservation so a retry after a 5xx can run again."""
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT set_config('app.merchant_id', $1, true)", merchant_id)
            await connection.execute(
                "SELECT gateway_release_idempotency($1, $2, $3)", merchant_id, key, fingerprint
            )
