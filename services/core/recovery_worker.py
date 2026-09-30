"""Recovery worker that resolves unknown external outcomes from persisted state."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from fastapi import HTTPException, status

from services.core.faults import fault_point
from services.core.recovery import CircuitBreaker
from services.core.repository import PaymentIntentRepository
from services.core.state_machine import PaymentState


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
    faults: Any = None,
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
        is_card = row["payment_method_type"] == "card"
        if row["status"] == PaymentState.PENDING_UNKNOWN.value or (
            row["status"] == PaymentState.REVERSAL_PENDING.value and not is_card
        ):
            # The ledger is the source of truth for money. A crash or lost response after the
            # ledger committed leaves the command pending here, so ask the ledger first and never
            # decide an outcome while its state for this key is unknown.
            lookup_path = "/v1/holds/by-key" if is_card else "/v1/entries/by-key"
            try:
                lookup = await ledger_http.get(
                    lookup_path, params={"idempotency_key": row["idempotency_key"]}
                )
            except httpx.HTTPError:
                await repository.reschedule_recovery(payment_id, row["recovery_attempts"] + 1)
                continue
            if lookup.status_code == 200:
                recorded = cast(dict[str, object], lookup.json())
                if is_card and recorded["status"] != "pending":
                    await repository.reschedule_recovery(payment_id, row["recovery_attempts"] + 1)
                    continue
                accepted, _, _ = await repository.transition(
                    payment_id,
                    row["merchant_id"],
                    PaymentState.AUTHORIZED if is_card else PaymentState.SUCCEEDED,
                    "recovery-worker",
                    "ledger already committed this payment's deterministic command",
                    str(payment_id),
                    extra_update=(
                        {"ledger_hold_id": int(cast(str, recorded["hold_id"]))} if is_card else None
                    ),
                    completed_command=(row["idempotency_key"], recorded, False),
                )
                processed += int(accepted)
                continue
            if lookup.status_code != 404:
                await repository.reschedule_recovery(payment_id, row["recovery_attempts"] + 1)
                continue
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
                fault_point(faults, "recovery.after_ledger_call")
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
            fault_point(faults, "recovery.after_ledger_call")
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


async def sweep_stalled_payments(
    repository: PaymentIntentRepository,
    ledger_http: httpx.AsyncClient,
    *,
    stale_after_seconds: float = 30,
    limit: int = 100,
    faults: Any = None,
) -> int:
    """Resume payments abandoned mid-request by a crashed or stalled process.

    * ``authorizing``: the external leg may or may not have been sent. Move to
      ``pending_unknown`` so the status-check policy decides from external truth.
    * ``capturing``: the capture decision is durable; replay the idempotent hold post.
    * ``cancelled`` with a pending void: replay the idempotent hold void.
    """
    resumed = 0
    for row in await repository.stalled_in_flight(stale_after_seconds, limit):
        payment_id = row["payment_id"]
        merchant_id = row["merchant_id"]
        correlation_id = str(payment_id)
        if row["status"] == PaymentState.AUTHORIZING.value:
            accepted, _, _ = await repository.transition(
                payment_id,
                merchant_id,
                PaymentState.PENDING_UNKNOWN,
                "recovery-worker",
                "in-flight confirmation stalled or crashed; outcome requires status check",
                correlation_id,
            )
            resumed += int(accepted)
            continue
        hold_id = row["ledger_hold_id"]
        if hold_id is None:
            await repository.release_lease(payment_id)
            continue
        action = "post" if row["status"] == PaymentState.CAPTURING.value else "void"
        try:
            result = _validate_ledger_response(
                await ledger_http.post(f"/v1/holds/{int(hold_id)}/{action}")
            )
        except (httpx.HTTPError, HTTPException):
            # The lease expires on its own; the next sweep retries the same idempotent call.
            continue
        fault_point(faults, "recovery.after_ledger_call")
        if action == "void":
            await repository.complete_command(row["idempotency_key"], result)
            resumed += 1
            continue
        accepted, _, _ = await repository.transition(
            payment_id,
            merchant_id,
            PaymentState.SUCCEEDED,
            "recovery-worker",
            "resumed interrupted capture; ledger hold posted",
            correlation_id,
            completed_command=(row["idempotency_key"], result, False),
        )
        resumed += int(accepted)
    return resumed
