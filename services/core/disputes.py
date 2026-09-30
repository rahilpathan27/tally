"""Disputes and chargebacks.

When the network opens a dispute it pulls the funds immediately (the payer is re-credited), so the
merchant is debited at open: Dr merchant payable (what it covers) + Dr merchant receivable (rest),
Cr bank nostro. If the merchant wins, the network returns the funds: Dr bank nostro, Cr merchant
receivable (up to its balance) and Cr merchant payable for the rest. A lost dispute needs no
further entry. Fraud chargebacks become risk-model labels.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import asyncpg
from libs.money import MAX_SAFE_INTEGER
from pydantic import BaseModel, ConfigDict, Field

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

RESPONSE_WINDOW = timedelta(days=7)
MAX_EVIDENCE_BYTES = 1_000_000
ReasonCode = Literal[
    "fraudulent", "product_not_received", "duplicate", "credit_not_processed", "other"
]


class DisputeState(StrEnum):
    OPENING = "opening"
    NEEDS_RESPONSE = "needs_response"
    UNDER_REVIEW = "under_review"
    WON = "won"
    LOST = "lost"


DISPUTE_TRANSITIONS: dict[DisputeState, frozenset[DisputeState]] = {
    DisputeState.OPENING: frozenset({DisputeState.NEEDS_RESPONSE}),
    DisputeState.NEEDS_RESPONSE: frozenset(
        {DisputeState.UNDER_REVIEW, DisputeState.WON, DisputeState.LOST}
    ),
    DisputeState.UNDER_REVIEW: frozenset({DisputeState.WON, DisputeState.LOST}),
    DisputeState.WON: frozenset(),
    DisputeState.LOST: frozenset(),
}


class DisputeError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class OpenDispute(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    payment_id: UUID = Field(strict=False)
    amount_minor: Annotated[int, Field(strict=True, gt=0, le=MAX_SAFE_INTEGER)]
    reason_code: ReasonCode
    network_reference: Annotated[str, Field(min_length=4, max_length=100)]


class SubmitEvidence(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    text: Annotated[str, Field(min_length=1, max_length=20_000)]
    document_base64: Annotated[str, Field(max_length=1_400_000)] | None = None
    document_name: Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,100}$")] | None = None


class ResolveDispute(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    outcome: Literal["won", "lost"]


class DisputeView(BaseModel):
    dispute_id: UUID
    payment_id: UUID
    amount_minor: int
    currency: str
    reason_code: str
    status: str
    respond_by: str
    evidence_submitted: bool
    evidence_sha256: str | None
    created_at: str


def dispute_view(row: asyncpg.Record) -> DisputeView:
    return DisputeView(
        dispute_id=row["dispute_id"],
        payment_id=row["payment_id"],
        amount_minor=row["amount_minor"],
        currency=row["currency"].strip(),
        reason_code=row["reason_code"],
        status=row["status"],
        respond_by=row["respond_by"].isoformat(),
        evidence_submitted=row["evidence_text"] is not None,
        evidence_sha256=row["evidence_sha256"],
        created_at=row["created_at"].isoformat(),
    )


async def transition_dispute(
    connection: asyncpg.Connection,
    dispute_id: UUID,
    target: DisputeState,
    actor: str,
    reason: str,
    **columns: object,
) -> asyncpg.Record:
    row = await connection.fetchrow(
        "SELECT * FROM disputes WHERE dispute_id = $1 FOR UPDATE", dispute_id
    )
    if row is None:
        raise DisputeError(404, "DISPUTE_NOT_FOUND", "Dispute was not found.")
    current = DisputeState(row["status"])
    if target not in DISPUTE_TRANSITIONS[current]:
        raise DisputeError(409, "ILLEGAL_DISPUTE_TRANSITION", f"Dispute is {current.value}.")
    assignments = ["status = $2", "updated_at = clock_timestamp()"]
    values: list[object] = [dispute_id, target.value]
    for name, value in columns.items():
        values.append(value)
        assignments.append(f"{name} = ${len(values)}")
    if target in {DisputeState.WON, DisputeState.LOST}:
        assignments.append("resolved_at = clock_timestamp()")
    updated = await connection.fetchrow(
        f"UPDATE disputes SET {', '.join(assignments)} WHERE dispute_id = $1 RETURNING *", *values
    )
    await connection.execute(
        """INSERT INTO dispute_transitions(
               dispute_id, merchant_id, from_state, to_state, actor, reason
           ) VALUES ($1, $2, $3, $4, $5, $6)""",
        dispute_id,
        row["merchant_id"],
        current.value,
        target.value,
        actor,
        reason,
    )
    await append_outbox(
        connection,
        "dispute",
        dispute_id,
        row["merchant_id"],
        f"dispute.{target.value}",
        {
            "dispute_id": str(dispute_id),
            "payment_id": str(row["payment_id"]),
            "amount_minor": row["amount_minor"],
            "reason_code": row["reason_code"],
            "status": target.value,
        },
    )
    if target == DisputeState.LOST or (
        target == DisputeState.NEEDS_RESPONSE and row["reason_code"] == "fraudulent"
    ):
        await connection.execute(
            """INSERT INTO risk_labels(payment_id, merchant_id, label, source, source_id)
               VALUES ($1, $2, 'fraud', 'chargeback', $3)
               ON CONFLICT (source, source_id) DO NOTHING""",
            row["payment_id"],
            row["merchant_id"],
            str(dispute_id),
        )
    assert updated is not None
    return updated


async def _on_dispute_debit(
    connection: asyncpg.Connection,
    command: asyncpg.Record,
    result: dict[str, object] | None,
    rejected: bool,
) -> None:
    if rejected:
        raise RuntimeError("chargeback debit was rejected by the ledger")
    await transition_dispute(
        connection,
        command["subject_id"],
        DisputeState.NEEDS_RESPONSE,
        "card-network",
        "chargeback funds withdrawn from the merchant",
    )


async def _on_dispute_won(
    connection: asyncpg.Connection,
    command: asyncpg.Record,
    result: dict[str, object] | None,
    rejected: bool,
) -> None:
    if rejected:
        raise RuntimeError("dispute reversal was rejected by the ledger")
    request = command["request"]
    if isinstance(request, str):
        request = json.loads(request)
    payable_credit = sum(
        int(p["amount_minor"])
        for p in request["postings"]
        if p["direction"] == "credit" and str(p["account_id"]).endswith(":payable:INR")
    )
    await transition_dispute(
        connection,
        command["subject_id"],
        DisputeState.WON,
        "card-network",
        "dispute decided in the merchant's favour; funds returned",
        reversal_payable_credit_minor=payable_credit,
    )


DISPUTE_HANDLERS = {"dispute_debit": _on_dispute_debit, "dispute_won": _on_dispute_won}


async def open_dispute(ctx: MoneyContext, body: OpenDispute) -> asyncpg.Record:
    payment = await ctx.pool.fetchrow(
        "SELECT merchant_id, amount_minor, status FROM payment_intents WHERE payment_id = $1",
        body.payment_id,
    )
    if payment is None:
        raise DisputeError(404, "PAYMENT_NOT_FOUND", "Payment was not found.")
    merchant_id = str(payment["merchant_id"])
    existing = await ctx.pool.fetchrow(
        "SELECT * FROM disputes WHERE network_reference = $1", body.network_reference
    )
    if existing is not None:
        if existing["payment_id"] != body.payment_id:
            raise DisputeError(409, "DISPUTE_REFERENCE_REUSED", "Network reference conflicts.")
        return existing
    dispute_id = uuid4()
    key = f"{dispute_id}:dispute:debit"
    async with merchant_money_lock(ctx.pool, merchant_id):
        await resolve_pending(ctx, merchant_id)
        available = await ledger_available(ctx.ledger_http, f"merchant:{merchant_id}:payable:INR")
        async with ctx.pool.acquire() as connection, connection.transaction():
            locked = await connection.fetchrow(
                "SELECT status, amount_minor FROM payment_intents WHERE payment_id = $1 FOR UPDATE",
                body.payment_id,
            )
            assert locked is not None
            if locked["status"] != "succeeded":
                raise DisputeError(409, "PAYMENT_NOT_DISPUTABLE", "Payment did not succeed.")
            committed = await connection.fetchval(
                """SELECT coalesce((SELECT sum(amount_minor) FROM refunds WHERE payment_id = $1
                                     AND status NOT IN ('failed', 'cancelled')), 0)
                        + coalesce((SELECT sum(amount_minor) FROM disputes WHERE payment_id = $1
                                     AND status <> 'won'), 0)""",
                body.payment_id,
            )
            if body.amount_minor > int(locked["amount_minor"]) - int(committed):
                raise DisputeError(
                    422, "DISPUTE_EXCEEDS_PAYMENT", "Disputed amount exceeds what remains."
                )
            from_payable, from_receivable = split_debit(body.amount_minor, available)
            await connection.execute(
                """INSERT INTO disputes(
                       dispute_id, payment_id, merchant_id, amount_minor, currency, reason_code,
                       status, network_reference, payable_portion_minor,
                       receivable_portion_minor, respond_by
                   ) VALUES ($1, $2, $3, $4, 'INR', $5, 'opening', $6, $7, $8, $9)""",
                dispute_id,
                body.payment_id,
                merchant_id,
                body.amount_minor,
                body.reason_code,
                body.network_reference,
                from_payable,
                from_receivable,
                datetime.now(UTC) + RESPONSE_WINDOW,
            )
            await connection.execute(
                """INSERT INTO dispute_transitions(
                       dispute_id, merchant_id, from_state, to_state, actor, reason
                   ) VALUES ($1, $2, NULL, 'opening', 'card-network', 'chargeback received')""",
                dispute_id,
                merchant_id,
            )
            lines: list[dict[str, object]] = [
                {
                    "account_id": "bank:simulated:INR",
                    "direction": "credit",
                    "amount_minor": body.amount_minor,
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
            await insert_command(
                connection,
                subject_type="dispute",
                subject_id=dispute_id,
                merchant_id=merchant_id,
                kind="dispute_debit",
                key=key,
                request=entry_request(key, lines),
                payment_id=body.payment_id,
            )
        command = await ctx.pool.fetchrow(
            "SELECT * FROM core_ledger_commands WHERE idempotency_key = $1", key
        )
        try:
            await execute_command(ctx, command)
        except LedgerUnavailable:
            pass
    row = await ctx.pool.fetchrow("SELECT * FROM disputes WHERE dispute_id = $1", dispute_id)
    assert row is not None
    return row


async def submit_evidence(
    ctx: MoneyContext,
    merchant_id: str,
    dispute_id: UUID,
    body: SubmitEvidence,
    write_object: Callable[[str, bytes], Awaitable[Any]] | None,
) -> asyncpg.Record:
    object_key: str | None = None
    digest: str | None = None
    if body.document_base64 is not None:
        try:
            document = base64.b64decode(body.document_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise DisputeError(422, "INVALID_EVIDENCE", "Evidence must be base64.") from exc
        if len(document) > MAX_EVIDENCE_BYTES:
            raise DisputeError(413, "EVIDENCE_TOO_LARGE", "Evidence exceeds 1 MB.")
        if write_object is None:
            raise DisputeError(503, "EVIDENCE_STORE_UNAVAILABLE", "Evidence storage is offline.")
        digest = hashlib.sha256(document).hexdigest()
        object_key = f"disputes/{merchant_id}/{dispute_id}/{digest}-{body.document_name or 'doc'}"
        await write_object(object_key, document)
    async with ctx.pool.acquire() as connection, connection.transaction():
        await connection.execute("SELECT set_config('app.merchant_id', $1, true)", merchant_id)
        owner = await connection.fetchval(
            "SELECT merchant_id FROM disputes WHERE dispute_id = $1", dispute_id
        )
        if owner != merchant_id:
            raise DisputeError(404, "DISPUTE_NOT_FOUND", "Dispute was not found.")
        return await transition_dispute(
            connection,
            dispute_id,
            DisputeState.UNDER_REVIEW,
            merchant_id,
            "merchant submitted evidence",
            evidence_text=body.text,
            evidence_object_key=object_key,
            evidence_sha256=digest,
        )


async def resolve_dispute(
    ctx: MoneyContext, dispute_id: UUID, outcome: Literal["won", "lost"]
) -> asyncpg.Record:
    row = await ctx.pool.fetchrow("SELECT * FROM disputes WHERE dispute_id = $1", dispute_id)
    if row is None:
        raise DisputeError(404, "DISPUTE_NOT_FOUND", "Dispute was not found.")
    if outcome == "lost":
        async with ctx.pool.acquire() as connection, connection.transaction():
            return await transition_dispute(
                connection,
                dispute_id,
                DisputeState.LOST,
                "card-network",
                "dispute decided for the cardholder",
            )
    if DisputeState.WON not in DISPUTE_TRANSITIONS[DisputeState(row["status"])]:
        raise DisputeError(409, "ILLEGAL_DISPUTE_TRANSITION", f"Dispute is {row['status']}.")
    merchant_id = str(row["merchant_id"])
    key = f"{dispute_id}:dispute:won"
    async with merchant_money_lock(ctx.pool, merchant_id):
        await resolve_pending(ctx, merchant_id)
        if not await ctx.pool.fetchval(
            "SELECT 1 FROM core_ledger_commands WHERE idempotency_key = $1", key
        ):
            receivable = await ledger_available(
                ctx.ledger_http, f"merchant:{merchant_id}:receivable:INR"
            )
            amount = int(row["amount_minor"])
            to_receivable = min(int(row["receivable_portion_minor"]), max(receivable, 0))
            lines: list[dict[str, object]] = [
                {"account_id": "bank:simulated:INR", "direction": "debit", "amount_minor": amount}
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
                    subject_type="dispute",
                    subject_id=dispute_id,
                    merchant_id=merchant_id,
                    kind="dispute_won",
                    key=key,
                    request=entry_request(key, lines),
                    payment_id=row["payment_id"],
                )
        await resolve_pending(ctx, merchant_id)
    updated = await ctx.pool.fetchrow("SELECT * FROM disputes WHERE dispute_id = $1", dispute_id)
    assert updated is not None
    return updated
