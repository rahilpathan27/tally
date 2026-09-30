"""Merchant payment-intent API and modular payment orchestrator."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, cast
from uuid import UUID, uuid4

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Path, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from libs.idempotency.store import PostgresIdempotencyStore
from libs.security.key_encryption import ApiKeyCipher
from libs.security.rate_limit import MerchantRateLimiter
from redis.asyncio import Redis

from services.api_gateway.auth import MerchantHmacAuth, MerchantPrincipal
from services.api_gateway.route import GatewayRoute, requires_scope
from services.core.faults import fault_point
from services.core.recovery import CircuitBreaker
from services.core.recovery_worker import (
    _validate_ledger_response,
    process_recovery_batch,
    sweep_stalled_payments,
)
from services.core.repository import PaymentIntentRepository
from services.core.schemas import ConfirmResponse, CreatePaymentIntent, PaymentIntentResponse
from services.core.state_machine import PaymentState

__all__ = [
    "PaymentIntentRepository",
    "create_app",
    "process_recovery_batch",
    "sweep_stalled_payments",
]

PaymentId = Annotated[UUID, Path()]
logger = logging.getLogger(__name__)


def _record_response(row: asyncpg.Record) -> PaymentIntentResponse:
    return PaymentIntentResponse(
        payment_id=row["payment_id"],
        amount_minor=row["amount_minor"],
        currency=row["currency"].strip(),
        payment_method_type=row["payment_method_type"],
        status=row["status"],
        created_at=row["created_at"].isoformat(),
    )


def _principal(request: Request) -> MerchantPrincipal:
    return cast(MerchantPrincipal, request.state.merchant_principal)


def _repository(request: Request) -> PaymentIntentRepository:
    return cast(PaymentIntentRepository, request.app.state.payment_repository)


def _correlation_id(request: Request) -> str:
    value = request.headers.get("x-request-id")
    return hashlib.sha256(value[:200].encode("utf-8")).hexdigest() if value else str(uuid4())


async def _await_status_check(request: Request, payment_id: UUID, reason: str) -> ConfirmResponse:
    """Park a payment whose external leg succeeded but whose ledger result is unknown."""
    await _repository(request).transition(
        payment_id,
        _principal(request).merchant_id,
        PaymentState.PENDING_UNKNOWN,
        "payment-orchestrator",
        reason,
        _correlation_id(request),
    )
    return ConfirmResponse(payment_id=payment_id, status="pending_unknown")


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database_url = os.environ.get("TALLY_DATABASE_URL")
        redis_url = os.environ.get("REDIS_URL")
        cipher_key = os.environ.get("TALLY_API_KEY_ENCRYPTION_KEY")
        if not database_url or not redis_url or not cipher_key:
            raise RuntimeError(
                "TALLY_DATABASE_URL, REDIS_URL and API-key encryption key are required"
            )
        try:
            encryption_key = base64.b64decode(cipher_key, altchars=b"-_", validate=True)
        except ValueError as exc:
            raise RuntimeError("TALLY_API_KEY_ENCRYPTION_KEY must be base64-encoded") from exc
        cipher = ApiKeyCipher(encryption_key)
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=15)
        redis = Redis.from_url(redis_url)
        app.state.pool = pool
        app.state.gateway_auth = MerchantHmacAuth(pool, cipher.decrypt)
        app.state.gateway_rate_limiter = MerchantRateLimiter(redis, namespace="gateway:merchant")
        app.state.gateway_idempotency = PostgresIdempotencyStore(pool)
        app.state.gateway_rate_limit_policy = lambda _: (120, 60)
        app.state.payment_repository = PaymentIntentRepository(pool)
        app.state.ledger_http = httpx.AsyncClient(
            base_url=os.environ.get("TALLY_LEDGER_URL", "http://127.0.0.1:8001"), timeout=5.0
        )
        app.state.bank_http = httpx.AsyncClient(
            base_url=os.environ.get("TALLY_BANK_SIM_URL", "http://127.0.0.1:8010"), timeout=5.0
        )
        app.state.payer_psp_http = httpx.AsyncClient(
            base_url=os.environ.get("TALLY_PAYER_PSP_URL", "http://127.0.0.1:8011"), timeout=5.0
        )
        app.state.card_network_http = httpx.AsyncClient(
            base_url=os.environ.get("TALLY_CARD_NETWORK_URL", "http://127.0.0.1:8012"), timeout=8.0
        )
        app.state.network_simulator_key = os.environ.get("TALLY_NETWORK_SIMULATOR_KEY", "")
        app.state.ledger_provisioning_key = os.environ.get("TALLY_LEDGER_INTERNAL_KEY", "")
        app.state.recovery_key = os.environ.get("TALLY_RECOVERY_KEY", "")
        app.state.bank_breaker = CircuitBreaker()
        app.state.card_network_breaker = CircuitBreaker()

        async def recovery_loop() -> None:
            while True:
                try:
                    await process_recovery_batch(
                        app.state.payment_repository,
                        app.state.bank_http,
                        app.state.ledger_http,
                        app.state.bank_breaker,
                        network_http=app.state.card_network_http,
                        network_breaker=app.state.card_network_breaker,
                    )
                    await sweep_stalled_payments(
                        app.state.payment_repository, app.state.ledger_http
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("payment recovery cycle failed")
                await asyncio.sleep(1)

        recovery_task = asyncio.create_task(recovery_loop())
        try:
            yield
        finally:
            recovery_task.cancel()
            try:
                await recovery_task
            except asyncio.CancelledError:
                pass
            await app.state.ledger_http.aclose()
            await app.state.bank_http.aclose()
            await app.state.payer_psp_http.aclose()
            await app.state.card_network_http.aclose()
            await redis.aclose()
            await pool.close()

    app = FastAPI(title="Tally Payment API", version="1.0.0", lifespan=lifespan)
    app.state.payment_repository = None
    app.state.provisioned_merchants = set()
    app.state.fault_injector = None

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": "INVALID_REQUEST", "message": "Request is invalid."}},
        )

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/v1/recovery/run", include_in_schema=False)
    async def run_recovery(request: Request) -> dict[str, int]:
        expected = request.app.state.recovery_key
        provided = request.headers.get("x-recovery-key", "")
        if not expected or not hmac.compare_digest(provided, expected):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid recovery credential")
        processed = await process_recovery_batch(
            _repository(request),
            request.app.state.bank_http,
            request.app.state.ledger_http,
            request.app.state.bank_breaker,
            network_http=request.app.state.card_network_http,
            network_breaker=request.app.state.card_network_breaker,
        )
        processed += await sweep_stalled_payments(
            _repository(request), request.app.state.ledger_http
        )
        return {"processed": processed}

    app.router.route_class = GatewayRoute

    @app.post(
        "/v1/payment_intents",
        response_model=PaymentIntentResponse,
        status_code=status.HTTP_201_CREATED,
    )
    @requires_scope("payments:write")
    async def create_payment_intent(
        body: CreatePaymentIntent, request: Request
    ) -> PaymentIntentResponse:
        principal = _principal(request)
        ledger: httpx.AsyncClient = request.app.state.ledger_http
        provisioning_key = request.app.state.ledger_provisioning_key
        if not provisioning_key:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "LEDGER_PROVISIONING_UNAVAILABLE",
                    "message": "Payment setup is unavailable.",
                },
            )
        provisioned_merchants: set[str] = request.app.state.provisioned_merchants
        try:
            if principal.merchant_id not in provisioned_merchants:
                provisioned = await ledger.post(
                    "/internal/v1/merchant-accounts",
                    json={"merchant_id": principal.merchant_id, "currency": "INR"},
                    headers={"x-ledger-provisioning-key": provisioning_key},
                )
                provisioned.raise_for_status()
                provisioned_merchants.add(principal.merchant_id)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "LEDGER_PROVISIONING_UNAVAILABLE",
                    "message": "Payment setup is unavailable.",
                },
            ) from exc
        try:
            row = await _repository(request).create(
                principal.merchant_id,
                principal.mode,
                body,
                _correlation_id(request),
            )
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "VPA_NOT_FOUND", "message": "A UPI VPA could not be resolved."},
            ) from exc
        return _record_response(row)

    @app.post("/v1/payment_intents/{payment_id}/confirm", response_model=ConfirmResponse)
    @requires_scope("payments:write")
    async def confirm_payment_intent(
        payment_id: PaymentId,
        request: Request,
    ) -> ConfirmResponse:
        principal = _principal(request)
        payment = await _repository(request).get(payment_id, principal.merchant_id)
        if payment is None:
            raise HTTPException(
                404, detail={"code": "PAYMENT_NOT_FOUND", "message": "Payment was not found."}
            )
        amount = int(payment["amount_minor"])
        ledger_key = f"{payment_id}:{payment['payment_method_type']}:authorization:hold"
        ledger_request: dict[str, object] = {
            "idempotency_key": ledger_key,
            "postings": [
                {"account_id": "bank:simulated:INR", "direction": "debit", "amount_minor": amount},
                {
                    "account_id": f"merchant:{principal.merchant_id}:payable:INR",
                    "direction": "credit",
                    "amount_minor": amount,
                },
            ],
        }
        payment_method = payment["payment_method_type"]
        if payment_method == "upi":
            ledger_key = f"{payment_id}:upi:transfer:post"
            ledger_request["idempotency_key"] = ledger_key
        command = (
            "place_hold" if payment_method == "card" else "post_entry",
            ledger_key,
            ledger_request,
        )
        accepted, _, _ = await _repository(request).transition(
            payment_id,
            principal.merchant_id,
            PaymentState.AUTHORIZING,
            principal.key_id,
            "merchant confirmed payment intent",
            _correlation_id(request),
            command=command,
        )
        if not accepted:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "ILLEGAL_PAYMENT_TRANSITION",
                    "message": "Payment cannot be confirmed from its current state.",
                },
            )
        fault_point(request.app.state, "confirm.after_authorizing_committed")

        if payment_method == "card":
            network: httpx.AsyncClient = request.app.state.card_network_http
            network_breaker: CircuitBreaker = request.app.state.card_network_breaker
            if not network_breaker.allow_request():
                await _repository(request).transition(
                    payment_id,
                    principal.merchant_id,
                    PaymentState.PENDING_UNKNOWN,
                    principal.key_id,
                    "card-network circuit breaker is open before dispatch",
                    _correlation_id(request),
                )
                return ConfirmResponse(payment_id=payment_id, status="pending_unknown")
            try:
                simulator_response = await network.post(
                    "/v1/authorizations",
                    json={
                        "payment_id": str(payment_id),
                        "payment_method_token": payment["payment_method_token"],
                        "amount_minor": amount,
                        "currency": payment["currency"].strip(),
                    },
                    headers={
                        "x-simulator-key": request.app.state.network_simulator_key,
                        "Idempotency-Key": f"{payment_id}:card:network:authorize",
                    },
                )
                simulator_response.raise_for_status()
                network_breaker.record_success()
            except httpx.HTTPError:
                network_breaker.record_failure()
                await _repository(request).transition(
                    payment_id,
                    principal.merchant_id,
                    PaymentState.PENDING_UNKNOWN,
                    principal.key_id,
                    "card network response was unavailable; outcome requires status check",
                    _correlation_id(request),
                )
                return ConfirmResponse(payment_id=payment_id, status="pending_unknown")
            simulator_result = simulator_response.json()
            if simulator_result.get("status") != "approved":
                await _repository(request).complete_command(
                    ledger_key, {"status": "declined"}, skipped=True
                )
                await _repository(request).transition(
                    payment_id,
                    principal.merchant_id,
                    PaymentState.FAILED,
                    "card-network-simulator",
                    "simulated card authorization was declined",
                    _correlation_id(request),
                )
                return ConfirmResponse(payment_id=payment_id, status="failed")
            fault_point(request.app.state, "card.after_network_approved")
        else:
            psp: httpx.AsyncClient = request.app.state.payer_psp_http
            try:
                psp_response = await psp.post(
                    "/v1/approvals",
                    json={
                        "payment_id": str(payment_id),
                        "payer_vpa": payment["payer_vpa"],
                        "amount_minor": amount,
                    },
                    headers={"Idempotency-Key": f"{payment_id}:upi:psp:authorize"},
                )
                psp_response.raise_for_status()
            except httpx.HTTPError:
                # No bank leg has been dispatched, so no funds can have moved.
                await _repository(request).transition(
                    payment_id,
                    principal.merchant_id,
                    PaymentState.FAILED,
                    "payment-orchestrator",
                    "payer PSP was unavailable before bank dispatch; no funds moved",
                    _correlation_id(request),
                    completed_command=(ledger_key, {"status": "payer_psp_unavailable"}, True),
                )
                return ConfirmResponse(payment_id=payment_id, status="failed")
            if psp_response.json().get("status") != "approved":
                await _repository(request).complete_command(
                    ledger_key, {"status": "payer_declined"}, skipped=True
                )
                await _repository(request).transition(
                    payment_id,
                    principal.merchant_id,
                    PaymentState.FAILED,
                    "payer-psp-simulator",
                    "payer declined the UPI collect request",
                    _correlation_id(request),
                )
                return ConfirmResponse(payment_id=payment_id, status="failed")
            fault_point(request.app.state, "upi.after_psp_approved")
            bank: httpx.AsyncClient = request.app.state.bank_http
            breaker: CircuitBreaker = request.app.state.bank_breaker
            if not breaker.allow_request():
                await _repository(request).transition(
                    payment_id,
                    principal.merchant_id,
                    PaymentState.PENDING_UNKNOWN,
                    "payment-orchestrator",
                    "bank circuit breaker is open before transfer dispatch",
                    _correlation_id(request),
                )
                return ConfirmResponse(payment_id=payment_id, status="pending_unknown")
            try:
                transfer_response = await bank.post(
                    "/v1/transfers",
                    json={
                        "payment_id": str(payment_id),
                        "remitter_bank": payment["remitter_bank"],
                        "beneficiary_bank": payment["beneficiary_bank"],
                        "amount_minor": amount,
                        "currency": payment["currency"].strip(),
                    },
                    headers={"Idempotency-Key": f"{payment_id}:upi:bank:transfer"},
                )
                transfer_response.raise_for_status()
                breaker.record_success()
            except httpx.HTTPError:
                breaker.record_failure()
                await _repository(request).transition(
                    payment_id,
                    principal.merchant_id,
                    PaymentState.PENDING_UNKNOWN,
                    "payment-orchestrator",
                    "bank response was unavailable; outcome requires status check",
                    _correlation_id(request),
                )
                return ConfirmResponse(payment_id=payment_id, status="pending_unknown")
            transfer_status = transfer_response.json().get("status")
            if transfer_status == "debit_succeeded_credit_failed":
                try:
                    reversal_response = await bank.post(f"/v1/transfers/{payment_id}/reverse")
                    reversal_response.raise_for_status()
                except httpx.HTTPError:
                    await _repository(request).transition(
                        payment_id,
                        principal.merchant_id,
                        PaymentState.PENDING_UNKNOWN,
                        "payment-orchestrator",
                        "debit succeeded but credit failed; reversal result is unknown",
                        _correlation_id(request),
                    )
                    return ConfirmResponse(payment_id=payment_id, status="pending_unknown")
                await _repository(request).transition(
                    payment_id,
                    principal.merchant_id,
                    PaymentState.FAILED,
                    "bank-simulator",
                    "credit leg failed and debit leg was reversed",
                    _correlation_id(request),
                    completed_command=(ledger_key, {"status": "reversed"}, True),
                )
                return ConfirmResponse(payment_id=payment_id, status="failed")
            if transfer_status != "approved":
                await _repository(request).transition(
                    payment_id,
                    principal.merchant_id,
                    PaymentState.FAILED,
                    "bank-simulator",
                    "simulated bank declined the transfer",
                    _correlation_id(request),
                    completed_command=(ledger_key, {"status": "bank_declined"}, True),
                )
                return ConfirmResponse(payment_id=payment_id, status="failed")
        ledger: httpx.AsyncClient = request.app.state.ledger_http
        if payment_method == "card":
            try:
                ledger_response = await ledger.post("/v1/holds", json=ledger_request)
                ledger_result = _validate_ledger_response(ledger_response)
            except (httpx.HTTPError, HTTPException):
                return await _await_status_check(
                    request, payment_id, "card approved but ledger hold result is unknown"
                )
            fault_point(request.app.state, "card.after_hold_placed")
            hold_id = int(cast(str, ledger_result["hold_id"]))
            await _repository(request).complete_command(ledger_key, ledger_result)
            accepted, _, _ = await _repository(request).transition(
                payment_id,
                principal.merchant_id,
                PaymentState.AUTHORIZED,
                "card-network-simulator",
                "card authorization approved and ledger hold placed",
                _correlation_id(request),
                extra_update={"ledger_hold_id": hold_id},
            )
            if not accepted:
                raise HTTPException(409, "payment authorization state changed concurrently")
            return ConfirmResponse(
                payment_id=payment_id, status="authorized", next_action="capture"
            )

        fault_point(request.app.state, "upi.after_bank_approved")
        try:
            ledger_response = await ledger.post("/v1/entries", json=ledger_request)
            ledger_result = _validate_ledger_response(ledger_response)
        except (httpx.HTTPError, HTTPException):
            return await _await_status_check(
                request, payment_id, "bank approved but ledger posting result is unknown"
            )
        fault_point(request.app.state, "upi.after_ledger_posted")
        await _repository(request).complete_command(ledger_key, ledger_result)
        await _repository(request).transition(
            payment_id,
            principal.merchant_id,
            PaymentState.SUCCEEDED,
            "payment-orchestrator",
            "UPI payer approved and both simulated bank legs approved",
            _correlation_id(request),
        )
        return ConfirmResponse(payment_id=payment_id, status="succeeded")

    @app.post("/v1/payment_intents/{payment_id}/capture", response_model=ConfirmResponse)
    @requires_scope("payments:write")
    async def capture_payment_intent(payment_id: PaymentId, request: Request) -> ConfirmResponse:
        principal = _principal(request)
        payment = await _repository(request).get(payment_id, principal.merchant_id)
        if payment is None:
            raise HTTPException(
                404, detail={"code": "PAYMENT_NOT_FOUND", "message": "Payment was not found."}
            )
        hold_id = payment["ledger_hold_id"]
        key = f"{payment_id}:card:capture:post"
        accepted, _, _ = await _repository(request).transition(
            payment_id,
            principal.merchant_id,
            PaymentState.CAPTURING,
            principal.key_id,
            "merchant captured authorized payment",
            _correlation_id(request),
            command=("post_hold", key, {"hold_id": hold_id}),
        )
        if not accepted:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "ILLEGAL_PAYMENT_TRANSITION",
                    "message": "Payment cannot be captured from its current state.",
                },
            )
        fault_point(request.app.state, "capture.after_capturing_committed")
        ledger: httpx.AsyncClient = request.app.state.ledger_http
        try:
            result = _validate_ledger_response(await ledger.post(f"/v1/holds/{hold_id}/post"))
        except (httpx.HTTPError, HTTPException):
            # The capture decision is durable; the recovery sweeper replays the hold post.
            return ConfirmResponse(payment_id=payment_id, status="capturing")
        fault_point(request.app.state, "capture.after_hold_posted")
        await _repository(request).complete_command(key, result)
        await _repository(request).transition(
            payment_id,
            principal.merchant_id,
            PaymentState.SUCCEEDED,
            principal.key_id,
            "ledger captured authorization hold",
            _correlation_id(request),
        )
        return ConfirmResponse(payment_id=payment_id, status="succeeded")

    @app.get("/v1/payment_intents/{payment_id}", response_model=PaymentIntentResponse)
    @requires_scope("payments:read")
    async def retrieve_payment_intent(
        payment_id: PaymentId, request: Request
    ) -> PaymentIntentResponse:
        principal = _principal(request)
        row = await _repository(request).get(payment_id, principal.merchant_id)
        if row is None:
            raise HTTPException(
                404, detail={"code": "PAYMENT_NOT_FOUND", "message": "Payment was not found."}
            )
        return _record_response(row)

    @app.post("/v1/payment_intents/{payment_id}/cancel", response_model=ConfirmResponse)
    @requires_scope("payments:write")
    async def cancel_payment_intent(payment_id: PaymentId, request: Request) -> ConfirmResponse:
        principal = _principal(request)
        payment = await _repository(request).get(payment_id, principal.merchant_id)
        if payment is None:
            raise HTTPException(
                404,
                detail={"code": "PAYMENT_NOT_FOUND", "message": "Payment was not found."},
            )
        hold_id = payment["ledger_hold_id"]
        command = None
        if hold_id is not None:
            command = (
                "void_hold",
                f"{payment_id}:card:authorization:void",
                {"hold_id": hold_id},
            )
        accepted, _, _ = await _repository(request).transition(
            payment_id,
            principal.merchant_id,
            PaymentState.CANCELLED,
            principal.key_id,
            "merchant cancelled payment intent",
            _correlation_id(request),
            command=command,
        )
        if not accepted:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "ILLEGAL_PAYMENT_TRANSITION",
                    "message": "Payment cannot be cancelled from its current state.",
                },
            )
        fault_point(request.app.state, "cancel.after_cancelled_committed")
        if hold_id is not None:
            ledger: httpx.AsyncClient = request.app.state.ledger_http
            try:
                _validate_ledger_response(await ledger.post(f"/v1/holds/{hold_id}/void"))
            except (httpx.HTTPError, HTTPException):
                # Cancellation is durable; the recovery sweeper replays the hold void.
                return ConfirmResponse(payment_id=payment_id, status="cancelled")
            await _repository(request).complete_command(
                f"{payment_id}:card:authorization:void", {"hold_id": hold_id}
            )
        return ConfirmResponse(payment_id=payment_id, status="cancelled")

    return app


app = create_app()
