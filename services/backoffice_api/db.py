"""Database access with the least-privileged role for the caller.

Merchant users run as ``tally_app`` with ``app.merchant_id`` set, so row-level security limits
every read to their tenant even if a query forgets a filter. Platform users run as ``tally_ops``
(read-only, cross-tenant by design; every action is audited).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from services.backoffice_api.auth import Principal


@asynccontextmanager
async def scoped(pool: asyncpg.Pool, principal: Principal) -> AsyncIterator[asyncpg.Connection]:
    async with pool.acquire() as connection, connection.transaction():
        if principal.merchant_id is not None:
            await connection.execute("SET LOCAL ROLE tally_app")
            await connection.execute(
                "SELECT set_config('app.merchant_id', $1, true)", principal.merchant_id
            )
        else:
            await connection.execute("SET LOCAL ROLE tally_ops")
        yield connection


async def audit(
    connection: asyncpg.Connection,
    principal: Principal | str,
    action: str,
    subject: str,
    details: str = "{}",
    merchant_id: str | None = None,
) -> int:
    actor = principal if isinstance(principal, str) else principal.email
    tenant = merchant_id if isinstance(principal, str) else principal.merchant_id or merchant_id
    return int(
        await connection.fetchval(
            "SELECT audit_append($1, $2, $3, $4, $5::jsonb)",
            actor,
            action,
            subject,
            tenant,
            details,
        )
    )
