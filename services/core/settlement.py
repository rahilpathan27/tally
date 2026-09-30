"""T+N merchant settlement, reserve handling and payouts.

A settlement for ``(merchant, business_date)`` gathers every payable-affecting event that has
committed in the ledger and has not been settled yet (``settlement_items`` has a primary key on
``(item_type, item_id)``, so an event can be settled only once):

* succeeded payments whose IST business date is on or before ``business_date - delay``;
* refund and dispute debits taken from payable; refund cancellations, dispute wins and payout
  returns credited back to payable; reserve holds whose release date has arrived.

``fees.compute_settlement`` nets them so merchant payable drops by exactly their contribution.
Because refund/dispute debits and settlements run under the same merchant lock, after posting the
payable balance equals the sales that are not yet eligible, which the integration tests assert.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import asyncpg
import httpx
from libs.common.business_time import cutoff_instant, last_closed_business_date
from libs.money import Currency
from libs.observability.metrics import PAYOUTS_RETURNED, SETTLEMENT_FAILURES
from pydantic import BaseModel

from services.core.faults import fault_point
from services.core.fees import (
    FeeSchedule,
    SettlementInputs,
    compute_settlement,
    entry_lines_total,
    payment_fee,
    settlement_postings,
)
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

DEFAULT_CONFIG: dict[str, Any] = {
    "settlement_delay_days": 2,
    "cutoff_time": None,
    "fee_rate": Decimal("0.020000"),
    "fixed_fee_minor": 0,
    "gst_rate": Decimal("0.1800"),
    "reserve_rate": Decimal("0.000000"),
    "reserve_hold_days": 30,
}
ObjectWriter = Callable[[str, bytes], Any]


class SettlementItemView(BaseModel):
    item_type: str
    item_id: UUID
    amount_minor: int
    fee_minor: int


class PayoutView(BaseModel):
    payout_id: UUID
    amount_minor: int
    status: str
    bank_reference: str | None
    instruction_object_key: str | None
    instruction_sha256: str | None


class SettlementView(BaseModel):
    settlement_id: UUID
    business_date: str
    currency: str
    status: str
    gross_minor: int
    payable_debits_minor: int
    payable_credits_minor: int
    fee_minor: int
    gst_minor: int
    reserve_held_minor: int
    reserve_released_minor: int
    recovered_minor: int
    shortfall_minor: int
    net_payout_minor: int
    fee_rate: str
    gst_rate: str
    reserve_rate: str
    ledger_entry_id: int | None
    items: list[SettlementItemView] | None = None
    payout: PayoutView | None = None


def settlement_view(
    row: asyncpg.Record,
    items: list[asyncpg.Record] | None = None,
    payout: asyncpg.Record | None = None,
) -> SettlementView:
    return SettlementView(
        settlement_id=row["settlement_id"],
        business_date=row["business_date"].isoformat(),
        currency=row["currency"].strip(),
        status=row["status"],
        gross_minor=row["gross_minor"],
        payable_debits_minor=row["payable_debits_minor"],
        payable_credits_minor=row["payable_credits_minor"],
        fee_minor=row["fee_minor"],
        gst_minor=row["gst_minor"],
        reserve_held_minor=row["reserve_held_minor"],
        reserve_released_minor=row["reserve_released_minor"],
        recovered_minor=row["recovered_minor"],
        shortfall_minor=row["shortfall_minor"],
        net_payout_minor=row["net_payout_minor"],
        fee_rate=str(row["fee_rate"]),
        gst_rate=str(row["gst_rate"]),
        reserve_rate=str(row["reserve_rate"]),
        ledger_entry_id=row["ledger_entry_id"],
        items=None
        if items is None
        else [
            SettlementItemView(
                item_type=item["item_type"],
                item_id=item["item_id"],
                amount_minor=item["amount_minor"],
                fee_minor=item["fee_minor"],
            )
            for item in items
        ],
        payout=None
        if payout is None
        else PayoutView(
            payout_id=payout["payout_id"],
            amount_minor=payout["amount_minor"],
            status=payout["status"],
            bank_reference=payout["bank_reference"],
            instruction_object_key=payout["instruction_object_key"],
            instruction_sha256=payout["instruction_sha256"],
        ),
    )


async def merchant_config(connection: asyncpg.Connection, merchant_id: str) -> dict[str, Any]:
    row = await connection.fetchrow(
        "SELECT * FROM merchant_settlement_configs WHERE merchant_id = $1", merchant_id
    )
    config = dict(DEFAULT_CONFIG)
    if row is not None:
        config.update({key: row[key] for key in DEFAULT_CONFIG})
    return config


# Ledger completion handlers ------------------------------------------------------------------
async def _on_settlement_post(
    connection: asyncpg.Connection,
    command: asyncpg.Record,
    result: dict[str, object] | None,
    rejected: bool,
) -> None:
    settlement_id: UUID = command["subject_id"]
    if rejected:
        # Nothing reached the ledger; free the items so the next run recomputes them.
        await connection.execute(
            "DELETE FROM settlement_items WHERE settlement_id = $1", settlement_id
        )
        await connection.execute("DELETE FROM settlements WHERE settlement_id = $1", settlement_id)
        return
    assert result is not None
    row = await connection.fetchrow(
        """UPDATE settlements SET status = 'posted', posted_at = clock_timestamp(),
               ledger_entry_id = $2
           WHERE settlement_id = $1 AND status = 'computed' RETURNING *""",
        settlement_id,
        int(str(result["entry_id"])),
    )
    if row is None:
        return
    if row["net_payout_minor"] > 0:
        await connection.execute(
            """INSERT INTO payouts(payout_id, settlement_id, merchant_id, amount_minor, currency,
                                   status)
               VALUES ($1, $2, $3, $4, 'INR', 'pending')""",
            uuid4(),
            settlement_id,
            row["merchant_id"],
            row["net_payout_minor"],
        )
    await append_outbox(
        connection,
        "settlement",
        settlement_id,
        row["merchant_id"],
        "settlement.posted",
        {
            "settlement_id": str(settlement_id),
            "business_date": row["business_date"].isoformat(),
            "net_payout_minor": row["net_payout_minor"],
            "fee_minor": row["fee_minor"],
            "gst_minor": row["gst_minor"],
        },
    )


async def _on_payout(
    connection: asyncpg.Connection,
    command: asyncpg.Record,
    result: dict[str, object] | None,
    rejected: bool,
) -> None:
    if rejected:
        raise RuntimeError("payout ledger command was rejected")
    status = "paid" if command["kind"] == "payout_complete" else "returned"
    row = await connection.fetchrow(
        """UPDATE payouts SET status = $2, resolved_at = clock_timestamp(),
               updated_at = clock_timestamp(), lease_until = NULL
           WHERE payout_id = $1 AND status IN ('pending', 'sent') RETURNING *""",
        command["subject_id"],
        status,
    )
    if row is not None:
        await append_outbox(
            connection,
            "payout",
            row["payout_id"],
            row["merchant_id"],
            f"payout.{status}",
            {
                "payout_id": str(row["payout_id"]),
                "settlement_id": str(row["settlement_id"]),
                "amount_minor": row["amount_minor"],
                "status": status,
            },
        )


SETTLEMENT_HANDLERS = {
    "settlement_post": _on_settlement_post,
    "payout_complete": _on_payout,
    "payout_return": _on_payout,
}


async def run_settlement(
    ctx: MoneyContext, merchant_id: str, business_date: date
) -> asyncpg.Record:
    """Compute and post one merchant's settlement for an IST business date (idempotent)."""
    async with merchant_money_lock(ctx.pool, merchant_id):
        await resolve_pending(ctx, merchant_id)
        existing = await ctx.pool.fetchrow(
            "SELECT * FROM settlements WHERE merchant_id = $1 AND business_date = $2",
            merchant_id,
            business_date,
        )
        if existing is not None:
            return existing
        receivable = await ledger_available(
            ctx.ledger_http, f"merchant:{merchant_id}:receivable:INR"
        )
        settlement_id = uuid4()
        key = f"settlement:{merchant_id}:{business_date.isoformat()}:post"
        async with ctx.pool.acquire() as connection, connection.transaction():
            config = await merchant_config(connection, merchant_id)
            cutoff = config["cutoff_time"] or time(0, 0)
            window_date = business_date - timedelta(days=int(config["settlement_delay_days"]))
            window_end = cutoff_instant(window_date, cutoff)
            schedule = FeeSchedule(
                fee_rate=Decimal(config["fee_rate"]),
                fixed_fee_minor=int(config["fixed_fee_minor"]),
                gst_rate=Decimal(config["gst_rate"]),
                reserve_rate=Decimal(config["reserve_rate"]),
            )
            payments = await connection.fetch(
                """SELECT p.payment_id, p.amount_minor FROM payment_intents p
                   WHERE p.merchant_id = $1 AND p.status = 'succeeded'
                     AND p.succeeded_at < $2
                     AND NOT EXISTS (SELECT 1 FROM settlement_items i
                                     WHERE i.item_type = 'payment' AND i.item_id = p.payment_id)
                   ORDER BY p.succeeded_at, p.payment_id""",
                merchant_id,
                window_end,
            )
            debits = await connection.fetch(
                """SELECT 'refund_debit' AS item_type, refund_id AS item_id,
                          payable_portion_minor AS amount
                   FROM refunds r WHERE merchant_id = $1 AND payable_portion_minor > 0
                     AND status IN ('processing', 'succeeded', 'requires_action', 'cancelled')
                     AND NOT EXISTS (SELECT 1 FROM settlement_items i
                         WHERE i.item_type = 'refund_debit' AND i.item_id = r.refund_id)
                   UNION ALL
                   SELECT 'dispute_debit', dispute_id, payable_portion_minor
                   FROM disputes d WHERE merchant_id = $1 AND payable_portion_minor > 0
                     AND status <> 'opening'
                     AND NOT EXISTS (SELECT 1 FROM settlement_items i
                         WHERE i.item_type = 'dispute_debit' AND i.item_id = d.dispute_id)""",
                merchant_id,
            )
            credits = await connection.fetch(
                """SELECT 'refund_cancel' AS item_type, refund_id AS item_id,
                          reversal_payable_credit_minor AS amount
                   FROM refunds r WHERE merchant_id = $1 AND status = 'cancelled'
                     AND reversal_payable_credit_minor > 0
                     AND NOT EXISTS (SELECT 1 FROM settlement_items i
                         WHERE i.item_type = 'refund_cancel' AND i.item_id = r.refund_id)
                   UNION ALL
                   SELECT 'dispute_won', dispute_id, reversal_payable_credit_minor
                   FROM disputes d WHERE merchant_id = $1 AND status = 'won'
                     AND reversal_payable_credit_minor > 0
                     AND NOT EXISTS (SELECT 1 FROM settlement_items i
                         WHERE i.item_type = 'dispute_won' AND i.item_id = d.dispute_id)
                   UNION ALL
                   SELECT 'payout_return', payout_id, amount_minor
                   FROM payouts p WHERE merchant_id = $1 AND status = 'returned'
                     AND NOT EXISTS (SELECT 1 FROM settlement_items i
                         WHERE i.item_type = 'payout_return' AND i.item_id = p.payout_id)""",
                merchant_id,
            )
            releases = await connection.fetch(
                """SELECT settlement_id, reserve_held_minor FROM settlements s
                   WHERE merchant_id = $1 AND status = 'posted' AND reserve_held_minor > 0
                     AND reserve_release_on <= $2
                     AND NOT EXISTS (SELECT 1 FROM settlement_items i
                         WHERE i.item_type = 'reserve_release' AND i.item_id = s.settlement_id)""",
                merchant_id,
                business_date,
            )
            inputs = SettlementInputs(
                payment_amounts=[int(p["amount_minor"]) for p in payments],
                payable_debits=sum(int(d["amount"]) for d in debits),
                payable_credits=sum(int(c["amount"]) for c in credits),
                reserve_release=sum(int(r["reserve_held_minor"]) for r in releases),
                receivable_balance=max(receivable, 0),
            )
            breakdown = compute_settlement(inputs, schedule, Currency.INR)
            postings = settlement_postings(merchant_id, breakdown, key, Currency.INR)
            status = "computed" if postings else "empty"
            row = await connection.fetchrow(
                """INSERT INTO settlements(
                       settlement_id, merchant_id, business_date, currency, status, gross_minor,
                       payable_debits_minor, payable_credits_minor, fee_minor, gst_minor,
                       reserve_held_minor, reserve_released_minor, recovered_minor,
                       shortfall_minor, net_payout_minor, fee_rate, fixed_fee_minor, gst_rate,
                       reserve_rate, reserve_release_on
                   ) VALUES ($1, $2, $3, 'INR', $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                             $15, $16, $17, $18, $19)
                   RETURNING *""",
                settlement_id,
                merchant_id,
                business_date,
                status,
                breakdown.gross,
                breakdown.payable_debits,
                breakdown.payable_credits,
                breakdown.fees,
                breakdown.gst,
                breakdown.reserve_held,
                breakdown.reserve_released,
                breakdown.recovered,
                breakdown.shortfall,
                breakdown.net_payout,
                schedule.fee_rate,
                schedule.fixed_fee_minor,
                schedule.gst_rate,
                schedule.reserve_rate,
                business_date + timedelta(days=int(config["reserve_hold_days"])),
            )
            items: list[tuple[UUID, str, UUID, int, int]] = [
                (
                    settlement_id,
                    "payment",
                    p["payment_id"],
                    int(p["amount_minor"]),
                    payment_fee(int(p["amount_minor"]), schedule, Currency.INR),
                )
                for p in payments
            ]
            items += [
                (settlement_id, str(d["item_type"]), d["item_id"], int(d["amount"]), 0)
                for d in (*debits, *credits)
            ]
            items += [
                (
                    settlement_id,
                    "reserve_release",
                    r["settlement_id"],
                    int(r["reserve_held_minor"]),
                    0,
                )
                for r in releases
            ]
            if items:
                await connection.executemany(
                    """INSERT INTO settlement_items(
                           settlement_id, item_type, item_id, amount_minor, fee_minor
                       ) VALUES ($1, $2, $3, $4, $5)""",
                    items,
                )
            if postings:
                if entry_lines_total(postings) <= 0:
                    raise AssertionError("settlement postings must move a positive amount")
                await insert_command(
                    connection,
                    subject_type="settlement",
                    subject_id=settlement_id,
                    merchant_id=merchant_id,
                    kind="settlement_post",
                    key=key,
                    request=entry_request(key, postings),
                )
        if not postings:
            assert row is not None
            return row
        fault_point(ctx.faults, "settlement.after_command_committed")
        command = await ctx.pool.fetchrow(
            "SELECT * FROM core_ledger_commands WHERE idempotency_key = $1", key
        )
        try:
            await execute_command(ctx, command)
        except LedgerUnavailable:
            pass
    result = await ctx.pool.fetchrow(
        "SELECT * FROM settlements WHERE settlement_id = $1", settlement_id
    )
    return result if result is not None else cast(asyncpg.Record, row)


def payout_instruction_file(payout: Any, business_date: date) -> bytes:
    """Bank payout instruction (CSV) for one settlement payout."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["payout_id", "merchant_id", "amount_minor", "currency", "value_date"])
    writer.writerow(
        [
            str(payout["payout_id"]),
            payout["merchant_id"],
            str(payout["amount_minor"]),
            "INR",
            business_date.isoformat(),
        ]
    )
    return buffer.getvalue().encode()


async def claim_due_payouts(pool: asyncpg.Pool, limit: int = 20) -> list[asyncpg.Record]:
    async with pool.acquire() as connection, connection.transaction():
        rows = await connection.fetch(
            """SELECT p.*, s.business_date FROM payouts p
               JOIN settlements s USING (settlement_id)
               WHERE p.status IN ('pending', 'sent') AND p.next_attempt_at <= clock_timestamp()
                 AND (p.lease_until IS NULL OR p.lease_until <= clock_timestamp())
               ORDER BY p.next_attempt_at FOR UPDATE OF p SKIP LOCKED LIMIT $1""",
            limit,
        )
        if rows:
            await connection.execute(
                """UPDATE payouts SET lease_until = clock_timestamp() + interval '30 seconds'
                   WHERE payout_id = ANY($1::uuid[])""",
                [row["payout_id"] for row in rows],
            )
        return cast(list[asyncpg.Record], rows)


async def process_payout(
    ctx: MoneyContext,
    bank_http: httpx.AsyncClient,
    payout: Any,
    write_object: ObjectWriter | None = None,
) -> str:
    payout_id: UUID = payout["payout_id"]
    if payout["instruction_object_key"] is None and write_object is not None:
        content = payout_instruction_file(payout, payout["business_date"])
        object_key = f"payouts/{payout['business_date'].isoformat()}/{payout_id}.csv"
        await write_object(object_key, content)
        await ctx.pool.execute(
            """UPDATE payouts SET instruction_object_key = $2, instruction_sha256 = $3
               WHERE payout_id = $1""",
            payout_id,
            object_key,
            hashlib.sha256(content).hexdigest(),
        )
    try:
        response = await bank_http.post(
            "/v1/payouts",
            json={
                "payout_id": str(payout_id),
                "merchant_id": payout["merchant_id"],
                "amount_minor": int(payout["amount_minor"]),
                "currency": "INR",
            },
            headers={"Idempotency-Key": f"{payout_id}:payout:bank"},
        )
        response.raise_for_status()
        outcome = str(response.json().get("status"))
    except (httpx.HTTPError, ValueError):
        attempts = int(payout["attempts"]) + 1
        await ctx.pool.execute(
            """UPDATE payouts SET status = 'sent', attempts = $2, lease_until = NULL,
                   next_attempt_at = clock_timestamp() + $3::interval,
                   updated_at = clock_timestamp()
               WHERE payout_id = $1 AND status IN ('pending', 'sent')""",
            payout_id,
            attempts,
            timedelta(seconds=min(3600, 2 ** min(attempts, 11))),
        )
        return "unknown"
    fault_point(ctx.faults, "payout.after_bank_sent")
    merchant_id = str(payout["merchant_id"])
    amount = int(payout["amount_minor"])
    in_transit = f"merchant:{merchant_id}:payout_in_transit:INR"
    lines: list[dict[str, object]]
    if outcome == "paid":
        kind, key = "payout_complete", f"{payout_id}:payout:complete"
        lines = [
            {"account_id": in_transit, "direction": "debit", "amount_minor": amount},
            {"account_id": "bank:simulated:INR", "direction": "credit", "amount_minor": amount},
        ]
    else:
        kind, key = "payout_return", f"{payout_id}:payout:return"
        lines = [
            {"account_id": in_transit, "direction": "debit", "amount_minor": amount},
            {
                "account_id": f"merchant:{merchant_id}:payable:INR",
                "direction": "credit",
                "amount_minor": amount,
            },
        ]
    async with ctx.pool.acquire() as connection, connection.transaction():
        await insert_command(
            connection,
            subject_type="payout",
            subject_id=payout_id,
            merchant_id=merchant_id,
            kind=kind,
            key=key,
            request=entry_request(key, lines),
        )
        await connection.execute(
            "UPDATE payouts SET bank_reference = $2 WHERE payout_id = $1",
            payout_id,
            response.json().get("bank_reference"),
        )
    command = await ctx.pool.fetchrow(
        "SELECT * FROM core_ledger_commands WHERE idempotency_key = $1", key
    )
    try:
        await execute_command(ctx, command)
    except LedgerUnavailable:
        return "pending_ledger"
    if outcome != "paid":
        PAYOUTS_RETURNED.inc()
    return "paid" if outcome == "paid" else "returned"


async def payout_worker_batch(
    ctx: MoneyContext,
    bank_http: httpx.AsyncClient,
    write_object: ObjectWriter | None = None,
    limit: int = 20,
) -> int:
    rows = await claim_due_payouts(ctx.pool, limit)
    for row in rows:
        await process_payout(ctx, bank_http, row, write_object)
    return len(rows)


async def settle_due_merchants(ctx: MoneyContext, now: datetime) -> int:
    """Scheduler entry point: settle every merchant for the last closed IST business date."""
    business_date = last_closed_business_date(now)
    merchants = await ctx.pool.fetch(
        """SELECT DISTINCT merchant_id FROM payment_intents WHERE status = 'succeeded'
           UNION SELECT merchant_id FROM merchant_settlement_configs"""
    )
    settled = 0
    for row in merchants:
        exists = await ctx.pool.fetchval(
            "SELECT 1 FROM settlements WHERE merchant_id = $1 AND business_date = $2",
            row["merchant_id"],
            business_date,
        )
        if exists:
            continue
        try:
            await run_settlement(ctx, str(row["merchant_id"]), business_date)
        except Exception:
            SETTLEMENT_FAILURES.inc()
            raise
        settled += 1
    return settled
