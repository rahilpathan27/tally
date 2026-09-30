"""Merchant dashboard routes. Every query runs as ``tally_app`` under the caller's tenant."""

from __future__ import annotations

import base64
import csv
import io
import json
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast
from uuid import UUID

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from services.backoffice_api import approvals
from services.backoffice_api.auth import Principal
from services.backoffice_api.db import audit, scoped
from services.backoffice_api.rbac import require
from services.core.webhooks import CreateWebhookEndpoint, create_endpoint

router = APIRouter(prefix="/bff/v1")
MERCHANT_SCOPES = (
    "payments:read",
    "payments:write",
    "settlements:read",
    "disputes:read",
    "disputes:write",
    "webhooks:read",
    "webhooks:write",
)


def _json(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value[:1] in "[{":
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _row(row: asyncpg.Record) -> dict[str, Any]:
    return {k: _json(v) for k, v in dict(row).items() if not isinstance(v, bytes | memoryview)}


def _pool(request: Request) -> asyncpg.Pool:
    return cast(asyncpg.Pool, request.app.state.pool)


def _cursor(value: str | None) -> tuple[datetime, UUID] | None:
    if not value:
        return None
    try:
        at, payment_id = base64.urlsafe_b64decode(value.encode()).decode().split("|")
        return datetime.fromisoformat(at), UUID(payment_id)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(
            400, detail={"code": "BAD_CURSOR", "message": "Invalid cursor."}
        ) from exc


PAYMENT_FILTERS = """
    ($1::text IS NULL OR p.status = $1)
    AND ($2::text IS NULL OR p.payment_method_type = $2)
    AND ($3::timestamptz IS NULL OR p.created_at >= $3)
    AND ($4::timestamptz IS NULL OR p.created_at < $4)
    AND ($5::bigint IS NULL OR p.amount_minor >= $5)
    AND ($6::bigint IS NULL OR p.amount_minor <= $6)
    AND ($7::text IS NULL OR p.payment_id::text = $7 OR p.payer_vpa ILIKE $7 || '%'
         OR p.payee_vpa ILIKE $7 || '%')
"""


class PaymentQuery(BaseModel):
    status: str | None = None
    method: Literal["card", "upi"] | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    min_amount_minor: int | None = Field(default=None, ge=0)
    max_amount_minor: int | None = Field(default=None, ge=0)
    q: str | None = Field(default=None, max_length=130)


def _filters(query: PaymentQuery) -> list[Any]:
    return [
        query.status,
        query.method,
        query.created_from,
        query.created_to,
        query.min_amount_minor,
        query.max_amount_minor,
        query.q,
    ]


@router.get("/me")
async def me(
    principal: Annotated[Principal, Depends(require("merchant:payments:read"))],
) -> dict[str, Any]:
    return {
        "email": principal.email,
        "roles": sorted(principal.roles),
        "merchant_id": principal.merchant_id,
    }


@router.get("/payments")
async def list_payments(
    request: Request,
    principal: Annotated[Principal, Depends(require("merchant:payments:read"))],
    query: Annotated[PaymentQuery, Depends()],
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    after = _cursor(cursor)
    async with scoped(_pool(request), principal) as connection:
        rows = await connection.fetch(
            f"""SELECT p.payment_id, p.amount_minor, p.currency, p.payment_method_type, p.status,
                       p.payer_vpa, p.payee_vpa, p.created_at, p.succeeded_at,
                       p.risk_outcome->>'decision' AS risk_decision
                FROM payment_intents p
                WHERE {PAYMENT_FILTERS}
                  AND ($8::timestamptz IS NULL OR (p.created_at, p.payment_id) < ($8, $9))
                ORDER BY p.created_at DESC, p.payment_id DESC LIMIT $10""",
            *_filters(query),
            after[0] if after else None,
            after[1] if after else None,
            limit + 1,
        )
    items = [_row(row) for row in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = base64.urlsafe_b64encode(
            f"{last['created_at'].isoformat()}|{last['payment_id']}".encode()
        ).decode()
    return {"items": items, "next_cursor": next_cursor}


@router.get("/payments/export.csv")
async def export_payments(
    request: Request,
    principal: Annotated[Principal, Depends(require("merchant:payments:read"))],
    query: Annotated[PaymentQuery, Depends()],
) -> StreamingResponse:
    async with scoped(_pool(request), principal) as connection:
        rows = await connection.fetch(
            f"""SELECT p.payment_id, p.created_at, p.status, p.payment_method_type,
                       p.amount_minor, p.currency, p.payer_vpa, p.payee_vpa
                FROM payment_intents p WHERE {PAYMENT_FILTERS}
                ORDER BY p.created_at DESC LIMIT 50000""",
            *_filters(query),
        )
        await audit(
            connection, principal, "payments_exported", "payments", json.dumps({"rows": len(rows)})
        )

    async def stream() -> AsyncIterator[bytes]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "payment_id",
                "created_at",
                "status",
                "method",
                "amount_minor",
                "currency",
                "payer_vpa",
                "payee_vpa",
            ]
        )
        for row in rows:
            # Leading =,+,-,@ would be evaluated by spreadsheets (CSV injection).
            writer.writerow(
                [
                    ("'" + str(v)) if isinstance(v, str) and v[:1] in "=+-@" else v
                    for v in (
                        row["payment_id"],
                        row["created_at"].isoformat(),
                        row["status"],
                        row["payment_method_type"],
                        row["amount_minor"],
                        row["currency"].strip(),
                        row["payer_vpa"],
                        row["payee_vpa"],
                    )
                ]
            )
        yield buffer.getvalue().encode()

    return StreamingResponse(
        stream(),
        media_type="text/csv",
        headers={"content-disposition": 'attachment; filename="payments.csv"'},
    )


async def payment_detail(
    connection: asyncpg.Connection, ledger_http: httpx.AsyncClient, payment_id: UUID
) -> dict[str, Any]:
    payment = await connection.fetchrow(
        "SELECT * FROM payment_intents WHERE payment_id = $1", payment_id
    )
    if payment is None:
        raise HTTPException(404, detail={"code": "PAYMENT_NOT_FOUND", "message": "Not found."})
    transitions = await connection.fetch(
        """SELECT from_state, to_state, accepted, actor, reason, occurred_at
           FROM payment_transitions WHERE payment_id = $1 ORDER BY transition_id""",
        payment_id,
    )
    refunds = await connection.fetch(
        "SELECT * FROM refunds WHERE payment_id = $1 ORDER BY created_at", payment_id
    )
    detail = _row(payment)
    detail.pop("payment_method_token", None)
    detail["timeline"] = [_row(t) for t in transitions]
    detail["refunds"] = [_row(r) for r in refunds]
    entries = []
    keys = [
        f"{payment_id}:upi:transfer:post",
        f"{payment_id}:upi:transfer:post:late-success-correction",
    ]
    for key in keys:
        response = await ledger_http.get("/v1/entries/by-key", params={"idempotency_key": key})
        if response.status_code == 200:
            entries.append({"kind": "entry", **response.json()})
    hold = await ledger_http.get(
        "/v1/holds/by-key", params={"idempotency_key": f"{payment_id}:card:authorization:hold"}
    )
    if hold.status_code == 200:
        body = hold.json()
        entries.append({"kind": "hold", **body})
        if body.get("entry_id"):
            captured = await ledger_http.get(f"/v1/entries/{body['entry_id']}")
            if captured.status_code == 200:
                entries.append({"kind": "capture", **captured.json()})
    detail["ledger"] = entries
    return detail


@router.get("/payments/{payment_id}")
async def get_payment(
    payment_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require("merchant:payments:read"))],
) -> dict[str, Any]:
    async with scoped(_pool(request), principal) as connection:
        return await payment_detail(connection, request.app.state.ledger_http, payment_id)


class RefundRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    payment_id: UUID = Field(strict=False)
    amount_minor: int | None = Field(default=None, gt=0, le=9_007_199_254_740_991)
    reason: str | None = Field(default=None, max_length=200)


@router.post("/refunds", status_code=201)
async def create_refund(
    body: RefundRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require("merchant:refunds:create"))],
) -> dict[str, Any]:
    state = request.app.state
    async with scoped(_pool(request), principal) as connection:
        payment = await connection.fetchrow(
            "SELECT amount_minor FROM payment_intents WHERE payment_id = $1", body.payment_id
        )
        threshold = await connection.fetchval(
            "SELECT refund_approval_threshold_minor FROM merchant_settlement_configs"
        )
    if payment is None:
        raise HTTPException(404, detail={"code": "PAYMENT_NOT_FOUND", "message": "Not found."})
    amount = body.amount_minor or int(payment["amount_minor"])
    if threshold is not None and amount >= int(threshold):
        row = await approvals.propose(
            _pool(request),
            principal,
            "refund_high_value",
            f"{body.payment_id}:{amount}",
            {
                "merchant_id": principal.merchant_id,
                "payment_id": str(body.payment_id),
                "amount_minor": amount,
                "reason": body.reason,
            },
        )
        return {"status": "pending_approval", "request_id": str(row["request_id"])}
    response = await state.core_http.post(
        "/internal/v1/refunds",
        json={
            "merchant_id": principal.merchant_id,
            "payment_id": str(body.payment_id),
            "amount_minor": body.amount_minor,
            "reason": body.reason,
        },
        headers={"x-internal-key": state.core_key, "x-actor": principal.email},
    )
    if response.status_code >= 400:
        raise HTTPException(response.status_code, detail=response.json().get("detail"))
    return dict(response.json())


@router.get("/settlements")
async def settlements(
    request: Request,
    principal: Annotated[Principal, Depends(require("merchant:settlements:read"))],
) -> list[dict[str, Any]]:
    async with scoped(_pool(request), principal) as connection:
        rows = await connection.fetch(
            """SELECT s.*, p.status AS payout_status, p.amount_minor AS payout_amount_minor
               FROM settlements s LEFT JOIN payouts p USING (settlement_id)
               ORDER BY s.business_date DESC LIMIT 100"""
        )
    return [_row(row) for row in rows]


@router.get("/settlements/{settlement_id}")
async def settlement(
    settlement_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require("merchant:settlements:read"))],
) -> dict[str, Any]:
    async with scoped(_pool(request), principal) as connection:
        row = await connection.fetchrow(
            "SELECT * FROM settlements WHERE settlement_id = $1", settlement_id
        )
        if row is None:
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Not found."})
        items = await connection.fetch(
            "SELECT * FROM settlement_items WHERE settlement_id = $1 ORDER BY item_type",
            settlement_id,
        )
        payout = await connection.fetchrow(
            "SELECT * FROM payouts WHERE settlement_id = $1", settlement_id
        )
    return {
        **_row(row),
        "items": [_row(i) for i in items],
        "payout": _row(payout) if payout else None,
    }


@router.get("/disputes")
async def disputes(
    request: Request, principal: Annotated[Principal, Depends(require("merchant:payments:read"))]
) -> list[dict[str, Any]]:
    async with scoped(_pool(request), principal) as connection:
        rows = await connection.fetch("SELECT * FROM disputes ORDER BY created_at DESC LIMIT 200")
    return [_row(row) for row in rows]


class EvidenceRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    text: str = Field(min_length=1, max_length=20_000)
    document_base64: str | None = Field(default=None, max_length=1_400_000)
    document_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,100}$")


@router.post("/disputes/{dispute_id}/evidence")
async def dispute_evidence(
    dispute_id: UUID,
    body: EvidenceRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require("merchant:disputes:write"))],
) -> dict[str, Any]:
    state = request.app.state
    response = await state.core_http.post(
        f"/internal/v1/disputes/{dispute_id}/evidence",
        json={"merchant_id": principal.merchant_id, **body.model_dump(exclude_none=True)},
        headers={"x-internal-key": state.core_key, "x-actor": principal.email},
    )
    if response.status_code >= 400:
        raise HTTPException(response.status_code, detail=response.json().get("detail"))
    return dict(response.json())


# API keys: the secret is shown exactly once and stored encrypted; the list never exposes it.
class CreateKey(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    scopes: list[Literal[MERCHANT_SCOPES]] = Field(min_length=1)  # type: ignore[valid-type]
    mode: Literal["test", "live"] = "test"
    expires_in_days: int = Field(default=90, ge=1, le=365)


async def _insert_key(
    request: Request, principal: Principal, scopes: list[str], mode: str, days: int
) -> dict[str, Any]:
    key_id = f"tly_{mode}_{secrets.token_urlsafe(12)}"
    secret = secrets.token_bytes(32)
    await _pool(request).execute(
        """INSERT INTO merchant_api_keys(key_id, merchant_id, secret_ciphertext, scopes, mode,
                                        expires_at)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        key_id,
        principal.merchant_id,
        request.app.state.api_key_cipher.encrypt(key_id, secret),
        sorted(set(scopes)),
        mode,
        datetime.now(UTC) + timedelta(days=days),
    )
    return {
        "key_id": key_id,
        "secret": base64.urlsafe_b64encode(secret).rstrip(b"=").decode(),
        "scopes": sorted(set(scopes)),
        "mode": mode,
    }


@router.get("/api-keys")
async def api_keys(
    request: Request, principal: Annotated[Principal, Depends(require("merchant:keys:manage"))]
) -> list[dict[str, Any]]:
    async with scoped(_pool(request), principal) as connection:
        rows = await connection.fetch(
            """SELECT key_id, scopes, mode, created_at, expires_at, revoked_at
               FROM merchant_api_keys ORDER BY created_at DESC"""
        )
    return [_row(row) for row in rows]


@router.post("/api-keys", status_code=201)
async def create_key(
    body: CreateKey,
    request: Request,
    principal: Annotated[Principal, Depends(require("merchant:keys:manage"))],
) -> dict[str, Any]:
    created = await _insert_key(
        request, principal, list(body.scopes), body.mode, body.expires_in_days
    )
    async with scoped(_pool(request), principal) as connection:
        await audit(
            connection,
            principal,
            "api_key_created",
            created["key_id"],
            json.dumps({"scopes": created["scopes"], "mode": body.mode}),
        )
    return created


async def _owned_key(request: Request, principal: Principal, key_id: str) -> asyncpg.Record:
    async with scoped(_pool(request), principal) as connection:
        row = await connection.fetchrow(
            "SELECT key_id, scopes, mode, revoked_at FROM merchant_api_keys WHERE key_id = $1",
            key_id,
        )
    if row is None or row["revoked_at"] is not None:
        raise HTTPException(404, detail={"code": "KEY_NOT_FOUND", "message": "No active key."})
    return row


@router.post("/api-keys/{key_id}/rotate", status_code=201)
async def rotate_key(
    key_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(require("merchant:keys:manage"))],
) -> dict[str, Any]:
    old = await _owned_key(request, principal, key_id)
    created = await _insert_key(request, principal, list(old["scopes"]), old["mode"], 90)
    # The old key keeps working for 24 hours so integrations can switch without downtime.
    await _pool(request).execute(
        """UPDATE merchant_api_keys SET expires_at = least(expires_at, clock_timestamp()
               + interval '24 hours') WHERE key_id = $1 AND merchant_id = $2""",
        key_id,
        principal.merchant_id,
    )
    async with scoped(_pool(request), principal) as connection:
        await audit(
            connection,
            principal,
            "api_key_rotated",
            key_id,
            json.dumps({"replacement": created["key_id"]}),
        )
    return {**created, "previous_key_expires_in_hours": 24}


@router.post("/api-keys/{key_id}/revoke")
async def revoke_key(
    key_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(require("merchant:keys:manage"))],
) -> dict[str, str]:
    await _owned_key(request, principal, key_id)
    await _pool(request).execute(
        """UPDATE merchant_api_keys SET revoked_at = clock_timestamp()
           WHERE key_id = $1 AND merchant_id = $2 AND revoked_at IS NULL""",
        key_id,
        principal.merchant_id,
    )
    async with scoped(_pool(request), principal) as connection:
        await audit(connection, principal, "api_key_revoked", key_id)
    return {"key_id": key_id, "status": "revoked"}


@router.get("/webhooks")
async def webhooks(
    request: Request,
    principal: Annotated[Principal, Depends(require("merchant:webhooks:manage"))],
) -> list[dict[str, Any]]:
    async with scoped(_pool(request), principal) as connection:
        endpoints = await connection.fetch(
            """SELECT endpoint_id, url, enabled_events, status, created_at, disabled_at
               FROM webhook_endpoints ORDER BY created_at"""
        )
        out = []
        for endpoint in endpoints:
            deliveries = await connection.fetch(
                """SELECT delivery_id, event_id, event_type, status, attempts, last_status_code,
                          last_error, created_at, delivered_at
                   FROM webhook_deliveries WHERE endpoint_id = $1
                   ORDER BY created_at DESC LIMIT 50""",
                endpoint["endpoint_id"],
            )
            out.append({**_row(endpoint), "deliveries": [_row(d) for d in deliveries]})
    return out


@router.post("/webhooks", status_code=201)
async def create_webhook(
    body: CreateWebhookEndpoint,
    request: Request,
    principal: Annotated[Principal, Depends(require("merchant:webhooks:manage"))],
) -> dict[str, Any]:
    from libs.security.ssrf import UnsafeDestination

    assert principal.merchant_id is not None
    try:
        row, secret = await create_endpoint(
            _pool(request),
            request.app.state.webhook_cipher,
            principal.merchant_id,
            body,
            request.app.state.webhook_trusted_hosts,
        )
    except UnsafeDestination as exc:
        raise HTTPException(
            422, detail={"code": "UNSAFE_WEBHOOK_URL", "message": str(exc)}
        ) from exc
    async with scoped(_pool(request), principal) as connection:
        await audit(
            connection,
            principal,
            "webhook_endpoint_created",
            str(row["endpoint_id"]),
            json.dumps({"url": body.url}),
        )
    return {"endpoint_id": str(row["endpoint_id"]), "url": row["url"], "secret": secret}


@router.post("/webhooks/deliveries/{delivery_id}/redeliver")
async def redeliver(
    delivery_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require("merchant:webhooks:manage"))],
) -> dict[str, str]:
    updated = await _pool(request).fetchval(
        """UPDATE webhook_deliveries SET status = 'pending', attempts = 0,
               next_attempt_at = clock_timestamp(), lease_until = NULL
           WHERE delivery_id = $1 AND merchant_id = $2 RETURNING 'pending'""",
        delivery_id,
        principal.merchant_id,
    )
    if updated is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Not found."})
    return {"delivery_id": str(delivery_id), "status": "pending"}


@router.get("/analytics")
async def analytics(
    request: Request,
    principal: Annotated[Principal, Depends(require("merchant:analytics:read"))],
    days: Annotated[int, Query(ge=1, le=90)] = 30,
) -> dict[str, Any]:
    async with scoped(_pool(request), principal) as connection:
        daily = await connection.fetch(
            """SELECT (created_at AT TIME ZONE 'Asia/Kolkata')::date AS day,
                      count(*) AS attempts,
                      count(*) FILTER (WHERE status = 'succeeded') AS succeeded,
                      coalesce(sum(amount_minor) FILTER (WHERE status = 'succeeded'), 0)
                          AS volume_minor
               FROM payment_intents WHERE created_at > clock_timestamp() - make_interval(days => $1)
               GROUP BY 1 ORDER BY 1""",
            days,
        )
        by_method = await connection.fetch(
            """SELECT payment_method_type AS method, count(*) AS attempts,
                      count(*) FILTER (WHERE status = 'succeeded') AS succeeded
               FROM payment_intents WHERE created_at > clock_timestamp() - make_interval(days => $1)
               GROUP BY 1""",
            days,
        )
        by_bank = await connection.fetch(
            """SELECT v.bank_id, count(*) AS attempts,
                      count(*) FILTER (WHERE p.status = 'succeeded') AS succeeded
               FROM payment_intents p JOIN core_vpas v ON v.vpa = p.payer_vpa
               WHERE p.created_at > clock_timestamp() - make_interval(days => $1)
               GROUP BY 1""",
            days,
        )
        latency = await connection.fetchrow(
            """SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY ms) AS p50,
                      percentile_disc(0.95) WITHIN GROUP (ORDER BY ms) AS p95
               FROM (SELECT extract(epoch FROM (max(t.occurred_at) - min(t.occurred_at))) * 1000
                            AS ms
                     FROM payment_transitions t JOIN payment_intents p USING (payment_id)
                     WHERE p.created_at > clock_timestamp() - make_interval(days => $1)
                       AND t.accepted AND t.to_state IN ('created', 'authorized', 'succeeded')
                     GROUP BY t.payment_id HAVING count(*) > 1) d""",
            days,
        )
        refunds = await connection.fetchrow(
            """SELECT count(*) AS refunds, coalesce(sum(amount_minor), 0) AS refunded_minor
               FROM refunds WHERE created_at > clock_timestamp() - make_interval(days => $1)""",
            days,
        )
    assert latency is not None and refunds is not None
    return {
        "daily": [_row(r) | {"day": r["day"].isoformat()} for r in daily],
        "by_method": [_row(r) for r in by_method],
        "by_bank": [_row(r) for r in by_bank],
        "confirm_latency_ms": {"p50": latency["p50"], "p95": latency["p95"]},
        "refunds": _row(refunds),
    }
