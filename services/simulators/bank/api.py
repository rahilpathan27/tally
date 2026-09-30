"""Configurable deterministic bank simulator for the local payment flow."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class TransferRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    payment_id: str = Field(min_length=1, max_length=64)
    remitter_bank: str = Field(min_length=1, max_length=64)
    beneficiary_bank: str = Field(min_length=1, max_length=64)
    amount_minor: int = Field(strict=True, gt=0, le=9_007_199_254_740_991)
    currency: Literal["INR"]


class TransferResponse(BaseModel):
    status: Literal["approved", "declined", "debit_succeeded_credit_failed"]
    bank_reference: str


class RefundRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    refund_id: str = Field(min_length=1, max_length=64)
    payment_id: str = Field(min_length=1, max_length=64)
    amount_minor: int = Field(strict=True, gt=0, le=9_007_199_254_740_991)
    currency: Literal["INR"]


class RefundResponse(BaseModel):
    status: Literal["succeeded", "failed"]
    bank_reference: str


class PayoutRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    payout_id: str = Field(min_length=1, max_length=64)
    merchant_id: str = Field(min_length=1, max_length=64)
    amount_minor: int = Field(strict=True, gt=0, le=9_007_199_254_740_991)
    currency: Literal["INR"]


class PayoutResponse(BaseModel):
    status: Literal["paid", "returned"]
    bank_reference: str


RAIL_MODES = {"approve", "decline", "timeout", "http_500"}


class ModeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modes: dict[str, str] = Field(default_factory=dict)
    refund_mode: str | None = None
    payout_mode: str | None = None


def utr(seed: str) -> str:
    """Deterministic 16-character UTR-style bank reference."""
    return "UTR" + str(int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16))[:13].zfill(13)


@dataclass(slots=True)
class BankSimulatorConfig:
    modes: dict[str, str] = field(default_factory=dict)
    requests: dict[str, tuple[str, TransferResponse]] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)
    transfer_banks: dict[str, tuple[str, str]] = field(default_factory=dict)
    refund_mode: str = "approve"
    payout_mode: str = "approve"
    rail_requests: dict[str, tuple[str, dict[str, str]]] = field(default_factory=dict)
    rail_statuses: dict[str, str] = field(default_factory=dict)
    # Every decided money movement, in decision order: the simulator's "bank truth" used to
    # produce settlement files for reconciliation.
    journal: list[dict[str, object]] = field(default_factory=list)


def _config_from_environment() -> BankSimulatorConfig:
    raw_modes = os.environ.get("TALLY_BANK_SIM_MODES", "{}")
    try:
        modes = json.loads(raw_modes)
    except json.JSONDecodeError as exc:
        raise RuntimeError("TALLY_BANK_SIM_MODES must be a JSON object") from exc
    if not isinstance(modes, dict) or any(
        not isinstance(bank, str)
        or mode
        not in {
            "approve",
            "decline",
            "credit_failure",
            "timeout",
            "late_success",
            "status_unknown",
            "http_500",
            "reverse_timeout",
        }
        for bank, mode in modes.items()
    ):
        raise RuntimeError(
            "bank modes must map bank IDs to approve, decline, credit_failure, timeout, "
            "late_success, status_unknown, http_500, or reverse_timeout"
        )
    return BankSimulatorConfig(modes=modes)


def create_app(config: BankSimulatorConfig | None = None) -> FastAPI:
    app = FastAPI(title="Tally Bank Simulator", version="1.0.0")
    app.state.config = config or _config_from_environment()

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/transfers", response_model=TransferResponse)
    async def transfer(
        body: TransferRequest,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ) -> TransferResponse:
        current: BankSimulatorConfig = app.state.config
        fingerprint = body.model_dump_json()
        prior = current.requests.get(idempotency_key)
        if prior is not None:
            if prior[0] != fingerprint:
                raise HTTPException(409, "bank idempotency key payload mismatch")
            return prior[1]

        modes = {
            current.modes.get(body.remitter_bank, "approve"),
            current.modes.get(body.beneficiary_bank, "approve"),
        }
        current.transfer_banks[body.payment_id] = (body.remitter_bank, body.beneficiary_bank)
        if "http_500" in modes:
            raise HTTPException(503, "simulated bank outage")
        result = TransferResponse(
            status=(
                "debit_succeeded_credit_failed"
                if "credit_failure" in modes
                else "declined"
                if "decline" in modes
                else "approved"
            ),
            bank_reference=utr(f"transfer:{body.payment_id}"),
        )
        current.requests[idempotency_key] = (fingerprint, result)
        if result.status == "approved":
            current.journal.append(
                {
                    "kind": "upi_transfer",
                    "reference": body.payment_id,
                    "bank_reference": result.bank_reference,
                    "amount_minor": body.amount_minor,
                    "status": "approved",
                    "decided_at": datetime.now(UTC).isoformat(),
                }
            )
        if not modes.intersection({"timeout", "status_unknown"}):
            current.statuses[body.payment_id] = result.status
        if modes.intersection({"timeout", "late_success", "status_unknown"}):
            await asyncio.sleep(0.1)
            raise HTTPException(504, "simulated response lost after bank decision")
        return result

    @app.get("/v1/transfers/{payment_id}/status")
    async def transfer_status(payment_id: str) -> dict[str, str]:
        current: BankSimulatorConfig = app.state.config
        banks = current.transfer_banks.get(payment_id, ())
        if any(current.modes.get(bank) == "status_unknown" for bank in banks):
            return {"payment_id": payment_id, "status": "unknown"}
        return {"payment_id": payment_id, "status": current.statuses.get(payment_id, "not_found")}

    @app.post("/v1/transfers/{payment_id}/reverse")
    async def reverse_transfer(payment_id: str) -> dict[str, str]:
        current: BankSimulatorConfig = app.state.config
        prior_status = current.statuses.get(payment_id, "not_found")
        if prior_status == "reversed":
            return {"payment_id": payment_id, "status": "reversed"}
        if prior_status != "debit_succeeded_credit_failed":
            raise HTTPException(409, "transfer has no reversible debit leg")
        current.statuses[payment_id] = "reversed"
        banks = current.transfer_banks.get(payment_id, ())
        if any(current.modes.get(bank) == "reverse_timeout" for bank in banks):
            raise HTTPException(504, "simulated reversal response lost after bank reversal")
        return {"payment_id": payment_id, "status": "reversed"}

    async def _rail(
        kind: str,
        mode: str,
        reference: str,
        idempotency_key: str,
        fingerprint: str,
        amount_minor: int,
        success: str,
        failure: str,
    ) -> dict[str, str]:
        current: BankSimulatorConfig = app.state.config
        prior = current.rail_requests.get(idempotency_key)
        if prior is not None:
            if prior[0] != fingerprint:
                raise HTTPException(409, f"{kind} idempotency key payload mismatch")
            return prior[1]
        if mode == "http_500":
            raise HTTPException(503, f"simulated {kind} rail outage")
        outcome = failure if mode == "decline" else success
        result = {"status": outcome, "bank_reference": utr(f"{kind}:{idempotency_key}")}
        current.rail_requests[idempotency_key] = (fingerprint, result)
        current.rail_statuses[reference] = outcome
        current.journal.append(
            {
                "kind": kind,
                "reference": reference,
                "bank_reference": result["bank_reference"],
                "amount_minor": amount_minor,
                "status": outcome,
                "decided_at": datetime.now(UTC).isoformat(),
            }
        )
        if mode == "timeout":
            raise HTTPException(504, f"simulated {kind} response lost after bank decision")
        return result

    @app.post("/v1/refunds", response_model=RefundResponse)
    async def refund(
        body: RefundRequest,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ) -> dict[str, str]:
        current: BankSimulatorConfig = app.state.config
        return await _rail(
            "refund",
            current.refund_mode,
            body.refund_id,
            idempotency_key,
            body.model_dump_json(),
            body.amount_minor,
            "succeeded",
            "failed",
        )

    @app.get("/v1/refunds/{refund_id}/status")
    async def refund_status(refund_id: str) -> dict[str, str]:
        current: BankSimulatorConfig = app.state.config
        return {"refund_id": refund_id, "status": current.rail_statuses.get(refund_id, "not_found")}

    @app.post("/v1/payouts", response_model=PayoutResponse)
    async def payout(
        body: PayoutRequest,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ) -> dict[str, str]:
        current: BankSimulatorConfig = app.state.config
        return await _rail(
            "payout",
            current.payout_mode,
            body.payout_id,
            idempotency_key,
            body.model_dump_json(),
            body.amount_minor,
            "paid",
            "returned",
        )

    @app.get("/v1/payouts/{payout_id}/status")
    async def payout_status(payout_id: str) -> dict[str, str]:
        current: BankSimulatorConfig = app.state.config
        return {"payout_id": payout_id, "status": current.rail_statuses.get(payout_id, "not_found")}

    @app.post("/internal/v1/modes")
    async def set_modes(
        body: ModeUpdate, x_simulator_admin: str = Header(default="")
    ) -> dict[str, object]:
        """Chaos control for demos; disabled unless an admin key is configured."""
        expected = os.environ.get("TALLY_SIMULATOR_ADMIN_KEY", "")
        if not expected or not hmac.compare_digest(expected, x_simulator_admin):
            raise HTTPException(401, "simulator admin authentication failed")
        allowed = {
            "approve",
            "decline",
            "credit_failure",
            "timeout",
            "late_success",
            "status_unknown",
            "http_500",
            "reverse_timeout",
        }
        if any(mode not in allowed for mode in body.modes.values()) or any(
            mode is not None and mode not in RAIL_MODES
            for mode in (body.refund_mode, body.payout_mode)
        ):
            raise HTTPException(422, "unknown simulator mode")
        current: BankSimulatorConfig = app.state.config
        current.modes = dict(body.modes)
        current.refund_mode = body.refund_mode or current.refund_mode
        current.payout_mode = body.payout_mode or current.payout_mode
        return {
            "modes": current.modes,
            "refund_mode": current.refund_mode,
            "payout_mode": current.payout_mode,
        }

    @app.get("/internal/v1/journal")
    async def journal() -> list[dict[str, object]]:
        current: BankSimulatorConfig = app.state.config
        return list(current.journal)

    return app


app = create_app()
