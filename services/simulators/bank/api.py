"""Configurable deterministic bank simulator for the local payment flow."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
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
    status: Literal["approved", "declined"]
    bank_reference: str


@dataclass(slots=True)
class BankSimulatorConfig:
    modes: dict[str, str] = field(default_factory=dict)
    requests: dict[str, tuple[str, TransferResponse]] = field(default_factory=dict)


def _config_from_environment() -> BankSimulatorConfig:
    raw_modes = os.environ.get("TALLY_BANK_SIM_MODES", "{}")
    try:
        modes = json.loads(raw_modes)
    except json.JSONDecodeError as exc:
        raise RuntimeError("TALLY_BANK_SIM_MODES must be a JSON object") from exc
    if not isinstance(modes, dict) or any(
        not isinstance(bank, str) or mode not in {"approve", "decline", "timeout", "http_500"}
        for bank, mode in modes.items()
    ):
        raise RuntimeError("bank modes must map bank IDs to approve, decline, timeout, or http_500")
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
        if "timeout" in modes:
            await asyncio.sleep(0.1)
            raise HTTPException(504, "simulated bank timeout")
        if "http_500" in modes:
            raise HTTPException(503, "simulated bank outage")
        result = TransferResponse(
            status="declined" if "decline" in modes else "approved",
            bank_reference=f"bank_{body.payment_id}",
        )
        current.requests[idempotency_key] = (fingerprint, result)
        return result

    return app


app = create_app()
