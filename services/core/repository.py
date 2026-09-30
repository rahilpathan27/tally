"""Persistence for payment intents, transitions, ledger commands and the outbox."""

from __future__ import annotations

import json
from typing import cast
from uuid import UUID, uuid4

import asyncpg
from libs.observability.metrics import PAYMENT_TRANSITIONS

from services.core.recovery import recovery_delay_seconds
from services.core.schemas import CreatePaymentIntent
from services.core.state_machine import PaymentState, transition_allowed


class LimitExceeded(ValueError):
    """A merchant limit set under maker-checker blocks this payment."""


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
            limits = await connection.fetchrow(
                "SELECT per_txn_max_minor, daily_max_minor FROM merchant_limits "
                "WHERE merchant_id = $1",
                merchant_id,
            )
            if limits is not None:
                if limits["per_txn_max_minor"] and body.amount_minor > limits["per_txn_max_minor"]:
                    raise LimitExceeded("amount exceeds the merchant's per-payment limit")
                if limits["daily_max_minor"]:
                    # Serialise creates for this merchant so concurrent requests cannot both
                    # pass the daily check.
                    await connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                        f"merchant-daily-limit:{merchant_id}",
                    )
                    today = await connection.fetchval(
                        """SELECT coalesce(sum(amount_minor), 0) FROM payment_intents
                           WHERE merchant_id = $1 AND status NOT IN ('failed', 'cancelled',
                                                                     'expired', 'reversed')
                             AND created_at >= date_trunc('day', clock_timestamp()
                                 AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata'""",
                        merchant_id,
                    )
                    if int(today) + body.amount_minor > limits["daily_max_minor"]:
                        raise LimitExceeded("payment would exceed the merchant's daily limit")
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
                       payment_method_token, payer_vpa, payee_vpa, status, mode, risk_context
                   ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'created', $9, $10::jsonb)
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
                "{}"
                if body.risk_context is None
                else body.risk_context.model_dump_json(exclude_none=True),
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
                PAYMENT_TRANSITIONS.labels(row["payment_method_type"], target.value).inc()
                await connection.execute(
                    """UPDATE payment_intents SET status = $3, updated_at = clock_timestamp(),
                           recovery_lease_until = NULL,
                           succeeded_at = CASE WHEN $3 = 'succeeded'
                               THEN clock_timestamp() ELSE succeeded_at END
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
                               payment_id, merchant_id, operation, idempotency_key, request,
                               subject_type, subject_id, kind
                           ) VALUES ($1, $2, $3, $4, $5::jsonb, 'payment', $1, $3)""",
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
                    if "ledger_hold_id" in extra_update:
                        await connection.execute(
                            """UPDATE payment_intents SET ledger_hold_id = $3
                               WHERE payment_id = $1 AND merchant_id = $2""",
                            payment_id,
                            merchant_id,
                            extra_update["ledger_hold_id"],
                        )
                    if "risk_outcome" in extra_update:
                        await connection.execute(
                            """UPDATE payment_intents SET risk_outcome = $3::jsonb
                               WHERE payment_id = $1 AND merchant_id = $2""",
                            payment_id,
                            merchant_id,
                            json.dumps(extra_update["risk_outcome"], default=str),
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
                          p.created_at, p.risk_context, p.risk_outcome, p.merchant_id,
                          payer.bank_id AS remitter_bank,
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

    async def stalled_in_flight(
        self, stale_after_seconds: float, limit: int = 100
    ) -> list[asyncpg.Record]:
        """Lease payments whose request path died between two durable effects.

        A live request moves ``authorizing``/``capturing`` forward within the outbound HTTP
        timeouts; anything older than ``stale_after_seconds`` with a pending command was
        abandoned by a crashed or stalled process and must be resumed from persisted state.
        """
        async with self.pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """SELECT p.payment_id, p.merchant_id, p.status, p.payment_method_type,
                          p.ledger_hold_id, c.idempotency_key, c.operation, c.request
                   FROM payment_intents p
                   JOIN core_ledger_commands c ON c.payment_id = p.payment_id
                   WHERE c.state = 'pending'
                     AND ((p.status = 'authorizing' AND c.operation IN ('place_hold', 'post_entry'))
                       OR (p.status = 'capturing' AND c.operation = 'post_hold')
                       OR (p.status = 'cancelled' AND c.operation = 'void_hold'))
                     AND p.updated_at <= clock_timestamp() - make_interval(secs => $1)
                     AND (p.recovery_lease_until IS NULL
                       OR p.recovery_lease_until <= clock_timestamp())
                   ORDER BY p.updated_at
                   FOR UPDATE OF p SKIP LOCKED LIMIT $2""",
                stale_after_seconds,
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

    async def release_lease(self, payment_id: UUID) -> None:
        await self.pool.execute(
            "UPDATE payment_intents SET recovery_lease_until = NULL WHERE payment_id = $1",
            payment_id,
        )

    async def reschedule_recovery(self, payment_id: UUID, attempts: int) -> None:
        delay_seconds = recovery_delay_seconds(attempts)
        await self.pool.execute(
            """UPDATE payment_intents SET recovery_attempts = $2,
                   next_recovery_at = clock_timestamp() + ($3 * interval '1 second'),
                   updated_at = clock_timestamp(), recovery_lease_until = NULL
               WHERE payment_id = $1
                 AND status IN ('pending_unknown', 'reversal_pending', 'reversed')""",
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
