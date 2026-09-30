"""HTTP boundary for the independent PostgreSQL ledger."""

from __future__ import annotations

import hmac
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

import asyncpg
from fastapi import FastAPI, Header, HTTPException, Path, Query, Request, status
from libs.money import MAX_SAFE_INTEGER
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from services.ledger.model import Direction

AccountId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
IdempotencyKey = Annotated[str, StringConstraints(min_length=1, max_length=200)]
MinorAmount = Annotated[int, Field(strict=True, gt=0, le=MAX_SAFE_INTEGER)]


class PostingInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    account_id: AccountId
    direction: Direction = Field(strict=False)
    amount_minor: MinorAmount


class PostEntryRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    idempotency_key: IdempotencyKey
    postings: Annotated[list[PostingInput], Field(min_length=2, max_length=100)]


class PlaceHoldRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    idempotency_key: IdempotencyKey
    postings: Annotated[list[PostingInput], Field(min_length=2, max_length=100)]


class PostingResponse(BaseModel):
    entry_id: str
    idempotency_key: str


class HoldResponse(BaseModel):
    hold_id: str
    idempotency_key: str
    status: str


class AccountBalanceResponse(BaseModel):
    account_id: str
    currency: str
    posted_minor: int
    pending_debits_minor: int
    pending_credits_minor: int
    available_minor: int
    version: int


class IntegrityCheck(BaseModel):
    check_name: str
    ok: bool
    detail: str


class MerchantAccountProvisionRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    merchant_id: Annotated[
        str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    ]
    currency: Literal["INR"] = "INR"


# Per-merchant accounts: sales accrue to payable; settlement moves value to reserve and
# payout_in_transit; receivable records what the merchant owes (refund/chargeback shortfalls).
MERCHANT_ACCOUNTS: tuple[tuple[str, str], ...] = (
    ("payable", "liability"),
    ("reserve", "liability"),
    ("payout_in_transit", "liability"),
    ("receivable", "asset"),
)


class MerchantAccountProvisionResponse(BaseModel):
    account_id: str
    currency: str


def _posting_payload(request: PostEntryRequest | PlaceHoldRequest) -> str:
    return json.dumps(
        [
            {
                "account_id": row.account_id,
                "direction": row.direction.value,
                "amount_minor": row.amount_minor,
            }
            for row in request.postings
        ],
        separators=(",", ":"),
    )


def _database_error(exc: asyncpg.PostgresError) -> HTTPException:
    sqlstate = exc.sqlstate
    detail: dict[str, str]
    if sqlstate == "23505":
        detail = {
            "code": "IDEMPOTENCY_CONFLICT",
            "message": "Idempotency key was reused with a different payload.",
        }
        return HTTPException(status.HTTP_409_CONFLICT, detail=detail)
    if sqlstate == "23503":
        detail = {
            "code": "ACCOUNT_NOT_FOUND",
            "message": "A referenced ledger account does not exist.",
        }
        return HTTPException(status.HTTP_404_NOT_FOUND, detail=detail)
    if sqlstate == "P0002":
        detail = {"code": "HOLD_NOT_FOUND", "message": "The hold does not exist."}
        return HTTPException(status.HTTP_404_NOT_FOUND, detail=detail)
    if sqlstate == "23514":
        if "negative" in exc.message.lower():
            detail = {
                "code": "INSUFFICIENT_FUNDS",
                "message": "The posting would make an account balance negative.",
            }
            return HTTPException(status.HTTP_409_CONFLICT, detail=detail)
        detail = {
            "code": "LEDGER_INVARIANT_VIOLATION",
            "message": "The journal entry failed a ledger invariant.",
        }
        return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)
    if sqlstate == "22023":
        detail = {"code": "INVALID_POSTING", "message": "The posting request is invalid."}
        return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)
    if sqlstate == "55000":
        detail = {"code": "ACCOUNT_CLOSED", "message": "A referenced ledger account is closed."}
        return HTTPException(status.HTTP_409_CONFLICT, detail=detail)
    detail = {"code": "LEDGER_UNAVAILABLE", "message": "The ledger could not complete the request."}
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    database_url = os.environ.get("LEDGER_DATABASE_URL")
    if not database_url:
        raise RuntimeError("LEDGER_DATABASE_URL must be configured")
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=10, command_timeout=5)
    app.state.pool = pool
    app.state.provisioning_key = os.environ.get("LEDGER_PROVISIONING_KEY", "")
    try:
        yield
    finally:
        await pool.close()


app = FastAPI(title="Tally Ledger", version="1.0.0", lifespan=lifespan)


def _pool(request: Request) -> asyncpg.Pool:
    pool: Any = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ledger database is not ready")
    return pool


@app.get("/health/live", include_in_schema=False)
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", include_in_schema=False)
async def readiness(request: Request) -> dict[str, str]:
    try:
        await _pool(request).fetchval("SELECT 1")
    except (asyncpg.PostgresError, OSError) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "ledger database is not ready"
        ) from exc
    return {"status": "ready"}


@app.post(
    "/internal/v1/merchant-accounts",
    response_model=MerchantAccountProvisionResponse,
    include_in_schema=False,
)
async def provision_merchant_account(
    body: MerchantAccountProvisionRequest,
    request: Request,
    x_ledger_provisioning_key: Annotated[str | None, Header()] = None,
) -> MerchantAccountProvisionResponse:
    expected = cast(str, getattr(request.app.state, "provisioning_key", ""))
    if (
        not expected
        or not x_ledger_provisioning_key
        or not hmac.compare_digest(expected, x_ledger_provisioning_key)
    ):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "ledger provisioning authentication failed"
        )
    account_id = f"merchant:{body.merchant_id}:payable:{body.currency}"
    async with _pool(request).acquire() as connection, connection.transaction():
        for suffix, account_type in MERCHANT_ACCOUNTS:
            merchant_account = f"merchant:{body.merchant_id}:{suffix}:{body.currency}"
            await connection.execute(
                """INSERT INTO ledger_accounts(account_id, account_type, currency, allow_negative)
                   VALUES ($1, $2::ledger_account_type, $3, false)
                   ON CONFLICT (account_id) DO NOTHING""",
                merchant_account,
                account_type,
                body.currency,
            )
            existing = await connection.fetchrow(
                """SELECT account_type::text, currency, allow_negative, closed
                   FROM ledger_accounts WHERE account_id = $1 FOR UPDATE""",
                merchant_account,
            )
            if (
                existing is None
                or existing["account_type"] != account_type
                or existing["currency"].strip() != body.currency
                or existing["allow_negative"]
                or existing["closed"]
            ):
                raise HTTPException(
                    status.HTTP_409_CONFLICT, "merchant ledger account conflicts with chart"
                )
            await connection.execute(
                """INSERT INTO ledger_account_balances(account_id) VALUES ($1)
                   ON CONFLICT DO NOTHING""",
                merchant_account,
            )
    return MerchantAccountProvisionResponse(account_id=account_id, currency=body.currency)


@app.post("/v1/entries", response_model=PostingResponse, status_code=status.HTTP_201_CREATED)
async def post_entry(body: PostEntryRequest, request: Request) -> PostingResponse:
    payload = _posting_payload(body)
    try:
        async with _pool(request).acquire() as connection:
            entry_id = await connection.fetchval(
                "SELECT ledger_post_entry($1, $2::jsonb)", body.idempotency_key, payload
            )
    except asyncpg.PostgresError as exc:
        raise _database_error(exc) from exc
    return PostingResponse(
        entry_id=str(entry_id),
        idempotency_key=body.idempotency_key,
    )


@app.post("/v1/holds", response_model=HoldResponse, status_code=status.HTTP_201_CREATED)
async def place_hold(body: PlaceHoldRequest, request: Request) -> HoldResponse:
    payload = _posting_payload(body)
    try:
        async with _pool(request).acquire() as connection:
            hold_id = await connection.fetchval(
                "SELECT ledger_place_hold($1, $2::jsonb)", body.idempotency_key, payload
            )
            hold_status = await connection.fetchval(
                "SELECT status::text FROM ledger_holds WHERE hold_id = $1", hold_id
            )
    except asyncpg.PostgresError as exc:
        raise _database_error(exc) from exc
    return HoldResponse(
        hold_id=str(hold_id), idempotency_key=body.idempotency_key, status=hold_status
    )


@app.post("/v1/holds/{hold_id}/post", response_model=PostingResponse)
async def post_hold(
    hold_id: Annotated[int, Path(gt=0, le=MAX_SAFE_INTEGER)], request: Request
) -> PostingResponse:
    try:
        async with _pool(request).acquire() as connection:
            entry_id = await connection.fetchval("SELECT ledger_post_hold($1)", hold_id)
    except asyncpg.PostgresError as exc:
        raise _database_error(exc) from exc
    return PostingResponse(entry_id=str(entry_id), idempotency_key=f"hold-post:{hold_id}")


@app.post("/v1/holds/{hold_id}/void", response_model=HoldResponse)
async def void_hold(
    hold_id: Annotated[int, Path(gt=0, le=MAX_SAFE_INTEGER)], request: Request
) -> HoldResponse:
    try:
        async with _pool(request).acquire() as connection:
            await connection.fetchval("SELECT ledger_void_hold($1)", hold_id)
            row = await connection.fetchrow(
                "SELECT idempotency_key, status::text FROM ledger_holds WHERE hold_id = $1",
                hold_id,
            )
    except asyncpg.PostgresError as exc:
        raise _database_error(exc) from exc
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": "HOLD_NOT_FOUND", "message": "Hold does not exist."},
        )
    return HoldResponse(
        hold_id=str(hold_id), idempotency_key=row["idempotency_key"], status=row["status"]
    )


class EntryLookupResponse(BaseModel):
    entry_id: str
    idempotency_key: str
    postings: list[PostingInput]


class HoldLookupResponse(BaseModel):
    hold_id: str
    idempotency_key: str
    status: str
    entry_id: str | None


@app.get("/v1/entries/by-key", response_model=EntryLookupResponse)
async def entry_by_key(idempotency_key: IdempotencyKey, request: Request) -> EntryLookupResponse:
    """Tell a recovering caller whether a deterministic key already committed."""
    try:
        rows = await _pool(request).fetch(
            """SELECT e.entry_id, p.account_id, p.direction::text AS direction, p.amount_minor
               FROM ledger_journal_entries e JOIN ledger_postings p USING (entry_id)
               WHERE e.idempotency_key = $1 ORDER BY p.posting_id""",
            idempotency_key,
        )
    except asyncpg.PostgresError as exc:
        raise _database_error(exc) from exc
    if not rows:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": "ENTRY_NOT_FOUND", "message": "No entry has this key."},
        )
    return EntryLookupResponse(
        entry_id=str(rows[0]["entry_id"]),
        idempotency_key=idempotency_key,
        postings=[
            PostingInput(
                account_id=row["account_id"],
                direction=Direction(row["direction"]),
                amount_minor=row["amount_minor"],
            )
            for row in rows
        ],
    )


@app.get("/v1/holds/by-key", response_model=HoldLookupResponse)
async def hold_by_key(idempotency_key: IdempotencyKey, request: Request) -> HoldLookupResponse:
    try:
        row = await _pool(request).fetchrow(
            """SELECT hold_id, status::text AS status, entry_id FROM ledger_holds
               WHERE idempotency_key = $1""",
            idempotency_key,
        )
    except asyncpg.PostgresError as exc:
        raise _database_error(exc) from exc
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": "HOLD_NOT_FOUND", "message": "No hold has this key."},
        )
    return HoldLookupResponse(
        hold_id=str(row["hold_id"]),
        idempotency_key=idempotency_key,
        status=row["status"],
        entry_id=None if row["entry_id"] is None else str(row["entry_id"]),
    )


@app.get("/v1/accounts/{account_id}/balance", response_model=AccountBalanceResponse)
async def get_balance(
    account_id: AccountId,
    request: Request,
) -> AccountBalanceResponse:
    query = """
        WITH per_hold AS (
            SELECT hp.hold_id,
                   sum(CASE
                       WHEN (a.account_type IN ('asset', 'expense')
                             AND hp.direction = 'debit')
                         OR (a.account_type IN ('liability', 'equity', 'income')
                             AND hp.direction = 'credit')
                       THEN hp.amount_minor ELSE -hp.amount_minor END) AS natural_delta
            FROM ledger_hold_postings hp
            JOIN ledger_holds h ON h.hold_id = hp.hold_id AND h.status = 'pending'
            JOIN ledger_accounts a ON a.account_id = hp.account_id
            WHERE hp.account_id = $1
            GROUP BY hp.hold_id
        ), reserved AS (
            SELECT coalesce(sum(least(natural_delta, 0)), 0) AS amount_minor FROM per_hold
        )
        SELECT a.account_id, a.currency,
               coalesce(b.posted_minor, 0) AS posted_minor,
               coalesce(b.pending_debits, 0) AS pending_debits,
               coalesce(b.pending_credits, 0) AS pending_credits,
               coalesce(b.posted_minor, 0) + r.amount_minor AS available_minor,
               coalesce(b.version, 0) AS version
        FROM ledger_accounts a
        LEFT JOIN ledger_account_balances b USING (account_id)
        CROSS JOIN reserved r
        WHERE a.account_id = $1
    """
    try:
        row = await _pool(request).fetchrow(query, account_id)
    except asyncpg.PostgresError as exc:
        raise _database_error(exc) from exc
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": "ACCOUNT_NOT_FOUND", "message": "Account does not exist."},
        )
    return AccountBalanceResponse(
        account_id=row["account_id"],
        currency=row["currency"].strip(),
        posted_minor=row["posted_minor"],
        pending_debits_minor=row["pending_debits"],
        pending_credits_minor=row["pending_credits"],
        available_minor=row["available_minor"],
        version=row["version"],
    )


@app.get("/v1/integrity", response_model=list[IntegrityCheck])
async def verify_integrity(request: Request) -> list[IntegrityCheck]:
    try:
        rows = await _pool(request).fetch(
            "SELECT check_name, ok, detail FROM ledger_verify_integrity()"
        )
    except asyncpg.PostgresError as exc:
        raise _database_error(exc) from exc
    return [
        IntegrityCheck(check_name=row["check_name"], ok=row["ok"], detail=row["detail"])
        for row in rows
    ]


class AccountSummary(BaseModel):
    account_id: str
    account_type: str
    currency: str
    allow_negative: bool
    closed: bool
    shard_parent_id: str | None
    posted_minor: int


class TrialBalanceRow(BaseModel):
    account_id: str
    account_type: str
    currency: str
    debit_minor: int
    credit_minor: int
    balance_minor: int


class TrialBalanceResponse(BaseModel):
    as_of: str | None
    rows: list[TrialBalanceRow]
    total_debit_minor: int
    total_credit_minor: int
    balanced: bool


class StatementLine(BaseModel):
    posting_id: str
    entry_id: str
    idempotency_key: str
    direction: str
    amount_minor: int
    natural_delta_minor: int
    created_at: str


class StatementResponse(BaseModel):
    account_id: str
    lines: list[StatementLine]
    next_cursor: str | None


class EntryDetail(BaseModel):
    entry_id: str
    idempotency_key: str
    entry_hash: str
    previous_hash: str
    created_at: str
    postings: list[PostingInput]


class IntegrityReport(BaseModel):
    generated_at: str
    checks: list[IntegrityCheck]
    trial_balance_balanced: bool
    snapshot_id: str | None
    snapshot_mismatches: int
    ok: bool


@app.get("/v1/accounts", response_model=list[AccountSummary])
async def list_accounts(request: Request) -> list[AccountSummary]:
    rows = await _pool(request).fetch(
        """SELECT a.account_id, a.account_type::text AS account_type, a.currency,
                  a.allow_negative, a.closed, a.shard_parent_id,
                  coalesce(b.posted_minor, 0) AS posted_minor
           FROM ledger_accounts a LEFT JOIN ledger_account_balances b USING (account_id)
           ORDER BY a.account_id"""
    )
    return [
        AccountSummary(
            account_id=row["account_id"],
            account_type=row["account_type"],
            currency=row["currency"].strip(),
            allow_negative=row["allow_negative"],
            closed=row["closed"],
            shard_parent_id=row["shard_parent_id"],
            posted_minor=row["posted_minor"],
        )
        for row in rows
    ]


@app.get("/v1/trial-balance", response_model=TrialBalanceResponse)
async def trial_balance(request: Request, as_of: datetime | None = None) -> TrialBalanceResponse:
    rows = await _pool(request).fetch(
        "SELECT * FROM ledger_trial_balance($1)", as_of or datetime.max.replace(tzinfo=UTC)
    )
    items = [
        TrialBalanceRow(
            account_id=row["account_id"],
            account_type=row["account_type"],
            currency=row["currency"].strip(),
            debit_minor=row["debit_minor"],
            credit_minor=row["credit_minor"],
            balance_minor=row["balance_minor"],
        )
        for row in rows
    ]
    debit = sum(row.debit_minor for row in items)
    credit = sum(row.credit_minor for row in items)
    return TrialBalanceResponse(
        as_of=as_of.isoformat() if as_of else None,
        rows=items,
        total_debit_minor=debit,
        total_credit_minor=credit,
        balanced=debit == credit,
    )


@app.get("/v1/accounts/{account_id}/statement", response_model=StatementResponse)
async def account_statement(
    account_id: AccountId,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: Annotated[int | None, Query(ge=1)] = None,
) -> StatementResponse:
    """Newest-first postings for an account (and its shards), keyset-paginated by posting ID."""
    rows = await _pool(request).fetch(
        """SELECT p.posting_id, p.entry_id, e.idempotency_key, p.direction::text AS direction,
                  p.amount_minor, p.created_at,
                  ledger_natural_delta(a.account_type, p.direction, p.amount_minor) AS delta
           FROM ledger_postings p
           JOIN ledger_accounts a ON a.account_id = p.account_id
           JOIN ledger_journal_entries e ON e.entry_id = p.entry_id
           WHERE (a.account_id = $1 OR a.shard_parent_id = $1)
             AND ($2::bigint IS NULL OR p.posting_id < $2)
           ORDER BY p.posting_id DESC LIMIT $3""",
        account_id,
        cursor,
        limit + 1,
    )
    lines = [
        StatementLine(
            posting_id=str(row["posting_id"]),
            entry_id=str(row["entry_id"]),
            idempotency_key=row["idempotency_key"],
            direction=row["direction"],
            amount_minor=row["amount_minor"],
            natural_delta_minor=row["delta"],
            created_at=row["created_at"].isoformat(),
        )
        for row in rows[:limit]
    ]
    next_cursor = lines[-1].posting_id if len(rows) > limit else None
    return StatementResponse(account_id=account_id, lines=lines, next_cursor=next_cursor)


@app.get("/v1/entries/{entry_id}", response_model=EntryDetail)
async def entry_detail(
    entry_id: Annotated[int, Path(gt=0, le=MAX_SAFE_INTEGER)], request: Request
) -> EntryDetail:
    rows = await _pool(request).fetch(
        """SELECT e.entry_id, e.idempotency_key, e.entry_hash, e.previous_hash, e.created_at,
                  p.account_id, p.direction::text AS direction, p.amount_minor
           FROM ledger_journal_entries e JOIN ledger_postings p USING (entry_id)
           WHERE e.entry_id = $1 ORDER BY p.posting_id""",
        entry_id,
    )
    if not rows:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": "ENTRY_NOT_FOUND", "message": "Entry does not exist."},
        )
    first = rows[0]
    return EntryDetail(
        entry_id=str(first["entry_id"]),
        idempotency_key=first["idempotency_key"],
        entry_hash=bytes(first["entry_hash"]).hex(),
        previous_hash=bytes(first["previous_hash"]).hex(),
        created_at=first["created_at"].isoformat(),
        postings=[
            PostingInput(
                account_id=row["account_id"],
                direction=Direction(row["direction"]),
                amount_minor=row["amount_minor"],
            )
            for row in rows
        ],
    )


@app.post("/internal/v1/integrity-report", response_model=IntegrityReport)
async def integrity_report(
    request: Request,
    x_ledger_provisioning_key: Annotated[str | None, Header()] = None,
) -> IntegrityReport:
    """Daily job: verify invariants, snapshot balances, and recheck the snapshot from postings."""
    expected = cast(str, getattr(request.app.state, "provisioning_key", ""))
    if (
        not expected
        or not x_ledger_provisioning_key
        or not hmac.compare_digest(expected, x_ledger_provisioning_key)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "ledger internal authentication failed")
    pool = _pool(request)
    checks = await pool.fetch("SELECT check_name, ok, detail FROM ledger_verify_integrity()")
    totals = await pool.fetchrow(
        "SELECT sum(debit_minor) AS d, sum(credit_minor) AS c FROM ledger_trial_balance()"
    )
    snapshot_id = await pool.fetchval("SELECT ledger_take_snapshot()")
    mismatches = await pool.fetchval(
        """SELECT count(*) FROM ledger_verify_snapshot($1)
           WHERE snapshot_minor <> recomputed_minor""",
        snapshot_id,
    )
    check_models = [
        IntegrityCheck(check_name=row["check_name"], ok=row["ok"], detail=row["detail"])
        for row in checks
    ]
    balanced = totals is not None and totals["d"] == totals["c"]
    return IntegrityReport(
        generated_at=datetime.now(UTC).isoformat(),
        checks=check_models,
        trial_balance_balanced=balanced,
        snapshot_id=str(snapshot_id),
        snapshot_mismatches=int(mismatches),
        ok=all(check.ok for check in check_models) and balanced and mismatches == 0,
    )
