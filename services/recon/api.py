"""Reconciliation service API: statement ingestion, runs, break queue, maker-checker, reports.

Callers are back-office services. Each request carries ``x-internal-key`` and ``x-actor`` (the
authenticated back-office user, supplied by the BFF after RBAC in Phase 11).
"""

from __future__ import annotations

import hmac
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from uuid import UUID

import asyncpg
import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from libs.common.object_store import FilesystemObjectStore, s3_store_from_env
from libs.observability.metrics import instrument
from libs.observability.tracing import configure_tracing
from libs.security.http import SecurityMiddleware
from pydantic import BaseModel, ConfigDict, Field

from services.recon.engine import ReconConfig
from services.recon.formats import FORMATS
from services.recon.service import (
    ADJUSTMENTS,
    ReconContext,
    ReconError,
    add_action,
    daily_report,
    decide,
    execute_approved,
    fetch_bank_statement,
    ingest_file,
    propose_adjustment,
    run_recon,
)

Source = Annotated[str, Query(pattern=r"^[a-z0-9-]{1,40}$")]


class RunRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    source: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,40}$")]
    business_date: date = Field(strict=False)


class CommentRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    note: Annotated[str, Field(min_length=1, max_length=2000)]


class AdjustmentRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    action: Literal[
        "book_to_suspense", "reverse_to_suspense", "book_bank_charges", "close_no_entry"
    ]
    note: Annotated[str, Field(min_length=1, max_length=2000)]


class DecisionRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    reason: Annotated[str, Field(min_length=1, max_length=2000)]


def _ctx(request: Request) -> ReconContext:
    return cast(ReconContext, request.app.state.recon_ctx)


def actor(request: Request) -> str:
    expected = cast(str, getattr(request.app.state, "internal_key", ""))
    provided = request.headers.get("x-internal-key", "")
    if not expected or not hmac.compare_digest(provided, expected):
        raise HTTPException(401, detail={"code": "UNAUTHENTICATED", "message": "Invalid key."})
    name = request.headers.get("x-actor", "")
    if not 1 <= len(name) <= 100:
        raise HTTPException(400, detail={"code": "ACTOR_REQUIRED", "message": "x-actor required."})
    return name


Actor = Annotated[str, Depends(actor)]


def _record(row: asyncpg.Record | None) -> dict[str, Any]:
    if row is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Not found."})
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, UUID | date):
            out[key] = str(value) if isinstance(value, UUID) else value.isoformat()
        elif isinstance(value, list):
            out[key] = [str(v) for v in value]
        elif isinstance(value, str) and key in {
            "payload",
            "result",
            "evidence",
            "issues",
            "breaks_by_type",
        }:
            out[key] = json.loads(value)
        elif hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


def create_app(ctx: ReconContext | None = None, internal_key: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if getattr(app.state, "recon_ctx", None) is not None:
            yield
            return
        pool = await asyncpg.create_pool(os.environ["TALLY_DATABASE_URL"], min_size=1, max_size=10)
        ledger_http = httpx.AsyncClient(
            base_url=os.environ.get("TALLY_LEDGER_URL", "http://127.0.0.1:8001"), timeout=30
        )
        store: Any = s3_store_from_env() or FilesystemObjectStore(
            Path(os.environ.get("TALLY_OBJECT_DIR", ".data/objects"))
        )
        app.state.recon_ctx = ReconContext(pool, ledger_http, store, ReconConfig())
        app.state.internal_key = os.environ.get("TALLY_INTERNAL_KEY", "")
        app.state.bank_http = httpx.AsyncClient(
            base_url=os.environ.get("TALLY_BANK_SIM_URL", "http://127.0.0.1:8010"), timeout=30
        )
        try:
            yield
        finally:
            await ledger_http.aclose()
            await app.state.bank_http.aclose()
            await pool.close()

    app = FastAPI(title="Tally Reconciliation", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        SecurityMiddleware, raw_body_paths=("/v1/files",), large_body_paths=("/v1/files",)
    )
    instrument(app, "recon")
    configure_tracing("recon", app)
    if ctx is not None:
        app.state.recon_ctx = ctx
    app.state.internal_key = internal_key or ""

    @app.exception_handler(ReconError)
    async def recon_error(request: Request, exc: ReconError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/files", status_code=201)
    async def upload_file(
        request: Request,
        who: Actor,
        source: Source,
        business_date: date,
        format: Annotated[str, Query()],
    ) -> dict[str, Any]:
        if format not in FORMATS:
            raise ReconError(422, "UNKNOWN_FORMAT", f"format must be one of {', '.join(FORMATS)}")
        data = await request.body()
        if not data or len(data) > 512 * 1024 * 1024:
            raise ReconError(413, "FILE_SIZE", "Statement must be 1 byte to 512 MB.")
        row = await ingest_file(_ctx(request), source, business_date, format, data)
        return _record(row)

    @app.post("/v1/files/fetch", status_code=201)
    async def fetch_from_bank(
        request: Request,
        who: Actor,
        body: RunRequest,
        format: Annotated[str, Query()] = "csv_rupees_ist",
    ) -> dict[str, Any]:
        """Pull a statement from the bank simulator (stands in for an SFTP drop)."""
        data = await fetch_bank_statement(request.app.state.bank_http, body.business_date, format)
        row = await ingest_file(_ctx(request), body.source, body.business_date, format, data)
        return _record(row)

    @app.post("/v1/runs", status_code=201)
    async def create_run(request: Request, who: Actor, body: RunRequest) -> dict[str, Any]:
        return _record(await run_recon(_ctx(request), body.source, body.business_date))

    @app.get("/v1/runs")
    async def list_runs(
        request: Request, who: Actor, limit: Annotated[int, Query(ge=1, le=200)] = 50
    ) -> list[dict[str, Any]]:
        rows = await _ctx(request).pool.fetch(
            "SELECT * FROM recon_runs ORDER BY finished_at DESC LIMIT $1", limit
        )
        return [_record(row) for row in rows]

    @app.get("/v1/breaks")
    async def list_breaks(
        request: Request,
        who: Actor,
        status: str | None = None,
        break_type: str | None = None,
        business_date: date | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        rows = await _ctx(request).pool.fetch(
            """SELECT break_id, source, business_date, break_type, reference, bank_reference,
                      kind, amount_minor, status, suggested_action, detail, sla_due_at,
                      created_at, resolved_at, resolution
               FROM recon_breaks
               WHERE ($1::text IS NULL OR status = $1)
                 AND ($2::text IS NULL OR break_type = $2)
                 AND ($3::date IS NULL OR business_date = $3)
               ORDER BY created_at, break_id LIMIT $4""",
            status,
            break_type,
            business_date,
            limit,
        )
        return [_record(row) for row in rows]

    @app.get("/v1/breaks/{break_id}")
    async def break_detail(break_id: UUID, request: Request, who: Actor) -> dict[str, Any]:
        pool = _ctx(request).pool
        detail = _record(
            await pool.fetchrow("SELECT * FROM recon_breaks WHERE break_id = $1", break_id)
        )
        actions = await pool.fetch(
            "SELECT actor, action, note, created_at FROM break_actions WHERE break_id = $1 "
            "ORDER BY action_id",
            break_id,
        )
        detail["actions"] = [_record(a) for a in actions]
        detail["adjustments_available"] = ADJUSTMENTS
        return detail

    @app.post("/v1/breaks/{break_id}/comments", status_code=201)
    async def comment(
        break_id: UUID, body: CommentRequest, request: Request, who: Actor
    ) -> dict[str, str]:
        async with _ctx(request).pool.acquire() as connection:
            exists = await connection.fetchval(
                "SELECT 1 FROM recon_breaks WHERE break_id = $1", break_id
            )
            if not exists:
                raise ReconError(404, "BREAK_NOT_FOUND", "Break was not found.")
            await add_action(connection, break_id, who, "comment", body.note)
        return {"status": "recorded"}

    @app.post("/v1/breaks/{break_id}/adjustments", status_code=201)
    async def adjust(
        break_id: UUID, body: AdjustmentRequest, request: Request, who: Actor
    ) -> dict[str, Any]:
        return _record(
            await propose_adjustment(_ctx(request), break_id, who, body.action, body.note)
        )

    @app.get("/v1/approvals")
    async def approvals(
        request: Request, who: Actor, status: str = "pending"
    ) -> list[dict[str, Any]]:
        rows = await _ctx(request).pool.fetch(
            "SELECT * FROM maker_checker_requests WHERE status = $1 ORDER BY created_at", status
        )
        return [_record(row) for row in rows]

    @app.post("/v1/approvals/{request_id}/approve")
    async def approve(
        request_id: UUID, body: DecisionRequest, request: Request, who: Actor
    ) -> dict[str, Any]:
        return _record(await decide(_ctx(request), request_id, who, True, body.reason))

    @app.post("/v1/approvals/{request_id}/reject")
    async def reject(
        request_id: UUID, body: DecisionRequest, request: Request, who: Actor
    ) -> dict[str, Any]:
        return _record(await decide(_ctx(request), request_id, who, False, body.reason))

    @app.post("/v1/approvals/{request_id}/execute")
    async def execute(request_id: UUID, request: Request, who: Actor) -> dict[str, Any]:
        return _record(await execute_approved(_ctx(request), request_id))

    @app.get("/v1/reports/daily")
    async def report(request: Request, who: Actor, business_date: date) -> dict[str, Any]:
        return await daily_report(_ctx(request).pool, business_date)

    return app


app = create_app()
