"""Merchant payment-intent API and modular payment orchestrator."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID, uuid4

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Path, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from libs.idempotency.store import PostgresIdempotencyStore
from libs.money import MAX_SAFE_INTEGER
from libs.security.key_encryption import ApiKeyCipher
from libs.security.rate_limit import MerchantRateLimiter
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from redis.asyncio import Redis

from services.api_gateway.auth import MerchantHmacAuth, MerchantPrincipal
from services.api_gateway.route import GatewayRoute, requires_scope
from services.core.recovery import CircuitBreaker, recovery_delay_seconds
from services.core.state_machine import PaymentState, transition_allowed

PaymentId = Annotated[UUID, Path()]
logger = logging.getLogger(__name__)
Vpa = Annotated[
    str,
    StringConstraints(
        min_length=4,
        max_length=130,
        pattern=r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$",
    ),
]


class CreatePaymentIntent(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    amount_minor: Annotated[int, Field(strict=True, gt=0, le=MAX_SAFE_INTEGER)]
    currency: Literal["INR"]
    payment_method_type: Literal["card", "upi"]
    payment_method_token: Annotated[str, Field(min_length=8, max_length=200)] | None = None
    payer_vpa: Vpa | None = None
    payee_vpa: Vpa | None = None

    @model_validator(mode="after")
    def validate_method_fields(self) -> CreatePaymentIntent:
        if self.payment_method_type == "card":
            if not self.payment_method_token or self.payer_vpa or self.payee_vpa:
                raise ValueError("card payments require a token and no VPA fields")
        elif self.payment_method_token or not self.payer_vpa or not self.payee_vpa:
            raise ValueError("UPI payments require payer and payee VPAs and no card token")
        return self


class PaymentIntentResponse(BaseModel):
    payment_id: UUID
    amount_minor: int
    currency: str
    payment_method_type: str
    status: str
    created_at: str | None = None


class ConfirmResponse(BaseModel):
    payment_id: UUID
    status: str
    next_action: str | None = None


class PaymentIntentRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def create(
        self,
        merchant_id: str,
        mode: str,
        body: CreatePaymentIntent,
        correlation_id: str,
    ) -> asyncpg.Record:
        payment_id = uuid4()
        event_payload = {
            "payment_id": str(payment_id),
            "merchant_id": merchant_id,
            "amount_minor": body.amount_minor,
            "currency": body.currency,
            "status": PaymentState.CREATED.value,
            "payment_method_type": body.payment_method_type,
        }
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT set_config('app.merchant_id', $1, true)", merchant_id)
            if body.payment_method_type == "upi":
                if body.payer_vpa == body.payee_vpa:
                    raise ValueError("payer and payee VPAs must be different")
                found = await connection.fetchval(
                    """SELECT count(*) = 2 FROM core_vpas
                       WHERE vpa = ANY($1::text[]) AND active""",
                    [body.payer_vpa, body.payee_vpa],
                )
                if not found:
                    raise ValueError("one or more UPI VPAs could not be resolved")
            row = await connection.fetchrow(
                """INSERT INTO payment_intents(
                       payment_id, merchant_id, amount_minor, currency, payment_method_type,
                       payment_method_token, payer_vpa, payee_vpa, status, mode
                   ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'created', $9)
                   RETURNING payment_id, amount_minor, currency, payment_method_type,
                             status, created_at""",
                payment_id,
                merchant_id,
                body.amount_minor,
                body.currency,
                body.payment_method_type,
                body.payment_method_token,
                body.payer_vpa,
                body.payee_vpa,
                mode,
            )
            await connection.execute(
                """INSERT INTO payment_transitions(
                       payment_id, merchant_id, from_state, to_state, accepted,
                       actor, reason, correlation_id
                   ) VALUES ($1, $2, NULL, 'created', true, $3, $4, $5)""",
                payment_id,
                merchant_id,
                merchant_id,
                "payment intent created",
                correlation_id,
            )
            await connection.execute(
                """INSERT INTO core_outbox(aggregate_id, merchant_id, event_type, payload)
                   VALUES ($1, $2, 'payment_intent.created', $3::jsonb)""",
                payment_id,
                merchant_id,
                json.dumps(event_payload, separators=(",", ":")),
            )
            assert row is not None
            return row

    async def transition(
        self,
        payment_id: UUID,
        merchant_id: str,
        target: PaymentState,
        actor: str,
        reason: str,
        correlation_id: str,
        *,
        command: tuple[str, str, dict[str, object]] | None = None,
        extra_update: dict[str, object] | None = None,
        completed_command: tuple[str, dict[str, object], bool] | None = None,
    ) -> tuple[bool, PaymentState, asyncpg.Record | None]:
        accepted = False
        current = target
        row: asyncpg.Record | None = None
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT set_config('app.merchant_id', $1, true)", merchant_id)
            row = await connection.fetchrow(
                """SELECT * FROM payment_intents
                   WHERE payment_id = $1 AND merchant_id = $2 FOR UPDATE""",
                payment_id,
                merchant_id,
            )
            if row is None:
                raise KeyError("payment intent was not found")
            current = PaymentState(row["status"])
            accepted = transition_allowed(current, target)
            await connection.execute(
                """INSERT INTO payment_transitions(
                       payment_id, merchant_id, from_state, to_state, accepted,
                       actor, reason, correlation_id
                   ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                payment_id,
                merchant_id,
                current.value,
                target.value,
                accepted,
                actor,
                reason if accepted else f"rejected transition: {reason}",
                correlation_id,
            )
            if accepted:
                await connection.execute(
                    """UPDATE payment_intents SET status = $3, updated_at = clock_timestamp(),
                           recovery_lease_until = NULL
                       WHERE payment_id = $1 AND merchant_id = $2""",
                    payment_id,
                    merchant_id,
                    target.value,
                )
                if target == PaymentState.PENDING_UNKNOWN:
                    await connection.execute(
                        """WITH selected_policy AS (
                               SELECT p.payment_id,
                                      COALESCE(policy.status_check_deadline_seconds, 30)
                                          AS deadline,
                                      COALESCE(policy.deemed_outcome, 'auto_reverse') AS outcome,
                                      COALESCE(policy.late_success_window_seconds, 600) AS window
                               FROM payment_intents p
                               LEFT JOIN core_vpas payer ON payer.vpa = p.payer_vpa
                               LEFT JOIN LATERAL (
                                   SELECT * FROM core_bank_recovery_policies candidate
                                   WHERE candidate.bank_id = payer.bank_id
                                     AND candidate.min_amount_minor <= p.amount_minor
                                     AND (candidate.max_amount_minor IS NULL
                                       OR candidate.max_amount_minor >= p.amount_minor)
                                   ORDER BY candidate.min_amount_minor DESC LIMIT 1
                               ) policy ON true
                               WHERE p.payment_id = $1 AND p.merchant_id = $2
                           )
                           UPDATE payment_intents p
                           SET recovery_attempts = 0,
                               next_recovery_at = clock_timestamp(),
                               recovery_deadline = clock_timestamp() + make_interval(
                                   secs => selected_policy.deadline::double precision),
                               recovery_policy = selected_policy.outcome,
                               late_success_window_seconds = selected_policy.window
                           FROM selected_policy
                           WHERE p.payment_id = selected_policy.payment_id""",
                        payment_id,
                        merchant_id,
                    )
                if target == PaymentState.REVERSED:
                    await connection.execute(
                        """UPDATE payment_intents
                           SET late_success_until = clock_timestamp() + make_interval(
                                   secs => late_success_window_seconds::double precision),
                               next_recovery_at = clock_timestamp() + interval '5 seconds'
                           WHERE payment_id = $1 AND merchant_id = $2""",
                        payment_id,
                        merchant_id,
                    )
                payload: dict[str, object] = {
                    "payment_id": str(payment_id),
                    "merchant_id": merchant_id,
                    "from_state": current.value,
                    "status": target.value,
                    "correlation_id": correlation_id,
                }
                await connection.execute(
                    """INSERT INTO core_outbox(aggregate_id, merchant_id, event_type, payload)
                       VALUES ($1, $2, $3, $4::jsonb)""",
                    payment_id,
                    merchant_id,
                    f"payment_intent.{target.value}",
                    json.dumps(payload, separators=(",", ":")),
                )
                if command is not None:
                    operation, idempotency_key, command_request = command
                    await connection.execute(
                        """INSERT INTO core_ledger_commands(
                               payment_id, merchant_id, operation, idempotency_key, request
                           ) VALUES ($1, $2, $3, $4, $5::jsonb)""",
                        payment_id,
                        merchant_id,
                        operation,
                        idempotency_key,
                        json.dumps(command_request, separators=(",", ":")),
                    )
                if completed_command is not None:
                    idempotency_key, result, skipped = completed_command
                    await connection.execute(
                        """UPDATE core_ledger_commands SET state = $2, result = $3::jsonb,
                               completed_at = clock_timestamp()
                           WHERE idempotency_key = $1""",
                        idempotency_key,
                        "skipped" if skipped else "completed",
                        json.dumps(result, separators=(",", ":")),
                    )
                if extra_update:
                    await connection.execute(
                        """UPDATE payment_intents SET ledger_hold_id = $3
                           WHERE payment_id = $1 AND merchant_id = $2""",
                        payment_id,
                        merchant_id,
                        extra_update["ledger_hold_id"],
                    )
        return accepted, current, row

    async def complete_command(
        self, idempotency_key: str, result: dict[str, object], *, skipped: bool = False
    ) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(
                """UPDATE core_ledger_commands SET state = $2, result = $3::jsonb,
                       completed_at = clock_timestamp() WHERE idempotency_key = $1""",
                idempotency_key,
                "skipped" if skipped else "completed",
                json.dumps(result, separators=(",", ":")),
            )

    async def get(self, payment_id: UUID, merchant_id: str) -> asyncpg.Record | None:
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT set_config('app.merchant_id', $1, true)", merchant_id)
            return await connection.fetchrow(
                """SELECT p.payment_id, p.amount_minor, p.currency, p.payment_method_type, p.status,
                          p.payment_method_token, p.payer_vpa, p.payee_vpa, p.ledger_hold_id,
                          p.created_at, payer.bank_id AS remitter_bank,
                          payee.bank_id AS beneficiary_bank
                   FROM payment_intents p
                   LEFT JOIN core_vpas payer ON payer.vpa = p.payer_vpa
                   LEFT JOIN core_vpas payee ON payee.vpa = p.payee_vpa
                   WHERE p.payment_id = $1 AND p.merchant_id = $2""",
                payment_id,
                merchant_id,
            )

    async def pending_recovery(self, limit: int = 100) -> list[asyncpg.Record]:
        async with self.pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """SELECT p.payment_id, p.merchant_id, p.status, p.recovery_attempts,
                          p.recovery_deadline, p.late_success_until,
                          p.recovery_deadline <= clock_timestamp() AS deadline_elapsed,
                          p.amount_minor, p.currency, p.payment_method_type, p.recovery_policy,
                          c.idempotency_key, c.request
                   FROM payment_intents p
                   JOIN core_ledger_commands c ON c.payment_id = p.payment_id
                   WHERE ((p.payment_method_type = 'upi' AND c.operation = 'post_entry'
                           AND ((p.status IN ('pending_unknown', 'reversal_pending')
                                 AND c.state = 'pending')
                             OR (p.status = 'reversed' AND c.state = 'pending'
                                 AND p.late_success_until IS NOT NULL)))
                       OR (p.payment_method_type = 'card' AND c.operation = 'place_hold'
                           AND ((p.status IN ('pending_unknown', 'reversal_pending')
                                 AND c.state = 'pending')
                             OR (p.status = 'reversed' AND c.state = 'pending'
                                 AND p.late_success_until IS NOT NULL))))
                     AND (p.next_recovery_at IS NULL OR p.next_recovery_at <= clock_timestamp())
                     AND (p.recovery_lease_until IS NULL
                       OR p.recovery_lease_until <= clock_timestamp())
                   ORDER BY p.next_recovery_at NULLS FIRST, p.created_at
                   FOR UPDATE OF p SKIP LOCKED LIMIT $1""",
                limit,
            )
            if rows:
                await connection.execute(
                    """UPDATE payment_intents SET recovery_lease_until =
                           clock_timestamp() + interval '30 seconds'
                       WHERE payment_id = ANY($1::uuid[])""",
                    [row["payment_id"] for row in rows],
                )
            return cast(list[asyncpg.Record], rows)

    async def reschedule_recovery(self, payment_id: UUID, attempts: int) -> None:
        delay_seconds = recovery_delay_seconds(attempts)
        await self.pool.execute(
            """UPDATE payment_intents SET recovery_attempts = $2,
                   next_recovery_at = clock_timestamp() + ($3 * interval '1 second'),
                   updated_at = clock_timestamp(), recovery_lease_until = NULL
               WHERE payment_id = $1 AND status IN ('pending_unknown', 'reversed')""",
            payment_id,
            attempts,
            delay_seconds,
        )

    async def close_late_success_watch(self, payment_id: UUID) -> None:
        await self.pool.execute(
            """UPDATE payment_intents SET late_success_until = NULL,
                   recovery_lease_until = NULL WHERE payment_id = $1""",
            payment_id,
        )

    async def record_late_success(
        self,
        payment_id: UUID,
        merchant_id: str,
        bank_status: str,
        correction_entry_id: str | None,
        correction_reference: str,
        original_command_key: str,
        correction_result: dict[str, object],
        ledger_hold_id: int | None = None,
    ) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            incident_id = await connection.fetchval(
                """INSERT INTO core_recovery_incidents(
                       payment_id, merchant_id, incident_type, bank_status,
                       correction_entry_id, correction_reference
                   ) VALUES ($1, $2, 'late_success_after_reversal', $3, $4, $5)
                   ON CONFLICT (payment_id, incident_type) DO NOTHING
                   RETURNING incident_id""",
                payment_id,
                merchant_id,
                bank_status,
                int(correction_entry_id) if correction_entry_id is not None else None,
                correction_reference,
            )
            if incident_id is not None:
                await connection.execute(
                    """INSERT INTO core_outbox(aggregate_id, merchant_id, event_type, payload)
                       VALUES ($1, $2, 'payment_intent.late_success_after_reversal', $3::jsonb)""",
                    payment_id,
                    merchant_id,
                    json.dumps(
                        {
                            "payment_id": str(payment_id),
                            "merchant_id": merchant_id,
                            "bank_status": bank_status,
                            "correction_entry_id": correction_entry_id,
                            "correction_reference": correction_reference,
                        },
                        separators=(",", ":"),
                    ),
                )
            await connection.execute(
                """UPDATE core_ledger_commands SET state = 'completed', result = $2::jsonb,
                       completed_at = clock_timestamp() WHERE idempotency_key = $1""",
                original_command_key,
                json.dumps(correction_result, separators=(",", ":")),
            )
            await connection.execute(
                """UPDATE payment_intents SET late_success_until = NULL,
                       recovery_lease_until = NULL,
                       ledger_hold_id = COALESCE($2, ledger_hold_id)
                   WHERE payment_id = $1""",
                payment_id,
                ledger_hold_id,
            )


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


def _validate_ledger_response(response: httpx.Response) -> dict[str, object]:
    try:
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "LEDGER_UNAVAILABLE", "message": "Payment processing is unavailable."},
        ) from exc
    value = response.json()
    if not isinstance(value, dict):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "ledger returned an invalid response")
    return cast(dict[str, object], value)


async def process_recovery_batch(
    repository: PaymentIntentRepository,
    bank_http: httpx.AsyncClient,
    ledger_http: httpx.AsyncClient,
    breaker: CircuitBreaker,
    *,
    network_http: httpx.AsyncClient | None = None,
    network_breaker: CircuitBreaker | None = None,
    limit: int = 100,
) -> int:
    """Resolve due UPI unknowns from authoritative simulator status responses."""
    processed = 0
    for row in await repository.pending_recovery(limit):
        if row["status"] == PaymentState.REVERSED.value and row[
            "late_success_until"
        ] <= datetime.now(UTC):
            await repository.close_late_success_watch(row["payment_id"])
            continue
        payment_id = row["payment_id"]
        call_breaker = network_breaker or breaker
        if row["payment_method_type"] != "card":
            call_breaker = breaker
        if not call_breaker.allow_request():
            break
        status_client = network_http if row["payment_method_type"] == "card" else bank_http
        if status_client is None:
            await repository.reschedule_recovery(payment_id, row["recovery_attempts"] + 1)
            continue
        status_path = (
            f"/v1/authorizations/{payment_id}/status"
            if row["payment_method_type"] == "card"
            else f"/v1/transfers/{payment_id}/status"
        )
        try:
            response = await status_client.get(status_path)
            response.raise_for_status()
            bank_status = response.json().get("status")
            call_breaker.record_success()
        except (httpx.HTTPError, ValueError):
            call_breaker.record_failure()
            await repository.reschedule_recovery(payment_id, row["recovery_attempts"] + 1)
            continue

        correlation_id = str(payment_id)
        if bank_status == "declined" and row["status"] == PaymentState.PENDING_UNKNOWN.value:
            await repository.transition(
                payment_id,
                row["merchant_id"],
                PaymentState.FAILED,
                "recovery-worker",
                "status check confirmed the external authorization was declined",
                correlation_id,
                completed_command=(row["idempotency_key"], {"status": "declined"}, True),
            )
            processed += 1
            continue

        if bank_status == "debit_succeeded_credit_failed":
            try:
                reversal = await bank_http.post(f"/v1/transfers/{payment_id}/reverse")
                reversal.raise_for_status()
                bank_status = reversal.json().get("status")
            except (httpx.HTTPError, ValueError):
                await repository.reschedule_recovery(payment_id, row["recovery_attempts"] + 1)
                continue

        if bank_status == "reversed" and row["status"] != PaymentState.REVERSED.value:
            final_state = (
                PaymentState.REVERSED
                if row["status"] == PaymentState.REVERSAL_PENDING.value
                else PaymentState.FAILED
            )
            accepted, _, _ = await repository.transition(
                payment_id,
                row["merchant_id"],
                final_state,
                "recovery-worker",
                "bank debit leg was reversed after the credit leg failed",
                correlation_id,
                completed_command=(row["idempotency_key"], {"status": "reversed"}, True),
            )
            if accepted:
                processed += 1
            continue

        if bank_status == "approved":
            command_request = row["request"]
            if isinstance(command_request, str):
                command_request = json.loads(command_request)
            if row["payment_method_type"] == "card":
                try:
                    ledger_result = _validate_ledger_response(
                        await ledger_http.post("/v1/holds", json=command_request)
                    )
                except (httpx.HTTPError, HTTPException):
                    await repository.reschedule_recovery(payment_id, row["recovery_attempts"] + 1)
                    continue
                if row["status"] == PaymentState.REVERSED.value:
                    hold_id = int(cast(str, ledger_result["hold_id"]))
                    try:
                        void_result = _validate_ledger_response(
                            await ledger_http.post(f"/v1/holds/{hold_id}/void")
                        )
                    except (httpx.HTTPError, HTTPException):
                        await repository.reschedule_recovery(
                            payment_id, row["recovery_attempts"] + 1
                        )
                        continue
                    await repository.record_late_success(
                        payment_id,
                        row["merchant_id"],
                        str(bank_status),
                        None,
                        f"ledger_hold:{hold_id}:void",
                        row["idempotency_key"],
                        {"status": "late_card_authorization_voided", **void_result},
                        ledger_hold_id=hold_id,
                    )
                    processed += 1
                    continue
                accepted, _, _ = await repository.transition(
                    payment_id,
                    row["merchant_id"],
                    PaymentState.AUTHORIZED,
                    "recovery-worker",
                    "card network status confirmed authorization; ledger hold placed",
                    correlation_id,
                    extra_update={"ledger_hold_id": int(cast(str, ledger_result["hold_id"]))},
                    completed_command=(row["idempotency_key"], ledger_result, False),
                )
                if accepted:
                    processed += 1
                continue
            late_success = row["status"] == PaymentState.REVERSED.value
            if late_success:
                corrected_request = dict(cast(dict[str, object], command_request))
                corrected_request["idempotency_key"] = (
                    f"{row['idempotency_key']}:late-success-correction"
                )
                postings = cast(list[dict[str, object]], corrected_request["postings"])
                postings[1] = {**postings[1], "account_id": "platform:suspense:INR"}
            else:
                corrected_request = cast(dict[str, object], command_request)
            try:
                ledger_result = _validate_ledger_response(
                    await ledger_http.post("/v1/entries", json=corrected_request)
                )
            except (httpx.HTTPError, HTTPException):
                await repository.reschedule_recovery(payment_id, row["recovery_attempts"] + 1)
                continue
            if late_success:
                await repository.record_late_success(
                    payment_id,
                    row["merchant_id"],
                    str(bank_status),
                    str(ledger_result["entry_id"]),
                    f"ledger_entry:{ledger_result['entry_id']}",
                    row["idempotency_key"],
                    {"status": "late_success_corrected_to_suspense", **ledger_result},
                )
                processed += 1
                continue
            accepted, _, _ = await repository.transition(
                payment_id,
                row["merchant_id"],
                PaymentState.SUCCEEDED,
                "recovery-worker",
                "bank status check confirmed transfer success",
                correlation_id,
                completed_command=(row["idempotency_key"], ledger_result, False),
            )
            if accepted:
                processed += 1
            continue

        if row["status"] == PaymentState.REVERSED.value:
            if row["late_success_until"] <= datetime.now(UTC):
                await repository.close_late_success_watch(payment_id)
            else:
                await repository.reschedule_recovery(payment_id, row["recovery_attempts"] + 1)
            continue

        if bank_status == "unknown" and row["deadline_elapsed"]:
            if row["recovery_policy"] == "deemed_success":
                command_request = row["request"]
                if isinstance(command_request, str):
                    command_request = json.loads(command_request)
                ledger_endpoint = (
                    "/v1/holds" if row["payment_method_type"] == "card" else "/v1/entries"
                )
                try:
                    ledger_result = _validate_ledger_response(
                        await ledger_http.post(ledger_endpoint, json=command_request)
                    )
                except (httpx.HTTPError, HTTPException):
                    await repository.reschedule_recovery(payment_id, row["recovery_attempts"] + 1)
                    continue
                is_card = row["payment_method_type"] == "card"
                accepted, _, _ = await repository.transition(
                    payment_id,
                    row["merchant_id"],
                    PaymentState.AUTHORIZED if is_card else PaymentState.SUCCEEDED,
                    "recovery-worker",
                    "bank status remained unknown at deadline; "
                    "configured deemed-success policy applied",
                    correlation_id,
                    extra_update=(
                        {"ledger_hold_id": int(cast(str, ledger_result["hold_id"]))}
                        if is_card
                        else None
                    ),
                    completed_command=(row["idempotency_key"], ledger_result, False),
                )
                if accepted:
                    processed += 1
                continue
            if row["recovery_policy"] != "auto_reverse":
                await repository.reschedule_recovery(payment_id, row["recovery_attempts"] + 1)
                continue

        if bank_status == "declined" or (
            row["deadline_elapsed"]
            and (
                bank_status == "not_found"
                or (bank_status == "unknown" and row["recovery_policy"] == "auto_reverse")
            )
        ):
            reversal_state = (
                PaymentState.REVERSED
                if row["status"] == PaymentState.REVERSAL_PENDING.value
                else PaymentState.REVERSAL_PENDING
            )
            accepted, _, _ = await repository.transition(
                payment_id,
                row["merchant_id"],
                reversal_state,
                "recovery-worker",
                "deemed no-transfer after status check deadline",
                correlation_id,
            )
            if accepted:
                if reversal_state == PaymentState.REVERSAL_PENDING:
                    await repository.transition(
                        payment_id,
                        row["merchant_id"],
                        PaymentState.REVERSED,
                        "recovery-worker",
                        "no bank debit was recorded; no ledger transfer was posted",
                        correlation_id,
                    )
                processed += 1
            continue

        await repository.reschedule_recovery(payment_id, row["recovery_attempts"] + 1)
    return processed


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
        try:
            provisioned = await ledger.post(
                "/internal/v1/merchant-accounts",
                json={"merchant_id": principal.merchant_id, "currency": "INR"},
                headers={"x-ledger-provisioning-key": provisioning_key},
            )
            provisioned.raise_for_status()
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
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "code": "PAYER_PSP_UNAVAILABLE",
                        "message": "Payer approval is unavailable.",
                    },
                ) from exc
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
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "code": "LEDGER_UNAVAILABLE",
                        "message": "Payment processing is unavailable.",
                    },
                ) from exc
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

        try:
            ledger_response = await ledger.post("/v1/entries", json=ledger_request)
            ledger_result = _validate_ledger_response(ledger_response)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "LEDGER_UNAVAILABLE",
                    "message": "Payment processing is unavailable.",
                },
            ) from exc
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
        ledger: httpx.AsyncClient = request.app.state.ledger_http
        response = await ledger.post(f"/v1/holds/{hold_id}/post")
        result = _validate_ledger_response(response)
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
        if hold_id is not None:
            ledger: httpx.AsyncClient = request.app.state.ledger_http
            response = await ledger.post(f"/v1/holds/{hold_id}/void")
            _validate_ledger_response(response)
            await _repository(request).complete_command(
                f"{payment_id}:card:authorization:void", {"hold_id": hold_id}
            )
        return ConfirmResponse(payment_id=payment_id, status="cancelled")

    return app


app = create_app()
