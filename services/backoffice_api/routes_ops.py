"""Operations, risk and approvals console routes (platform roles; cross-tenant, audited)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from services.backoffice_api import approvals
from services.backoffice_api.auth import Principal
from services.backoffice_api.db import audit, scoped
from services.backoffice_api.rbac import require
from services.backoffice_api.routes_merchant import _row, payment_detail
from services.workers import aml

router = APIRouter(prefix="/bff/v1/ops")


def _pool(request: Request) -> asyncpg.Pool:
    return cast(asyncpg.Pool, request.app.state.pool)


async def _proxy(
    request: Request,
    principal: Principal,
    service: str,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    params: dict[str, Any] | None = None,
    content: bytes | None = None,
) -> Any:
    client: httpx.AsyncClient = getattr(request.app.state, f"{service}_http")
    headers = {
        "x-internal-key": getattr(request.app.state, f"{service}_key", ""),
        "x-actor": principal.email,
    }
    response = await client.request(
        method,
        path,
        json=json_body,
        params={k: v for k, v in (params or {}).items() if v is not None},
        content=content,
        headers=headers,
    )
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = {"code": "UPSTREAM_ERROR", "message": response.text[:200]}
        raise HTTPException(response.status_code, detail=detail)
    return response.json()


# Payments ------------------------------------------------------------------------------------
@router.get("/payments")
async def search_payments(
    request: Request,
    principal: Annotated[Principal, Depends(require("ops:payments:read"))],
    q: str | None = None,
    status: str | None = None,
    merchant_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    async with scoped(_pool(request), principal) as connection:
        rows = await connection.fetch(
            """SELECT payment_id, merchant_id, amount_minor, payment_method_type, status,
                      payer_vpa, payee_vpa, created_at, updated_at
               FROM payment_intents
               WHERE ($1::text IS NULL OR payment_id::text = $1 OR payer_vpa = $1)
                 AND ($2::text IS NULL OR status = $2)
                 AND ($3::text IS NULL OR merchant_id = $3)
               ORDER BY created_at DESC LIMIT $4""",
            q,
            status,
            merchant_id,
            limit,
        )
    return [_row(row) for row in rows]


@router.get("/payments/{payment_id}")
async def payment(
    payment_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require("ops:payments:read"))],
) -> dict[str, Any]:
    async with scoped(_pool(request), principal) as connection:
        detail = await payment_detail(connection, request.app.state.ledger_http, payment_id)
        await audit(
            connection,
            principal,
            "payment_viewed",
            str(payment_id),
            merchant_id=detail.get("merchant_id"),
        )
    return detail


class RefundAction(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    action: str = Field(pattern=r"^(retry|cancel)$")


@router.post("/refunds/{refund_id}/action")
async def refund_action(
    refund_id: UUID,
    body: RefundAction,
    request: Request,
    principal: Annotated[Principal, Depends(require("ops:refunds:act"))],
) -> Any:
    result = await _proxy(
        request, principal, "core", "POST", f"/internal/v1/refunds/{refund_id}/{body.action}"
    )
    async with scoped(_pool(request), principal) as connection:
        await audit(connection, principal, f"refund_{body.action}", str(refund_id))
    return result


# Switch monitor ------------------------------------------------------------------------------
async def switch_snapshot(
    pool: asyncpg.Pool, core_http: httpx.AsyncClient, key: str
) -> dict[str, Any]:
    in_flight = await pool.fetch(
        """SELECT payment_id, merchant_id, status, payment_method_type, amount_minor,
                  extract(epoch FROM clock_timestamp() - updated_at)::int AS age_seconds,
                  recovery_attempts, recovery_deadline
           FROM payment_intents
           WHERE status IN ('authorizing', 'pending_unknown', 'reversal_pending', 'capturing',
                            'risk_review')
           ORDER BY updated_at LIMIT 200"""
    )
    banks = await pool.fetch(
        """SELECT v.bank_id,
                  count(*) FILTER (WHERE t.to_state = 'succeeded') AS succeeded,
                  count(*) FILTER (WHERE t.to_state IN ('failed', 'reversed')) AS failed,
                  count(*) FILTER (WHERE t.to_state = 'pending_unknown') AS unknown
           FROM payment_transitions t
           JOIN payment_intents p USING (payment_id)
           JOIN core_vpas v ON v.vpa = p.payer_vpa
           WHERE t.accepted AND t.occurred_at > clock_timestamp() - interval '1 hour'
           GROUP BY v.bank_id ORDER BY v.bank_id"""
    )
    try:
        response = await core_http.get("/internal/v1/switch/state", headers={"x-internal-key": key})
        breakers = response.json() if response.status_code == 200 else {}
    except httpx.HTTPError:
        breakers = {"error": "core unavailable"}
    return {
        "in_flight": [_row(r) for r in in_flight],
        "banks": [
            {
                **_row(b),
                "success_rate_bps": 0
                if (b["succeeded"] + b["failed"]) == 0
                else b["succeeded"] * 10_000 // (b["succeeded"] + b["failed"]),
            }
            for b in banks
        ],
        "breakers": breakers,
    }


@router.get("/switch")
async def switch(
    request: Request, principal: Annotated[Principal, Depends(require("ops:switch:read"))]
) -> dict[str, Any]:
    state = request.app.state
    return await switch_snapshot(_pool(request), state.core_http, state.core_key)


@router.get("/switch/stream")
async def switch_stream(
    request: Request, principal: Annotated[Principal, Depends(require("ops:switch:read"))]
) -> StreamingResponse:
    state = request.app.state
    interval = float(getattr(state, "sse_interval_seconds", 2.0))

    async def events() -> AsyncIterator[bytes]:
        for _ in range(int(getattr(state, "sse_max_events", 1_800))):
            if await request.is_disconnected():
                break
            snapshot = await switch_snapshot(_pool(request), state.core_http, state.core_key)
            yield f"event: switch\ndata: {json.dumps(snapshot, default=str)}\n\n".encode()
            await asyncio.sleep(interval)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        # no-transform stops proxies (including the console's dev rewrite) compressing and
        # therefore buffering the stream.
        headers={"x-accel-buffering": "no", "cache-control": "no-cache, no-transform"},
    )


# Ledger explorer -----------------------------------------------------------------------------
@router.get("/ledger/{path:path}")
async def ledger(
    path: str,
    request: Request,
    principal: Annotated[Principal, Depends(require("ops:ledger:read"))],
) -> Any:
    allowed = ("accounts", "trial-balance", "entries/", "integrity")
    if not path.startswith(allowed) or ".." in path:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Unknown ledger view."})
    return await _proxy(
        request, principal, "ledger", "GET", f"/v1/{path}", params=dict(request.query_params)
    )


class AdjustmentProposal(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    postings: list[dict[str, Any]] = Field(min_length=2, max_length=20)
    reason: str = Field(min_length=5, max_length=500)


@router.post("/ledger/adjustments", status_code=201)
async def propose_adjustment(
    body: AdjustmentProposal,
    request: Request,
    principal: Annotated[Principal, Depends(require("ops:ledger:adjust"))],
) -> dict[str, Any]:
    debit = sum(
        int(p.get("amount_minor", 0)) for p in body.postings if p.get("direction") == "debit"
    )
    credit = sum(
        int(p.get("amount_minor", 0)) for p in body.postings if p.get("direction") == "credit"
    )
    if debit != credit or debit <= 0:
        raise HTTPException(
            422, detail={"code": "UNBALANCED", "message": "Debits must equal credits."}
        )
    row = await approvals.propose(
        _pool(request),
        principal,
        "ledger_adjustment",
        f"adjust:{uuid4()}",
        body.model_dump(),
    )
    return _row(row)


# Reconciliation (proxied to the recon service with the caller's identity) ---------------------
@router.get("/recon/{path:path}")
async def recon_read(
    path: str,
    request: Request,
    principal: Annotated[Principal, Depends(require("ops:recon:read"))],
) -> Any:
    if not path.startswith(("runs", "breaks", "reports/daily")) or ".." in path:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Unknown recon view."})
    return await _proxy(
        request, principal, "recon", "GET", f"/v1/{path}", params=dict(request.query_params)
    )


@router.post("/recon/{path:path}")
async def recon_act(
    path: str,
    request: Request,
    principal: Annotated[Principal, Depends(require("ops:recon:act"))],
) -> Any:
    if not path.startswith(("runs", "breaks/", "files/fetch")) or ".." in path:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Unknown recon action."})
    body = await request.json() if await request.body() else None
    return await _proxy(
        request,
        principal,
        "recon",
        "POST",
        f"/v1/{path}",
        json_body=body,
        params=dict(request.query_params),
    )


# Risk console --------------------------------------------------------------------------------
@router.get("/risk/{path:path}")
async def risk_read(
    path: str,
    request: Request,
    principal: Annotated[Principal, Depends(require("risk:read"))],
) -> Any:
    if not path.startswith(("reviews", "decisions", "rules", "models", "model-performance")):
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Unknown risk view."})
    return await _proxy(
        request, principal, "risk", "GET", f"/v1/{path}", params=dict(request.query_params)
    )


class ReviewDecision(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    outcome: str = Field(pattern=r"^(approve|decline)$")
    note: str = Field(min_length=1, max_length=2000)


@router.post("/risk/reviews/{case_id}/resolve")
async def resolve_review(
    case_id: UUID,
    body: ReviewDecision,
    request: Request,
    principal: Annotated[Principal, Depends(require("risk:reviews:act"))],
) -> Any:
    return await _proxy(
        request,
        principal,
        "risk",
        "POST",
        f"/v1/reviews/{case_id}/resolve",
        json_body=body.model_dump(),
    )


@router.post("/risk/rules/proposals", status_code=201)
async def propose_rules(
    request: Request, principal: Annotated[Principal, Depends(require("risk:rules:propose"))]
) -> Any:
    return await _proxy(
        request, principal, "risk", "POST", "/v1/rules/proposals", json_body=await request.json()
    )


@router.post("/risk/models/{version}/champion-proposals", status_code=201)
async def propose_champion(
    version: str,
    request: Request,
    principal: Annotated[Principal, Depends(require("risk:models:propose"))],
) -> Any:
    return await _proxy(
        request, principal, "risk", "POST", f"/v1/models/{version}/champion-proposals"
    )


@router.post("/risk/drift/run")
async def run_drift(
    request: Request, principal: Annotated[Principal, Depends(require("risk:read"))]
) -> Any:
    return await _proxy(
        request,
        principal,
        "risk",
        "POST",
        "/v1/drift/run",
        params={"sample": request.query_params.get("sample", "5000")},
    )


# AML-lite ------------------------------------------------------------------------------------
@router.get("/aml/alerts")
async def aml_alerts(
    request: Request,
    principal: Annotated[Principal, Depends(require("aml:read"))],
    status: str = "open",
) -> list[dict[str, Any]]:
    async with scoped(_pool(request), principal) as connection:
        rows = await connection.fetch(
            "SELECT * FROM aml_alerts WHERE status = $1 ORDER BY created_at DESC LIMIT 200", status
        )
    return [_row(row) for row in rows]


@router.post("/aml/run")
async def aml_run(
    request: Request, principal: Annotated[Principal, Depends(require("aml:act"))]
) -> dict[str, int]:
    created = await aml.run_detectors(_pool(request))
    async with scoped(_pool(request), principal) as connection:
        await audit(connection, principal, "aml_run", "aml", json.dumps(created))
    return created


class CaseRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    alert_ids: list[Annotated[UUID, Field(strict=False)]] = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=5, max_length=2000)


@router.post("/aml/cases", status_code=201)
async def aml_case(
    body: CaseRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require("aml:act"))],
) -> dict[str, Any]:
    case_id = await aml.open_case(_pool(request), body.alert_ids, body.summary, principal.email)
    async with scoped(_pool(request), principal) as connection:
        await audit(
            connection,
            principal,
            "aml_case_opened",
            str(case_id),
            json.dumps({"alerts": [str(a) for a in body.alert_ids]}),
        )
    return {"case_id": str(case_id)}


# Limits and risk policy (dual control) -------------------------------------------------------
@router.post("/limits", status_code=201)
async def propose_limits(
    body: approvals.LimitChange,
    request: Request,
    principal: Annotated[Principal, Depends(require("limits:propose"))],
) -> dict[str, Any]:
    row = await approvals.propose(
        _pool(request), principal, "limit_change", body.merchant_id, body.model_dump()
    )
    return _row(row)


@router.post("/risk-policies", status_code=201)
async def propose_risk_policy(
    body: approvals.RiskPolicy,
    request: Request,
    principal: Annotated[Principal, Depends(require("limits:propose"))],
) -> dict[str, Any]:
    row = await approvals.propose(
        _pool(request), principal, "merchant_risk_policy", body.merchant_id, body.model_dump()
    )
    return _row(row)


# Approvals inbox -----------------------------------------------------------------------------
@router.get("/approvals")
async def approvals_inbox(
    request: Request,
    principal: Annotated[Principal, Depends(require("approvals:read"))],
    status: str = "pending",
) -> list[dict[str, Any]]:
    async with scoped(_pool(request), principal) as connection:
        rows = await connection.fetch(
            "SELECT * FROM maker_checker_requests WHERE status = $1 ORDER BY created_at", status
        )
    return [_row(row) | {"can_decide": row["maker"] != principal.email} for row in rows]


class DecisionBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)


@router.post("/approvals/{request_id}/{verb}")
async def decide(
    request_id: UUID,
    verb: str,
    body: DecisionBody,
    request: Request,
    principal: Annotated[Principal, Depends(require("approvals:decide"))],
) -> dict[str, Any]:
    if verb not in {"approve", "reject"}:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Unknown decision."})
    return await approvals.decide(
        request.app.state, request_id, principal, verb == "approve", body.reason
    )


# Audit ---------------------------------------------------------------------------------------
@router.get("/audit")
async def audit_log(
    request: Request,
    principal: Annotated[Principal, Depends(require("audit:read"))],
    before: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    async with scoped(_pool(request), principal) as connection:
        rows = await connection.fetch(
            """SELECT seq, occurred_at, actor, action, subject, merchant_id, details,
                      encode(entry_hash, 'hex') AS entry_hash
               FROM audit_log WHERE ($1::bigint IS NULL OR seq < $1)
               ORDER BY seq DESC LIMIT $2""",
            before,
            limit,
        )
        verified = await connection.fetchrow("SELECT * FROM audit_verify()")
        anchor = await connection.fetchrow(
            "SELECT seq, encode(entry_hash, 'hex') AS entry_hash, object_key, anchored_at "
            "FROM audit_anchors ORDER BY seq DESC LIMIT 1"
        )
    assert verified is not None
    return {
        "items": [_row(r) for r in rows],
        "chain": {
            "ok": verified["ok"],
            "entries": verified["entries"],
            "broken_at": verified["broken_at"],
        },
        "latest_anchor": _row(anchor) if anchor else None,
    }


# Chaos control (demo/staging only) -----------------------------------------------------------
class ChaosModes(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    bank_modes: dict[str, str] = Field(default_factory=dict)
    refund_mode: str | None = None
    payout_mode: str | None = None
    card_network_mode: str | None = None
    payer_psp_mode: str | None = None


@router.post("/chaos")
async def chaos(
    body: ChaosModes,
    request: Request,
    principal: Annotated[Principal, Depends(require("chaos:control"))],
) -> dict[str, Any]:
    state = request.app.state
    if not getattr(state, "chaos_enabled", False):
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Chaos control is off."})
    result: dict[str, Any] = {}
    for service, payload in (
        (
            "bank",
            {
                "modes": body.bank_modes,
                "refund_mode": body.refund_mode,
                "payout_mode": body.payout_mode,
            },
        ),
        ("network", {"mode": body.card_network_mode}),
        ("psp", {"mode": body.payer_psp_mode}),
    ):
        client: httpx.AsyncClient | None = getattr(state, f"sim_{service}_http", None)
        if client is None:
            continue
        response = await client.post(
            "/internal/v1/modes",
            json=payload,
            headers={"x-simulator-admin": state.simulator_admin_key},
        )
        if response.status_code != 200:
            raise HTTPException(
                502,
                detail={
                    "code": "SIMULATOR_REJECTED",
                    "message": f"{service}: {response.status_code} {response.text[:200]}",
                },
            )
        result[service] = response.json()
    async with scoped(_pool(request), principal) as connection:
        await audit(
            connection, principal, "chaos_modes_set", "simulators", json.dumps(body.model_dump())
        )
    return result
