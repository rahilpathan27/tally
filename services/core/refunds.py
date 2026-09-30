"""Full and partial refunds with over-refund protection and escalation.

Ledger treatment (all INR minor units):

* ``refund_debit``: Dr merchant payable (what payable covers) + Dr merchant receivable (the
  shortfall, recovered from later settlements), Cr ``platform:refunds_clearing`` (owed to payer).
* ``refund_complete`` (bank confirmed): Dr refunds clearing, Cr bank nostro.
* ``refund_cancel`` (ops gave up; maker-checker in the back office): Dr refunds clearing,
  Cr merchant receivable (up to its balance) and Cr merchant payable for the rest.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

import asyncpg
import httpx
from libs.money import MAX_SAFE_INTEGER
from pydantic import BaseModel, ConfigDict, Field

from services.core.faults import fault_point
from services.core.fees import split_debit
from services.core.money_ops import (
    LedgerUnavailable,
    MoneyContext,
    append_outbox,
    entry_request,
    execute_command,
    insert_command,
    ledger_available,
    merchant_money_lock,
    resolve_pending,
)

MAX_BANK_ATTEMPTS = 5


class RefundState(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    REQUIRES_ACTION = "requires_action"
    CANCELLED = "cancelled"
    FAILED = "failed"


REFUND_TRANSITIONS: dict[RefundState, frozenset[RefundState]] = {
    RefundState.PENDING: frozenset({RefundState.PROCESSING, RefundState.FAILED}),
    RefundState.PROCESSING: frozenset({RefundState.SUCCEEDED, RefundState.REQUIRES_ACTION}),
    RefundState.REQUIRES_ACTION: frozenset(
        {RefundState.PROCESSING, RefundState.CANCELLED, RefundState.SUCCEEDED}
    ),
    RefundState.SUCCEEDED: frozenset(),
    RefundState.CANCELLED: frozenset(),
    RefundState.FAILED: frozenset(),
}


class RefundError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class CreateRefund(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    payment_id: UUID = Field(strict=False)
    amount_minor: Annotated[int, Field(strict=True, gt=0, le=MAX_SAFE_INTEGER)] | None = None
    reason: Annotated[str, Field(max_length=200)] | None = None


class RefundResponse(BaseModel):
    refund_id: UUID
    payment_id: UUID
    amount_minor: int
    currency: str
    status: str
    reason: str | None
    payable_portion_minor: int
    receivable_portion_minor: int
    failure_reason: str | None
    created_at: str


def refund_response(row: asyncpg.Record) -> RefundResponse:
    return RefundResponse(
        refund_id=row["refund_id"],
        payment_id=row["payment_id"],
        amount_minor=row["amount_minor"],
        currency=row["currency"].strip(),
        status=row["status"],
        reason=row["reason"],
        payable_portion_minor=row["payable_portion_minor"],
        receivable_portion_minor=row["receivable_portion_minor"],
        failure_reason=row["failure_reason"],
        created_at=row["created_at"].isoformat(),
    )


async def transition_refund(
    connection: asyncpg.Connection,
    refund_id: UUID,
    target: RefundState,
    actor: str,
    reason: str,
    **columns: object,
) -> asyncpg.Record:
    row = await connection.fetchrow(
        "SELECT * FROM refunds WHERE refund_id = $1 FOR UPDATE", refund_id
    )
    if row is None:
        raise RefundError(404, "REFUND_NOT_FOUND", "Refund was not found.")
    current = RefundState(row["status"])
    if target not in REFUND_TRANSITIONS[current]:
        raise RefundError(409, "ILLEGAL_REFUND_TRANSITION", f"Refund is {current.value}.")
    assignments = ["status = $2", "updated_at = clock_timestamp()"]
    values: list[object] = [refund_id, target.value]
    for name, value in columns.items():
        values.append(value)
        assignments.append(f"{name} = ${len(values)}")
    if target == RefundState.SUCCEEDED:
        assignments.append("succeeded_at = clock_timestamp()")
    updated = await connection.fetchrow(
        f"UPDATE refunds SET {', '.join(assignments)} WHERE refund_id = $1 RETURNING *", *values
    )
    await connection.execute(
        """INSERT INTO refund_transitions(
               refund_id, merchant_id, from_state, to_state, actor, reason
           ) VALUES ($1, $2, $3, $4, $5, $6)""",
        refund_id,
        row["merchant_id"],
        current.value,
        target.value,
        actor,
        reason,
    )
    await append_outbox(
        connection,
        "refund",
        refund_id,
        row["merchant_id"],
        f"refund.{target.value}",
        {
            "refund_id": str(refund_id),
            "payment_id": str(row["payment_id"]),
            "merchant_id": row["merchant_id"],
            "amount_minor": row["amount_minor"],
            "status": target.value,
        },
    )
    assert updated is not None
    return updated


# Completion handlers run in the same transaction that marks the ledger command completed.
async def _on_refund_debit(
    connection: asyncpg.Connection,
    command: asyncpg.Record,
    result: dict[str, object] | None,
    rejected: bool,
) -> None:
    if rejected:
        await transition_refund(
            connection,
            command["subject_id"],
            RefundState.FAILED,
            "ledger",
            "ledger rejected the refund debit",
            failure_reason="ledger_rejected",
        )
        return
    await transition_refund(
        connection,
        command["subject_id"],
        RefundState.PROCESSING,
        "ledger",
        "refund amount moved to refunds clearing",
        next_attempt_at=datetime.now(UTC),
    )


async def _on_refund_complete(
    connection: asyncpg.Connection,
    command: asyncpg.Record,
    result: dict[str, object] | None,
    rejected: bool,
) -> None:
    if rejected:
        raise RuntimeError("refunds clearing could not settle a bank-confirmed refund")
    await transition_refund(
        connection,
        command["subject_id"],
        RefundState.SUCCEEDED,
        "bank-simulator",
        "bank confirmed the refund credit to the payer",
    )


async def _on_refund_cancel(
    connection: asyncpg.Connection,
    command: asyncpg.Record,
    result: dict[str, object] | None,
    rejected: bool,
) -> None:
    if rejected:
        raise RuntimeError("refund cancellation was rejected by the ledger")
    request = command["request"]
    if isinstance(request, str):
        request = json.loads(request)
    payable_credit = sum(
        int(p["amount_minor"])
        for p in request["postings"]
        if p["direction"] == "credit" and str(p["account_id"]).endswith(":payable:INR")
    )
    await transition_refund(
        connection,
        command["subject_id"],
        RefundState.CANCELLED,
        "ops",
        "refund cancelled; funds returned to the merchant",
        reversal_payable_credit_minor=payable_credit,
    )


REFUND_HANDLERS = {
    "refund_debit": _on_refund_debit,
    "refund_complete": _on_refund_complete,
    "refund_cancel": _on_refund_cancel,
}


async def create_refund(
    ctx: MoneyContext, merchant_id: str, body: CreateRefund, actor: str
) -> asyncpg.Record:
    refund_id = uuid4()
    async with merchant_money_lock(ctx.pool, merchant_id):
        await resolve_pending(ctx, merchant_id)
        available = await ledger_available(ctx.ledger_http, f"merchant:{merchant_id}:payable:INR")
        async with ctx.pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT set_config('app.merchant_id', $1, true)", merchant_id)
            payment = await connection.fetchrow(
                """SELECT payment_id, amount_minor, currency, status FROM payment_intents
                   WHERE payment_id = $1 AND merchant_id = $2 FOR UPDATE""",
                body.payment_id,
                merchant_id,
            )
            if payment is None:
                raise RefundError(404, "PAYMENT_NOT_FOUND", "Payment was not found.")
            if payment["status"] != "succeeded":
                raise RefundError(
                    409, "PAYMENT_NOT_REFUNDABLE", "Only succeeded payments can be refunded."
                )
            committed = await connection.fetchval(
                """SELECT coalesce((SELECT sum(amount_minor) FROM refunds WHERE payment_id = $1
                                     AND status NOT IN ('failed', 'cancelled')), 0)
                        + coalesce((SELECT sum(amount_minor) FROM disputes WHERE payment_id = $1
                                     AND status <> 'won'), 0)""",
                body.payment_id,
            )
            refundable = int(payment["amount_minor"]) - int(committed)
            amount = body.amount_minor if body.amount_minor is not None else refundable
            if amount <= 0 or amount > refundable:
                raise RefundError(
                    422,
                    "REFUND_EXCEEDS_REFUNDABLE",
                    f"At most {max(refundable, 0)} minor units can be refunded.",
                )
            from_payable, from_receivable = split_debit(amount, available)
            await connection.execute(
                """INSERT INTO refunds(
                       refund_id, payment_id, merchant_id, amount_minor, currency, status, reason,
                       payable_portion_minor, receivable_portion_minor
                   ) VALUES ($1, $2, $3, $4, 'INR', 'pending', $5, $6, $7)""",
                refund_id,
                body.payment_id,
                merchant_id,
                amount,
                body.reason,
                from_payable,
                from_receivable,
            )
            await connection.execute(
                """INSERT INTO refund_transitions(
                       refund_id, merchant_id, from_state, to_state, actor, reason
                   ) VALUES ($1, $2, NULL, 'pending', $3, 'refund requested')""",
                refund_id,
                merchant_id,
                actor,
            )
            await append_outbox(
                connection,
                "refund",
                refund_id,
                merchant_id,
                "refund.created",
                {
                    "refund_id": str(refund_id),
                    "payment_id": str(body.payment_id),
                    "amount_minor": amount,
                    "status": "pending",
                },
            )
            lines: list[dict[str, object]] = [
                {
                    "account_id": "platform:refunds_clearing:INR",
                    "direction": "credit",
                    "amount_minor": amount,
                }
            ]
            if from_payable:
                lines.append(
                    {
                        "account_id": f"merchant:{merchant_id}:payable:INR",
                        "direction": "debit",
                        "amount_minor": from_payable,
                    }
                )
            if from_receivable:
                lines.append(
                    {
                        "account_id": f"merchant:{merchant_id}:receivable:INR",
                        "direction": "debit",
                        "amount_minor": from_receivable,
                    }
                )
            key = f"{refund_id}:refund:debit"
            await insert_command(
                connection,
                subject_type="refund",
                subject_id=refund_id,
                merchant_id=merchant_id,
                kind="refund_debit",
                key=key,
                request=entry_request(key, lines),
                payment_id=body.payment_id,
            )
        fault_point(ctx.faults, "refund.after_command_committed")
        command = await ctx.pool.fetchrow(
            "SELECT * FROM core_ledger_commands WHERE idempotency_key = $1", key
        )
        try:
            await execute_command(ctx, command)
        except LedgerUnavailable:
            pass  # Stays pending; the next money operation or the sweeper replays it.
    row = await ctx.pool.fetchrow("SELECT * FROM refunds WHERE refund_id = $1", refund_id)
    assert row is not None
    return row


async def claim_due_refunds(pool: asyncpg.Pool, limit: int = 50) -> list[asyncpg.Record]:
    async with pool.acquire() as connection, connection.transaction():
        rows = await connection.fetch(
            """SELECT * FROM refunds
               WHERE status = 'processing' AND next_attempt_at <= clock_timestamp()
                 AND (lease_until IS NULL OR lease_until <= clock_timestamp())
               ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED LIMIT $1""",
            limit,
        )
        if rows:
            await connection.execute(
                """UPDATE refunds SET lease_until = clock_timestamp() + interval '30 seconds'
                   WHERE refund_id = ANY($1::uuid[])""",
                [row["refund_id"] for row in rows],
            )
        return cast(list[asyncpg.Record], rows)


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(3600, 2 ** min(attempts, 11)))


async def process_refund(ctx: MoneyContext, bank_http: httpx.AsyncClient, row: Any) -> str:
    """Send one refund attempt to the payer's bank and apply the outcome."""
    refund_id: UUID = row["refund_id"]
    attempt = int(row["bank_attempts"])
    try:
        response = await bank_http.post(
            "/v1/refunds",
            json={
                "refund_id": str(refund_id),
                "payment_id": str(row["payment_id"]),
                "amount_minor": int(row["amount_minor"]),
                "currency": "INR",
            },
            headers={"Idempotency-Key": f"{refund_id}:refund:bank:{attempt}"},
        )
        response.raise_for_status()
        outcome = str(response.json().get("status"))
    except (httpx.HTTPError, ValueError):
        # Unknown: retry the same attempt key so the bank replays its original decision.
        await ctx.pool.execute(
            """UPDATE refunds SET next_attempt_at = clock_timestamp() + $2::interval,
                   lease_until = NULL WHERE refund_id = $1""",
            refund_id,
            _backoff(attempt),
        )
        return "unknown"
    fault_point(ctx.faults, "refund.after_bank_sent")
    if outcome == "succeeded":
        key = f"{refund_id}:refund:complete"
        async with ctx.pool.acquire() as connection, connection.transaction():
            await insert_command(
                connection,
                subject_type="refund",
                subject_id=refund_id,
                merchant_id=row["merchant_id"],
                kind="refund_complete",
                key=key,
                request=entry_request(
                    key,
                    [
                        {
                            "account_id": "platform:refunds_clearing:INR",
                            "direction": "debit",
                            "amount_minor": int(row["amount_minor"]),
                        },
                        {
                            "account_id": "bank:simulated:INR",
                            "direction": "credit",
                            "amount_minor": int(row["amount_minor"]),
                        },
                    ],
                ),
                payment_id=row["payment_id"],
            )
        command = await ctx.pool.fetchrow(
            "SELECT * FROM core_ledger_commands WHERE idempotency_key = $1", key
        )
        try:
            await execute_command(ctx, command)
        except LedgerUnavailable:
            return "pending_ledger"
        return "succeeded"
    attempt += 1
    async with ctx.pool.acquire() as connection, connection.transaction():
        if attempt >= MAX_BANK_ATTEMPTS:
            await transition_refund(
                connection,
                refund_id,
                RefundState.REQUIRES_ACTION,
                "refund-worker",
                f"bank declined the refund {attempt} times; manual action required",
                bank_attempts=attempt,
                failure_reason="bank_declined",
                lease_until=None,
            )
            return "requires_action"
        await connection.execute(
            """UPDATE refunds SET bank_attempts = $2, lease_until = NULL,
                   next_attempt_at = clock_timestamp() + $3::interval,
                   failure_reason = 'bank_declined', updated_at = clock_timestamp()
               WHERE refund_id = $1""",
            refund_id,
            attempt,
            _backoff(attempt),
        )
    return "retry"


async def refund_worker_batch(
    ctx: MoneyContext, bank_http: httpx.AsyncClient, limit: int = 50
) -> int:
    rows = await claim_due_refunds(ctx.pool, limit)
    for row in rows:
        await process_refund(ctx, bank_http, row)
    return len(rows)


async def retry_refund(ctx: MoneyContext, refund_id: UUID, actor: str) -> asyncpg.Record:
    async with ctx.pool.acquire() as connection, connection.transaction():
        return await transition_refund(
            connection,
            refund_id,
            RefundState.PROCESSING,
            actor,
            "manual retry approved",
            bank_attempts=0,
            next_attempt_at=datetime.now(UTC),
            failure_reason=None,
        )


async def cancel_refund(ctx: MoneyContext, refund_id: UUID, actor: str) -> asyncpg.Record:
    """Return an unrecoverable refund's funds to the merchant (receivable first, then payable)."""
    row = await ctx.pool.fetchrow("SELECT * FROM refunds WHERE refund_id = $1", refund_id)
    if row is None:
        raise RefundError(404, "REFUND_NOT_FOUND", "Refund was not found.")
    if row["status"] != RefundState.REQUIRES_ACTION.value:
        raise RefundError(409, "ILLEGAL_REFUND_TRANSITION", "Only escalated refunds can cancel.")
    merchant_id = str(row["merchant_id"])
    key = f"{refund_id}:refund:cancel"
    async with merchant_money_lock(ctx.pool, merchant_id):
        await resolve_pending(ctx, merchant_id)
        existing = await ctx.pool.fetchval(
            "SELECT 1 FROM core_ledger_commands WHERE idempotency_key = $1", key
        )
        if not existing:
            receivable = await ledger_available(
                ctx.ledger_http, f"merchant:{merchant_id}:receivable:INR"
            )
            amount = int(row["amount_minor"])
            to_receivable = min(int(row["receivable_portion_minor"]), max(receivable, 0))
            lines: list[dict[str, object]] = [
                {
                    "account_id": "platform:refunds_clearing:INR",
                    "direction": "debit",
                    "amount_minor": amount,
                }
            ]
            if to_receivable:
                lines.append(
                    {
                        "account_id": f"merchant:{merchant_id}:receivable:INR",
                        "direction": "credit",
                        "amount_minor": to_receivable,
                    }
                )
            if amount - to_receivable:
                lines.append(
                    {
                        "account_id": f"merchant:{merchant_id}:payable:INR",
                        "direction": "credit",
                        "amount_minor": amount - to_receivable,
                    }
                )
            async with ctx.pool.acquire() as connection, connection.transaction():
                await insert_command(
                    connection,
                    subject_type="refund",
                    subject_id=refund_id,
                    merchant_id=merchant_id,
                    kind="refund_cancel",
                    key=key,
                    request=entry_request(key, lines),
                    payment_id=row["payment_id"],
                )
            await resolve_pending(ctx, merchant_id)
    updated = await ctx.pool.fetchrow("SELECT * FROM refunds WHERE refund_id = $1", refund_id)
    assert updated is not None
    return updated
