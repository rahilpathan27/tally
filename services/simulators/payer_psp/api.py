"""Payer PSP simulator for the UPI-style confirmation step."""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass, field
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class PayerApprovalRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    payment_id: str = Field(min_length=1, max_length=64)
    payer_vpa: str = Field(min_length=3, max_length=130)
    amount_minor: int = Field(strict=True, gt=0, le=9_007_199_254_740_991)


class PayerApprovalResponse(BaseModel):
    status: Literal["approved", "declined"]
    psp_reference: str


@dataclass(slots=True)
class PayerPspConfig:
    mode: str = "approve"
    requests: dict[str, tuple[str, PayerApprovalResponse]] = field(default_factory=dict)


def create_app(config: PayerPspConfig | None = None) -> FastAPI:
    current = config or PayerPspConfig(mode=os.environ.get("TALLY_PAYER_PSP_MODE", "approve"))
    if current.mode not in {"approve", "decline", "http_500"}:
        raise RuntimeError("payer PSP mode must be approve, decline, or http_500")
    app = FastAPI(title="Tally Payer PSP Simulator", version="1.0.0")
    app.state.config = current

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/approvals", response_model=PayerApprovalResponse)
    async def approve(
        body: PayerApprovalRequest,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ) -> PayerApprovalResponse:
        config_now: PayerPspConfig = app.state.config
        fingerprint = body.model_dump_json()
        prior = config_now.requests.get(idempotency_key)
        if prior:
            if prior[0] != fingerprint:
                raise HTTPException(409, "PSP idempotency key payload mismatch")
            return prior[1]
        if config_now.mode == "http_500":
            raise HTTPException(503, "simulated payer PSP outage")
        result = PayerApprovalResponse(
            status="approved" if config_now.mode == "approve" else "declined",
            psp_reference=f"psp_{body.payment_id}",
        )
        config_now.requests[idempotency_key] = (fingerprint, result)
        return result

    @app.post("/internal/v1/modes")
    async def set_mode(
        body: dict[str, str | None], x_simulator_admin: str = Header(default="")
    ) -> dict[str, str]:
        expected = os.environ.get("TALLY_SIMULATOR_ADMIN_KEY", "")
        if not expected or not hmac.compare_digest(expected, x_simulator_admin):
            raise HTTPException(401, "simulator admin authentication failed")
        mode = body.get("mode")
        if mode is not None:
            if mode not in {"approve", "decline", "http_500"}:
                raise HTTPException(422, "unknown simulator mode")
            app.state.config.mode = mode
        return {"mode": app.state.config.mode}

    return app


app = create_app()
