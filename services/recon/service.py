"""Reconciliation runs, break workflow and maker-checker adjustments."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol, cast

import asyncpg
import httpx
from libs.common.business_time import cutoff_instant

from services.recon.engine import (
    BankRecord,
    Kind,
    LedgerRecord,
    ReconConfig,
    SwitchRecord,
    reconcile,
)
from services.recon.formats import FORMATS, parse_statement, write_statement

NOSTRO = "bank:simulated:INR"
SUSPENSE = "platform:suspense:INR"
BANK_CHARGES = "platform:bank_charges:INR"
SLA = {
    "missing_at_bank": timedelta(days=2),
    "missing_internally": timedelta(days=1),
    "amount_mismatch": timedelta(days=1),
    "duplicate": timedelta(days=3),
    "status_mismatch": timedelta(days=1),
    "timing_difference": timedelta(days=2),
    "fee_tax_mismatch": timedelta(days=5),
    "unknown": timedelta(days=1),
}


class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes) -> str: ...

    async def get(self, key: str) -> bytes: ...


class ReconError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(slots=True)
class ReconContext:
    pool: asyncpg.Pool
    ledger_http: httpx.AsyncClient
    store: ObjectStore
    config: ReconConfig


# Sources --------------------------------------------------------------------------------------
def ledger_record_from_line(line: dict[str, Any]) -> LedgerRecord | None:
    """Attribute a nostro posting to a business object by its deterministic key."""
    key = str(line["source_key"])
    suffixes = (
        (":upi:transfer:post:late-success-correction", Kind.UPI_TRANSFER, True),
        (":upi:transfer:post", Kind.UPI_TRANSFER, False),
        (":refund:complete", Kind.REFUND, False),
        (":payout:complete", Kind.PAYOUT, False),
    )
    for suffix, kind, suspense in suffixes:
        if key.endswith(suffix):
            return LedgerRecord(
                reference=key[: -len(suffix)],
                kind=kind,
                amount_minor=int(line["amount_minor"]),
                direction="in" if line["direction"] == "debit" else "out",
                posted_at=datetime.fromisoformat(str(line["created_at"])),
                entry_id=str(line["entry_id"]),
                suspense=suspense,
            )
    return None  # card captures, chargebacks and adjustments are outside nostro recon scope


async def load_ledger(
    ledger_http: httpx.AsyncClient, start: datetime, end: datetime
) -> list[LedgerRecord]:
    records: list[LedgerRecord] = []
    cursor: str | None = None
    while True:
        params: dict[str, str | int] = {
            "limit": 5000,
            "from_time": start.isoformat(),
            "to_time": end.isoformat(),
        }
        if cursor:
            params["cursor"] = cursor
        response = await ledger_http.get(f"/v1/accounts/{NOSTRO}/statement", params=params)
        response.raise_for_status()
        body = response.json()
        for line in body["lines"]:
            record = ledger_record_from_line(line)
            if record is not None:
                records.append(record)
        cursor = body["next_cursor"]
        if not cursor:
            return records


async def load_switch(pool: asyncpg.Pool, start: datetime, end: datetime) -> list[SwitchRecord]:
    rows = await pool.fetch(
        """SELECT p.payment_id::text AS reference, 'upi_transfer' AS kind, p.amount_minor,
                  CASE WHEN p.status = 'succeeded' THEN 'success'
                       WHEN EXISTS (SELECT 1 FROM core_recovery_incidents i
                                    WHERE i.payment_id = p.payment_id
                                      AND i.incident_type = 'late_success_after_reversal')
                            THEN 'late_success'
                       WHEN p.status IN ('failed', 'reversed', 'cancelled', 'expired')
                            THEN 'failed'
                       ELSE 'pending' END AS status,
                  coalesce(p.succeeded_at, p.updated_at) AS occurred_at,
                  p.payer_vpa AS vpa, p.merchant_id
           FROM payment_intents p
           WHERE p.payment_method_type = 'upi'
             AND coalesce(p.succeeded_at, p.updated_at) >= $1
             AND coalesce(p.succeeded_at, p.updated_at) < $2
           UNION ALL
           SELECT r.refund_id::text, 'refund', r.amount_minor,
                  CASE WHEN r.status = 'succeeded' THEN 'success'
                       WHEN r.status IN ('cancelled', 'failed') THEN 'failed'
                       ELSE 'pending' END,
                  coalesce(r.succeeded_at, r.updated_at), NULL, r.merchant_id
           FROM refunds r
           WHERE coalesce(r.succeeded_at, r.updated_at) >= $1
             AND coalesce(r.succeeded_at, r.updated_at) < $2
           UNION ALL
           SELECT o.payout_id::text, 'payout', o.amount_minor,
                  CASE WHEN o.status = 'paid' THEN 'success'
                       WHEN o.status = 'returned' THEN 'failed' ELSE 'pending' END,
                  coalesce(o.resolved_at, o.updated_at), NULL, o.merchant_id
           FROM payouts o
           WHERE coalesce(o.resolved_at, o.updated_at) >= $1
             AND coalesce(o.resolved_at, o.updated_at) < $2""",
        start,
        end,
    )
    return [
        SwitchRecord(
            reference=row["reference"],
            kind=Kind(row["kind"]),
            amount_minor=int(row["amount_minor"]),
            status=row["status"],
            occurred_at=row["occurred_at"],
            vpa=row["vpa"],
            merchant_id=row["merchant_id"],
        )
        for row in rows
    ]


def bank_records_from_journal(
    journal: list[dict[str, Any]], start: datetime, end: datetime
) -> list[BankRecord]:
    """The bank simulator's decision journal rendered as statement lines for one day."""
    kinds = {"upi_transfer": Kind.UPI_TRANSFER, "refund": Kind.REFUND, "payout": Kind.PAYOUT}
    lines: list[BankRecord] = []
    for item in journal:
        decided = datetime.fromisoformat(str(item["decided_at"]))
        if not start <= decided < end:
            continue
        lines.append(
            BankRecord(
                line_no=len(lines) + 1,
                reference=str(item["reference"]),
                bank_reference=str(item["bank_reference"]),
                kind=kinds[str(item["kind"])],
                amount_minor=int(item["amount_minor"]),
                status="success"
                if item["status"] in {"approved", "succeeded", "paid"}
                else "failed",
                occurred_at=decided,
            )
        )
    return lines


async def fetch_bank_statement(
    bank_http: httpx.AsyncClient, business_date: date, fmt: str
) -> bytes:
    """Simulate the bank's SFTP drop: render its journal for a business date in its format."""
    response = await bank_http.get("/internal/v1/journal")
    response.raise_for_status()
    end = cutoff_instant(business_date)
    return write_statement(
        bank_records_from_journal(response.json(), end - timedelta(days=1), end), fmt
    )


# Files ----------------------------------------------------------------------------------------
async def ingest_file(
    ctx: ReconContext, source: str, business_date: date, fmt: str, data: bytes
) -> asyncpg.Record:
    if fmt not in FORMATS:
        raise ReconError(422, "UNKNOWN_FORMAT", f"format must be one of {', '.join(FORMATS)}")
    digest = hashlib.sha256(data).hexdigest()
    existing = await ctx.pool.fetchrow(
        "SELECT * FROM recon_files WHERE source = $1 AND business_date = $2 AND sha256 = $3",
        source,
        business_date,
        digest,
    )
    if existing is not None:
        return existing
    records, issues = parse_statement(data, fmt)
    object_key = f"recon/{source}/{business_date.isoformat()}/{digest}.{fmt}"
    stored = await ctx.store.put(object_key, data)
    if stored != digest:
        raise ReconError(500, "STORE_CHECKSUM_MISMATCH", "object store checksum mismatch")
    row = await ctx.pool.fetchrow(
        """INSERT INTO recon_files(
               file_id, source, business_date, format, object_key, sha256, row_count,
               issue_count, issues
           ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
           ON CONFLICT (source, business_date, sha256) DO NOTHING RETURNING *""",
        uuid.uuid4(),
        source,
        business_date,
        fmt,
        object_key,
        digest,
        len(records),
        len(issues),
        json.dumps([{"line": i.line_no, "message": i.message} for i in issues[:100]]),
    )
    if row is None:
        row = await ctx.pool.fetchrow(
            "SELECT * FROM recon_files WHERE source = $1 AND business_date = $2 AND sha256 = $3",
            source,
            business_date,
            digest,
        )
    assert row is not None
    return row


async def _statement(
    ctx: ReconContext, source: str, day: date
) -> tuple[list[BankRecord], list[uuid.UUID]] | None:
    files = await ctx.pool.fetch(
        """SELECT file_id, format, object_key FROM recon_files
           WHERE source = $1 AND business_date = $2 ORDER BY ingested_at""",
        source,
        day,
    )
    if not files:
        return None
    records: list[BankRecord] = []
    for file in files:
        parsed, _ = parse_statement(await ctx.store.get(file["object_key"]), file["format"])
        offset = len(records)
        records.extend(
            BankRecord(
                line_no=offset + r.line_no,
                reference=r.reference,
                bank_reference=r.bank_reference,
                kind=r.kind,
                amount_minor=r.amount_minor,
                status=r.status,
                occurred_at=r.occurred_at,
                vpa=r.vpa,
                batch_id=r.batch_id,
            )
            for r in parsed
        )
    return records, [f["file_id"] for f in files]


# Runs -----------------------------------------------------------------------------------------
async def run_recon(ctx: ReconContext, source: str, business_date: date) -> asyncpg.Record:
    started = datetime.now(UTC)
    clock = time.monotonic()
    today = await _statement(ctx, source, business_date)
    if today is None:
        raise ReconError(409, "STATEMENT_MISSING", "No statement has been ingested for the date.")
    bank, file_ids = today
    next_day = await _statement(ctx, source, business_date + timedelta(days=1))
    day_end = cutoff_instant(business_date)
    day_start = day_end - timedelta(days=1)
    ledger = await load_ledger(ctx.ledger_http, day_start, day_end)
    switch = await load_switch(ctx.pool, day_start, day_end)
    carried = frozenset(
        str(row["reference"])
        for row in await ctx.pool.fetch(
            """SELECT reference FROM recon_breaks
               WHERE source = $1 AND business_date = $2 AND break_type = 'timing_difference'
                 AND reference IS NOT NULL""",
            source,
            business_date - timedelta(days=1),
        )
    )
    result = reconcile(
        ledger,
        switch,
        bank,
        business_date=business_date.isoformat(),
        source=source,
        day_end=day_end,
        next_day_bank=None if next_day is None else next_day[0],
        carried_over=carried,
        config=ctx.config,
    )
    run_id = uuid.uuid4()
    counts = Counter(b.break_type.value for b in result.breaks)
    ledger_by_ref: dict[str, list[dict[str, object]]] = {}
    for record in ledger:
        ledger_by_ref.setdefault(record.reference, []).append(
            {
                "entry_id": record.entry_id,
                "amount_minor": record.amount_minor,
                "direction": record.direction,
                "posted_at": record.posted_at.isoformat(),
                "suspense": record.suspense,
            }
        )
    switch_by_ref = {s.reference: s for s in switch}
    bank_by_key: dict[str, list[dict[str, object]]] = {}
    for line in bank:
        view: dict[str, object] = {
            "line_no": line.line_no,
            "reference": line.reference,
            "bank_reference": line.bank_reference,
            "kind": line.kind.value,
            "amount_minor": line.amount_minor,
            "status": line.status,
            "occurred_at": line.occurred_at.isoformat(),
            "vpa": line.vpa,
        }
        for key in {line.reference, line.bank_reference} - {None}:
            bank_by_key.setdefault(str(key), []).append(view)

    def evidence(item: Any) -> str:
        switch_record = switch_by_ref.get(item.reference or "")
        return json.dumps(
            {
                "ledger": ledger_by_ref.get(item.reference or "", []),
                "switch": None
                if switch_record is None
                else {
                    "reference": switch_record.reference,
                    "kind": switch_record.kind.value,
                    "amount_minor": switch_record.amount_minor,
                    "status": switch_record.status,
                    "occurred_at": switch_record.occurred_at.isoformat(),
                    "vpa": switch_record.vpa,
                    "merchant_id": switch_record.merchant_id,
                },
                "bank": bank_by_key.get(item.bank_reference or item.reference or "", []),
            }
        )

    open_value = sum(b.amount_minor for b in result.breaks if b.status == "open")
    async with ctx.pool.acquire() as connection, connection.transaction():
        run = await connection.fetchrow(
            """INSERT INTO recon_runs(
                   run_id, source, business_date, file_ids, bank_lines, internal_records,
                   matched_lines, unmatched_lines, breaks_by_type, open_break_value_minor,
                   duration_ms, started_at
               ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12)
               RETURNING *""",
            run_id,
            source,
            business_date,
            file_ids,
            len(bank),
            len({r.reference for r in ledger} | {s.reference for s in switch}),
            result.matched_bank_lines,
            result.unmatched_bank_lines,
            json.dumps(dict(counts)),
            open_value,
            int((time.monotonic() - clock) * 1000),
            started,
        )
        await connection.copy_records_to_table(
            "recon_matches",
            records=[
                (run_id, m.match_type, list(m.references), list(m.bank_lines))
                for m in result.matches
            ],
            columns=["run_id", "match_type", "refs", "bank_lines"],
        )
        await connection.executemany(
            """INSERT INTO recon_breaks(
                   break_id, source, business_date, break_type, reference, bank_reference, kind,
                   amount_minor, internal_amount_minor, bank_amount_minor, status,
                   suggested_action, detail, first_seen_run, last_seen_run, sla_due_at, evidence
               ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $14, $15,
                         $16::jsonb)
               ON CONFLICT (break_id) DO UPDATE SET
                   amount_minor = EXCLUDED.amount_minor,
                   internal_amount_minor = EXCLUDED.internal_amount_minor,
                   bank_amount_minor = EXCLUDED.bank_amount_minor,
                   detail = EXCLUDED.detail,
                   evidence = EXCLUDED.evidence,
                   last_seen_run = EXCLUDED.last_seen_run,
                   updated_at = clock_timestamp(),
                   status = CASE WHEN recon_breaks.status IN ('resolved', 'pending_approval')
                                 THEN recon_breaks.status ELSE EXCLUDED.status END""",
            [
                (
                    uuid.UUID(b.break_id),
                    source,
                    business_date,
                    b.break_type.value,
                    b.reference,
                    b.bank_reference,
                    b.kind,
                    b.amount_minor,
                    b.internal_amount_minor,
                    b.bank_amount_minor,
                    b.status,
                    b.suggested_action,
                    b.detail,
                    run_id,
                    started + SLA[b.break_type.value],
                    evidence(b),
                )
                for b in result.breaks
            ],
        )
        # Breaks from an earlier run of this date that no longer reproduce clear themselves.
        stale = await connection.fetch(
            """UPDATE recon_breaks SET status = 'auto_resolved', resolved_at = clock_timestamp(),
                   resolution = 'not reproduced by a later run', updated_at = clock_timestamp()
               WHERE source = $1 AND business_date = $2 AND status = 'open'
                 AND last_seen_run <> $3 RETURNING break_id""",
            source,
            business_date,
            run_id,
        )
        # Timing differences from the previous day that this statement carried over clear too.
        carried_rows = await connection.fetch(
            """UPDATE recon_breaks SET status = 'auto_resolved', resolved_at = clock_timestamp(),
                   resolution = 'reported on the next statement', updated_at = clock_timestamp()
               WHERE source = $1 AND business_date = $2 AND break_type = 'timing_difference'
                 AND status = 'open' AND reference = ANY($3::text[]) RETURNING break_id""",
            source,
            business_date - timedelta(days=1),
            sorted(m.references[0] for m in result.matches if m.match_type == "carry_over"),
        )
        for row in (*stale, *carried_rows):
            await connection.execute(
                """INSERT INTO break_actions(break_id, actor, action, note)
                   VALUES ($1, 'recon-engine', 'auto_resolved', $2)""",
                row["break_id"],
                f"run {run_id}",
            )
    assert run is not None
    return run


# Workflow -------------------------------------------------------------------------------------
ADJUSTMENTS: dict[str, str] = {
    "book_to_suspense": "Record an unattributed bank movement: Dr nostro, Cr suspense.",
    "reverse_to_suspense": "Remove a movement the bank never made: Dr suspense, Cr nostro.",
    "book_bank_charges": "Book bank charges and GST on charges: Dr bank charges, Cr nostro.",
    "close_no_entry": "Close with an explanation; no ledger entry.",
}


def adjustment_postings(action: str, brk: asyncpg.Record) -> list[dict[str, object]]:
    amount = int(brk["amount_minor"])
    if action == "close_no_entry":
        return []
    if amount <= 0:
        raise ReconError(422, "NOTHING_TO_ADJUST", "The break has no amount to post.")
    pairs = {
        "book_to_suspense": (NOSTRO, SUSPENSE),
        "reverse_to_suspense": (SUSPENSE, NOSTRO),
        "book_bank_charges": (BANK_CHARGES, NOSTRO),
    }
    if action not in pairs:
        raise ReconError(422, "UNKNOWN_ADJUSTMENT", "Unsupported adjustment.")
    debit, credit = pairs[action]
    return [
        {"account_id": debit, "direction": "debit", "amount_minor": amount},
        {"account_id": credit, "direction": "credit", "amount_minor": amount},
    ]


async def add_action(
    connection: asyncpg.Connection, break_id: uuid.UUID, actor: str, action: str, note: str | None
) -> None:
    await connection.execute(
        "INSERT INTO break_actions(break_id, actor, action, note) VALUES ($1, $2, $3, $4)",
        break_id,
        actor,
        action,
        note,
    )


async def propose_adjustment(
    ctx: ReconContext, break_id: uuid.UUID, maker: str, action: str, note: str
) -> asyncpg.Record:
    async with ctx.pool.acquire() as connection, connection.transaction():
        brk = await connection.fetchrow(
            "SELECT * FROM recon_breaks WHERE break_id = $1 FOR UPDATE", break_id
        )
        if brk is None:
            raise ReconError(404, "BREAK_NOT_FOUND", "Break was not found.")
        if brk["status"] != "open":
            raise ReconError(409, "BREAK_NOT_OPEN", f"Break is {brk['status']}.")
        postings = adjustment_postings(action, brk)
        request = await connection.fetchrow(
            """INSERT INTO maker_checker_requests(
                   request_id, action_type, subject_id, payload, maker, status
               ) VALUES ($1, 'recon_adjustment', $2, $3::jsonb, $4, 'pending') RETURNING *""",
            uuid.uuid4(),
            str(break_id),
            json.dumps({"adjustment": action, "note": note, "postings": postings}),
            maker,
        )
        await connection.execute(
            """UPDATE recon_breaks SET status = 'pending_approval', updated_at = clock_timestamp()
               WHERE break_id = $1""",
            break_id,
        )
        await add_action(connection, break_id, maker, f"proposed:{action}", note)
    assert request is not None
    return request


async def decide(
    ctx: ReconContext, request_id: uuid.UUID, checker: str, approve: bool, reason: str
) -> asyncpg.Record:
    """Approve (and execute) or reject a request. The maker can never approve their own."""
    async with ctx.pool.acquire() as connection, connection.transaction():
        request = await connection.fetchrow(
            "SELECT * FROM maker_checker_requests WHERE request_id = $1 FOR UPDATE", request_id
        )
        if request is None:
            raise ReconError(404, "REQUEST_NOT_FOUND", "Approval request was not found.")
        if request["status"] != "pending":
            raise ReconError(409, "REQUEST_DECIDED", f"Request is {request['status']}.")
        if request["maker"] == checker:
            raise ReconError(403, "MAKER_CANNOT_APPROVE", "The maker cannot decide their request.")
        break_id = uuid.UUID(request["subject_id"])
        if not approve:
            await connection.execute(
                """UPDATE maker_checker_requests SET status = 'rejected', checker = $2,
                       decision_reason = $3, decided_at = clock_timestamp()
                   WHERE request_id = $1""",
                request_id,
                checker,
                reason,
            )
            await connection.execute(
                "UPDATE recon_breaks SET status = 'open', updated_at = clock_timestamp() "
                "WHERE break_id = $1",
                break_id,
            )
            await add_action(connection, break_id, checker, "rejected", reason)
            row = await connection.fetchrow(
                "SELECT * FROM maker_checker_requests WHERE request_id = $1", request_id
            )
            assert row is not None
            return row
        await connection.execute(
            """UPDATE maker_checker_requests SET status = 'approved', checker = $2,
                   decision_reason = $3, decided_at = clock_timestamp() WHERE request_id = $1""",
            request_id,
            checker,
            reason,
        )
    return await execute_approved(ctx, request_id)


async def execute_approved(ctx: ReconContext, request_id: uuid.UUID) -> asyncpg.Record:
    """Post an approved adjustment. Safe to repeat: the ledger key is the request ID."""
    request = await ctx.pool.fetchrow(
        "SELECT * FROM maker_checker_requests WHERE request_id = $1", request_id
    )
    if request is None or request["status"] not in {"approved", "failed"}:
        raise ReconError(409, "REQUEST_NOT_EXECUTABLE", "Only approved requests can execute.")
    break_id = uuid.UUID(request["subject_id"])
    payload = request["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    postings = cast(list[dict[str, object]], payload["postings"])
    result: dict[str, object] = {"adjustment": payload["adjustment"]}
    status = "executed"
    if postings:
        try:
            response = await ctx.ledger_http.post(
                "/v1/entries",
                json={"idempotency_key": f"recon-adjust:{request_id}", "postings": postings},
            )
        except httpx.HTTPError as exc:
            raise ReconError(503, "LEDGER_UNAVAILABLE", "Retry execution later.") from exc
        if response.status_code >= 500:
            raise ReconError(503, "LEDGER_UNAVAILABLE", "Retry execution later.")
        if response.status_code >= 400:
            status = "failed"
            result["ledger_error"] = response.status_code
        else:
            result["entry_id"] = response.json()["entry_id"]
    async with ctx.pool.acquire() as connection, connection.transaction():
        row = await connection.fetchrow(
            """UPDATE maker_checker_requests SET status = $2, result = $3::jsonb
               WHERE request_id = $1 RETURNING *""",
            request_id,
            status,
            json.dumps(result),
        )
        if status == "executed":
            await connection.execute(
                """UPDATE recon_breaks SET status = 'resolved', resolved_at = clock_timestamp(),
                       resolution = $2, updated_at = clock_timestamp() WHERE break_id = $1""",
                break_id,
                f"{payload['adjustment']} approved by {request['checker']}",
            )
            await add_action(
                connection, break_id, str(request["checker"]), "approved_and_executed", None
            )
    assert row is not None
    return row


async def daily_report(pool: asyncpg.Pool, business_date: date) -> dict[str, Any]:
    runs = await pool.fetch(
        """SELECT DISTINCT ON (source) * FROM recon_runs WHERE business_date = $1
           ORDER BY source, finished_at DESC""",
        business_date,
    )
    breaks = await pool.fetch(
        """SELECT break_type, status, count(*) AS n, sum(amount_minor) AS value
           FROM recon_breaks WHERE business_date = $1 GROUP BY break_type, status""",
        business_date,
    )
    aging = await pool.fetch(
        """SELECT CASE WHEN age < interval '1 day' THEN '0-1d'
                       WHEN age < interval '3 days' THEN '1-3d'
                       WHEN age < interval '7 days' THEN '3-7d' ELSE '7d+' END AS bucket,
                  count(*) AS n, sum(amount_minor) AS value,
                  count(*) FILTER (WHERE sla_due_at < clock_timestamp()) AS sla_breached
           FROM (SELECT clock_timestamp() - created_at AS age, amount_minor, sla_due_at
                 FROM recon_breaks WHERE status IN ('open', 'pending_approval')) b
           GROUP BY 1 ORDER BY 1"""
    )
    matched = sum(int(r["matched_lines"]) for r in runs)
    lines = sum(int(r["bank_lines"]) for r in runs)
    return {
        "business_date": business_date.isoformat(),
        "sources": [
            {
                "source": r["source"],
                "run_id": str(r["run_id"]),
                "bank_lines": r["bank_lines"],
                "matched_lines": r["matched_lines"],
                "open_break_value_minor": r["open_break_value_minor"],
                "duration_ms": r["duration_ms"],
            }
            for r in runs
        ],
        "match_rate_bps": 10_000 if lines == 0 else matched * 10_000 // lines,
        "breaks": [
            {
                "break_type": r["break_type"],
                "status": r["status"],
                "count": r["n"],
                "value_minor": int(r["value"]),
            }
            for r in breaks
        ],
        "aging": [
            {
                "bucket": r["bucket"],
                "count": r["n"],
                "value_minor": int(r["value"]),
                "sla_breached": r["sla_breached"],
            }
            for r in aging
        ],
    }
