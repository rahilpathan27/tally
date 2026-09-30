"""HTTP boundary for the independent PostgreSQL ledger."""

from __future__ import annotations

import hmac
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, cast

import asyncpg
from fastapi import FastAPI, Header, HTTPException, Path, Request, status
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
        await connection.execute(
            """INSERT INTO ledger_accounts(account_id, account_type, currency, allow_negative)
               VALUES ($1, 'liability', $2, false) ON CONFLICT (account_id) DO NOTHING""",
            account_id,
            body.currency,
        )
        existing = await connection.fetchrow(
            """SELECT account_type::text, currency, allow_negative, closed FROM ledger_accounts
               WHERE account_id = $1 FOR UPDATE""",
            account_id,
        )
        if (
            existing is None
            or existing["account_type"] != "liability"
            or existing["currency"].strip() != body.currency
            or existing["allow_negative"]
            or existing["closed"]
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT, "merchant ledger account conflicts with chart"
            )
        await connection.execute(
            "INSERT INTO ledger_account_balances(account_id) VALUES ($1) ON CONFLICT DO NOTHING",
            account_id,
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
