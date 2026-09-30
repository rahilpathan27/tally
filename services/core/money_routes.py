"""Merchant and internal HTTP routes for refunds, settlements, disputes and webhooks."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, Request, status
from libs.security.ssrf import UnsafeDestination
from pydantic import BaseModel, ConfigDict, Field

from services.api_gateway.auth import MerchantPrincipal
from services.api_gateway.route import GatewayRoute, requires_scope
from services.core.disputes import (
    DisputeError,
    DisputeView,
    OpenDispute,
    ResolveDispute,
    SubmitEvidence,
    dispute_view,
    open_dispute,
    resolve_dispute,
    submit_evidence,
)
from services.core.money_ops import (
    LedgerUnavailable,
    MerchantBusy,
    MoneyContext,
    sweep_money_commands,
)
from services.core.refunds import (
    CreateRefund,
    RefundError,
    RefundResponse,
    cancel_refund,
    create_refund,
    refund_response,
    refund_worker_batch,
    retry_refund,
)
from services.core.settlement import (
    SettlementView,
    payout_worker_batch,
    run_settlement,
    settle_due_merchants,
    settlement_view,
)
from services.core.webhooks import (
    CreateWebhookEndpoint,
    DeliveryView,
    WebhookDispatcher,
    WebhookEndpointView,
    create_endpoint,
    delivery_view,
    endpoint_view,
    queue_test_event,
    redeliver,
    relay_outbox,
)

ResourceId = Annotated[UUID, Path()]
merchant_router = APIRouter(route_class=GatewayRoute)
internal_router = APIRouter(include_in_schema=False)


def _ctx(request: Request) -> MoneyContext:
    return cast(MoneyContext, request.app.state.money_ctx)


def _principal(request: Request) -> MerchantPrincipal:
    return cast(MerchantPrincipal, request.state.merchant_principal)


def _object_writer(request: Request) -> Callable[[str, bytes], Awaitable[Any]] | None:
    store = getattr(request.app.state, "object_store", None)
    return None if store is None else store.put


@asynccontextmanager
async def _domain_errors() -> AsyncIterator[None]:
    try:
        yield
    except (RefundError, DisputeError) as exc:
        raise HTTPException(
            exc.status_code, detail={"code": exc.code, "message": exc.message}
        ) from exc
    except MerchantBusy as exc:
        # 5xx so the gateway releases the idempotency reservation and the retry can run.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "MERCHANT_OPERATION_IN_PROGRESS", "message": "Retry shortly."},
            headers={"Retry-After": "1"},
        ) from exc
    except LedgerUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "LEDGER_UNAVAILABLE", "message": "Ledger is unavailable."},
        ) from exc


def _not_found(code: str) -> HTTPException:
    return HTTPException(404, detail={"code": code, "message": "Resource was not found."})


# Refunds -----------------------------------------------------------------------------------
@merchant_router.post("/v1/refunds", response_model=RefundResponse, status_code=201)
@requires_scope("payments:write")
async def create_refund_route(body: CreateRefund, request: Request) -> RefundResponse:
    principal = _principal(request)
    async with _domain_errors():
        row = await create_refund(_ctx(request), principal.merchant_id, body, principal.key_id)
    return refund_response(row)


@merchant_router.get("/v1/refunds/{refund_id}", response_model=RefundResponse)
@requires_scope("payments:read")
async def get_refund(refund_id: ResourceId, request: Request) -> RefundResponse:
    row = await _ctx(request).pool.fetchrow(
        "SELECT * FROM refunds WHERE refund_id = $1 AND merchant_id = $2",
        refund_id,
        _principal(request).merchant_id,
    )
    if row is None:
        raise _not_found("REFUND_NOT_FOUND")
    return refund_response(row)


@merchant_router.get(
    "/v1/payment_intents/{payment_id}/refunds", response_model=list[RefundResponse]
)
@requires_scope("payments:read")
async def list_payment_refunds(payment_id: ResourceId, request: Request) -> list[RefundResponse]:
    rows = await _ctx(request).pool.fetch(
        """SELECT * FROM refunds WHERE payment_id = $1 AND merchant_id = $2
           ORDER BY created_at""",
        payment_id,
        _principal(request).merchant_id,
    )
    return [refund_response(row) for row in rows]


# Settlements -------------------------------------------------------------------------------
@merchant_router.get("/v1/settlements", response_model=list[SettlementView])
@requires_scope("settlements:read")
async def list_settlements(
    request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 30
) -> list[SettlementView]:
    rows = await _ctx(request).pool.fetch(
        """SELECT * FROM settlements WHERE merchant_id = $1
           ORDER BY business_date DESC LIMIT $2""",
        _principal(request).merchant_id,
        limit,
    )
    return [settlement_view(row) for row in rows]


@merchant_router.get("/v1/settlements/{settlement_id}", response_model=SettlementView)
@requires_scope("settlements:read")
async def get_settlement(settlement_id: ResourceId, request: Request) -> SettlementView:
    pool = _ctx(request).pool
    row = await pool.fetchrow(
        "SELECT * FROM settlements WHERE settlement_id = $1 AND merchant_id = $2",
        settlement_id,
        _principal(request).merchant_id,
    )
    if row is None:
        raise _not_found("SETTLEMENT_NOT_FOUND")
    items = await pool.fetch(
        "SELECT * FROM settlement_items WHERE settlement_id = $1 ORDER BY item_type, item_id",
        settlement_id,
    )
    payout = await pool.fetchrow("SELECT * FROM payouts WHERE settlement_id = $1", settlement_id)
    return settlement_view(row, items, payout)


# Disputes ----------------------------------------------------------------------------------
@merchant_router.get("/v1/disputes", response_model=list[DisputeView])
@requires_scope("disputes:read")
async def list_disputes(request: Request) -> list[DisputeView]:
    rows = await _ctx(request).pool.fetch(
        "SELECT * FROM disputes WHERE merchant_id = $1 ORDER BY created_at DESC LIMIT 100",
        _principal(request).merchant_id,
    )
    return [dispute_view(row) for row in rows]


@merchant_router.get("/v1/disputes/{dispute_id}", response_model=DisputeView)
@requires_scope("disputes:read")
async def get_dispute(dispute_id: ResourceId, request: Request) -> DisputeView:
    row = await _ctx(request).pool.fetchrow(
        "SELECT * FROM disputes WHERE dispute_id = $1 AND merchant_id = $2",
        dispute_id,
        _principal(request).merchant_id,
    )
    if row is None:
        raise _not_found("DISPUTE_NOT_FOUND")
    return dispute_view(row)


@merchant_router.post("/v1/disputes/{dispute_id}/evidence", response_model=DisputeView)
@requires_scope("disputes:write")
async def dispute_evidence(
    dispute_id: ResourceId, body: SubmitEvidence, request: Request
) -> DisputeView:
    async with _domain_errors():
        row = await submit_evidence(
            _ctx(request),
            _principal(request).merchant_id,
            dispute_id,
            body,
            _object_writer(request),
        )
    return dispute_view(row)


# Webhooks ----------------------------------------------------------------------------------
@merchant_router.post("/v1/webhook_endpoints", response_model=WebhookEndpointView, status_code=201)
@requires_scope("webhooks:write")
async def create_webhook_endpoint(
    body: CreateWebhookEndpoint, request: Request
) -> WebhookEndpointView:
    try:
        row, secret = await create_endpoint(
            _ctx(request).pool,
            request.app.state.webhook_cipher,
            _principal(request).merchant_id,
            body,
            request.app.state.webhook_trusted_hosts,
        )
    except UnsafeDestination as exc:
        raise HTTPException(
            422, detail={"code": "UNSAFE_WEBHOOK_URL", "message": str(exc)}
        ) from exc
    return endpoint_view(row, secret)  # the secret is returned exactly once


@merchant_router.get("/v1/webhook_endpoints", response_model=list[WebhookEndpointView])
@requires_scope("webhooks:read")
async def list_webhook_endpoints(request: Request) -> list[WebhookEndpointView]:
    rows = await _ctx(request).pool.fetch(
        "SELECT * FROM webhook_endpoints WHERE merchant_id = $1 ORDER BY created_at",
        _principal(request).merchant_id,
    )
    return [endpoint_view(row) for row in rows]


@merchant_router.post(
    "/v1/webhook_endpoints/{endpoint_id}/disable", response_model=WebhookEndpointView
)
@requires_scope("webhooks:write")
async def disable_webhook_endpoint(
    endpoint_id: ResourceId, request: Request
) -> WebhookEndpointView:
    row = await _ctx(request).pool.fetchrow(
        """UPDATE webhook_endpoints SET status = 'disabled', disabled_at = clock_timestamp()
           WHERE endpoint_id = $1 AND merchant_id = $2 RETURNING *""",
        endpoint_id,
        _principal(request).merchant_id,
    )
    if row is None:
        raise _not_found("WEBHOOK_ENDPOINT_NOT_FOUND")
    return endpoint_view(row)


@merchant_router.get(
    "/v1/webhook_endpoints/{endpoint_id}/deliveries", response_model=list[DeliveryView]
)
@requires_scope("webhooks:read")
async def list_deliveries(
    endpoint_id: ResourceId,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[DeliveryView]:
    rows = await _ctx(request).pool.fetch(
        """SELECT * FROM webhook_deliveries WHERE endpoint_id = $1 AND merchant_id = $2
           ORDER BY created_at DESC LIMIT $3""",
        endpoint_id,
        _principal(request).merchant_id,
        limit,
    )
    return [delivery_view(row) for row in rows]


@merchant_router.post("/v1/webhook_deliveries/{delivery_id}/redeliver", response_model=DeliveryView)
@requires_scope("webhooks:write")
async def redeliver_route(delivery_id: ResourceId, request: Request) -> DeliveryView:
    try:
        row = await redeliver(_ctx(request).pool, _principal(request).merchant_id, delivery_id)
    except KeyError as exc:
        raise _not_found("WEBHOOK_DELIVERY_NOT_FOUND") from exc
    return delivery_view(row)


@merchant_router.post("/v1/webhook_endpoints/{endpoint_id}/test", response_model=DeliveryView)
@requires_scope("webhooks:write")
async def test_webhook_endpoint(endpoint_id: ResourceId, request: Request) -> DeliveryView:
    pool = _ctx(request).pool
    try:
        delivery_id = await queue_test_event(pool, _principal(request).merchant_id, endpoint_id)
    except KeyError as exc:
        raise _not_found("WEBHOOK_ENDPOINT_NOT_FOUND") from exc
    dispatcher: WebhookDispatcher = request.app.state.webhook_dispatcher
    rows = await pool.fetch(
        """SELECT d.*, e.url, e.secret_ciphertext, e.status AS endpoint_status
           FROM webhook_deliveries d JOIN webhook_endpoints e USING (endpoint_id)
           WHERE d.delivery_id = $1""",
        delivery_id,
    )
    await dispatcher.deliver(rows[0])
    row = await pool.fetchrow(
        "SELECT * FROM webhook_deliveries WHERE delivery_id = $1", delivery_id
    )
    assert row is not None
    return delivery_view(row)


# Internal operations (ops console / simulators / schedulers) -------------------------------
def require_internal(request: Request) -> None:
    expected = cast(str, request.app.state.recovery_key)
    provided = request.headers.get("x-internal-key", "")
    if not expected or not hmac.compare_digest(provided, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid internal credential")


class RunSettlement(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    merchant_id: Annotated[str, Field(min_length=1, max_length=64)]
    business_date: date = Field(strict=False)


@internal_router.post("/internal/v1/settlements/run", response_model=SettlementView)
async def run_settlement_route(body: RunSettlement, request: Request) -> SettlementView:
    require_internal(request)
    async with _domain_errors():
        row = await run_settlement(_ctx(request), body.merchant_id, body.business_date)
    return settlement_view(row)


@internal_router.post("/internal/v1/disputes", response_model=DisputeView, status_code=201)
async def open_dispute_route(body: OpenDispute, request: Request) -> DisputeView:
    require_internal(request)
    async with _domain_errors():
        row = await open_dispute(_ctx(request), body)
    return dispute_view(row)


@internal_router.post("/internal/v1/disputes/{dispute_id}/resolve", response_model=DisputeView)
async def resolve_dispute_route(
    dispute_id: ResourceId, body: ResolveDispute, request: Request
) -> DisputeView:
    require_internal(request)
    async with _domain_errors():
        row = await resolve_dispute(_ctx(request), dispute_id, body.outcome)
    return dispute_view(row)


@internal_router.post("/internal/v1/refunds/{refund_id}/retry", response_model=RefundResponse)
async def retry_refund_route(refund_id: ResourceId, request: Request) -> RefundResponse:
    require_internal(request)
    actor = request.headers.get("x-actor", "ops")
    async with _domain_errors():
        async with _ctx(request).pool.acquire():
            row = await retry_refund(_ctx(request), refund_id, actor)
    return refund_response(row)


@internal_router.post("/internal/v1/refunds/{refund_id}/cancel", response_model=RefundResponse)
async def cancel_refund_route(refund_id: ResourceId, request: Request) -> RefundResponse:
    require_internal(request)
    async with _domain_errors():
        row = await cancel_refund(_ctx(request), refund_id, request.headers.get("x-actor", "ops"))
    return refund_response(row)


class InternalRefund(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    merchant_id: Annotated[str, Field(min_length=1, max_length=64)]
    payment_id: UUID = Field(strict=False)
    amount_minor: Annotated[int, Field(gt=0, le=9_007_199_254_740_991)] | None = None
    reason: Annotated[str, Field(max_length=200)] | None = None


@internal_router.post("/internal/v1/refunds", response_model=RefundResponse, status_code=201)
async def internal_refund(body: InternalRefund, request: Request) -> RefundResponse:
    """Refund created from the back office (after maker-checker when above threshold)."""
    require_internal(request)
    actor = request.headers.get("x-actor", "backoffice")
    async with _domain_errors():
        row = await create_refund(
            _ctx(request),
            body.merchant_id,
            CreateRefund(
                payment_id=body.payment_id, amount_minor=body.amount_minor, reason=body.reason
            ),
            actor,
        )
    return refund_response(row)


class InternalEvidence(SubmitEvidence):
    merchant_id: Annotated[str, Field(min_length=1, max_length=64)]


@internal_router.post("/internal/v1/disputes/{dispute_id}/evidence", response_model=DisputeView)
async def internal_evidence(
    dispute_id: ResourceId, body: InternalEvidence, request: Request
) -> DisputeView:
    require_internal(request)
    evidence = SubmitEvidence(
        text=body.text, document_base64=body.document_base64, document_name=body.document_name
    )
    async with _domain_errors():
        row = await submit_evidence(
            _ctx(request), body.merchant_id, dispute_id, evidence, _object_writer(request)
        )
    return dispute_view(row)


@internal_router.get("/internal/v1/switch/state")
async def switch_state(request: Request) -> dict[str, Any]:
    """Circuit-breaker state for the ops switch monitor (process-local breakers)."""
    require_internal(request)
    state = request.app.state
    out: dict[str, Any] = {}
    for name in ("bank_breaker", "card_network_breaker"):
        breaker = getattr(state, name, None)
        if breaker is not None:
            out[name] = {"open": breaker.is_open, "failures": breaker.failures}
    return out


async def run_money_workers(state: Any, *, settle: bool = False) -> dict[str, int]:
    """One pass of every Phase 8 background worker (also used by tests and demos)."""
    ctx: MoneyContext = state.money_ctx
    store = getattr(state, "object_store", None)
    results = {
        "money_commands": await sweep_money_commands(ctx),
        "refunds": await refund_worker_batch(ctx, state.bank_http),
        "payouts": await payout_worker_batch(
            ctx, state.bank_http, None if store is None else store.put
        ),
        "outbox": await relay_outbox(ctx.pool, getattr(state, "event_publisher", None)),
    }
    results["webhooks"] = await state.webhook_dispatcher.run_batch()
    if settle:
        results["settlements"] = await settle_due_merchants(ctx, datetime.now(UTC))
    return results


@internal_router.post("/internal/v1/workers/run")
async def run_workers_route(request: Request) -> dict[str, int]:
    require_internal(request)
    return await run_money_workers(request.app.state)


def configure_money_services(
    state: Any,
    *,
    pool: Any,
    cipher: Any,
    object_store: Any = None,
    event_publisher: Any = None,
    webhook_http: Any = None,
    trusted_webhook_hosts: tuple[str, ...] = (),
    resolver: Any = None,
) -> None:
    """Attach the Phase 8 money context and workers to an app's state."""
    import httpx

    from services.core.disputes import DISPUTE_HANDLERS
    from services.core.refunds import REFUND_HANDLERS
    from services.core.settlement import SETTLEMENT_HANDLERS

    state.money_ctx = MoneyContext(
        pool=pool,
        ledger_http=state.ledger_http,
        handlers={**REFUND_HANDLERS, **SETTLEMENT_HANDLERS, **DISPUTE_HANDLERS},
        faults=state,
    )
    state.object_store = object_store
    state.event_publisher = event_publisher
    state.webhook_cipher = cipher
    state.webhook_trusted_hosts = trusted_webhook_hosts
    dispatcher = WebhookDispatcher(
        pool=pool,
        http=webhook_http or httpx.AsyncClient(timeout=5.0, follow_redirects=False),
        cipher=cipher,
        trusted_hosts=trusted_webhook_hosts,
    )
    if resolver is not None:
        dispatcher.resolver = resolver
    state.webhook_dispatcher = dispatcher
