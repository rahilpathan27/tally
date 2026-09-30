"""Card network simulator; the vault is the only service that handles PAN data."""

from __future__ import annotations

import hmac
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal, cast

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from services.vault.model import luhn_valid


class AuthorizationRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    payment_id: str = Field(min_length=1, max_length=64)
    payment_method_token: str = Field(min_length=8, max_length=200)
    amount_minor: int = Field(strict=True, gt=0, le=9_007_199_254_740_991)
    currency: Literal["INR"]


class AuthorizationResponse(BaseModel):
    status: Literal["approved", "declined"]
    network_reference: str


@dataclass(frozen=True, slots=True)
class CardNetworkConfig:
    vault_url: str
    vault_network_key: str
    service_key: str
    mode: str = "approve"


def create_app(config: CardNetworkConfig | None = None) -> FastAPI:
    if config is None:
        config = CardNetworkConfig(
            vault_url=os.environ.get("TALLY_VAULT_URL", "http://127.0.0.1:8002"),
            vault_network_key=os.environ.get("TALLY_VAULT_NETWORK_KEY", ""),
            service_key=os.environ.get("TALLY_NETWORK_SIMULATOR_KEY", ""),
            mode=os.environ.get("TALLY_CARD_NETWORK_MODE", "approve"),
        )
    if config.mode not in {"approve", "decline", "http_500", "timeout", "late_success"}:
        raise RuntimeError("unsupported card network simulator mode")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = httpx.AsyncClient(timeout=5.0)
        app.state.http_client = client
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(title="Tally Card Network Simulator", version="1.0.0", lifespan=lifespan)
    app.state.config = config
    app.state.requests = {}
    app.state.statuses = {}

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/authorizations", response_model=AuthorizationResponse)
    async def authorize(
        body: AuthorizationRequest,
        x_simulator_key: str | None = Header(default=None, alias="X-Simulator-Key"),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ) -> AuthorizationResponse:
        current: CardNetworkConfig = app.state.config
        if not x_simulator_key or not hmac.compare_digest(x_simulator_key, current.service_key):
            raise HTTPException(401, "simulator authentication failed")
        fingerprint = body.model_dump_json()
        requests = cast(dict[str, tuple[str, AuthorizationResponse]], app.state.requests)
        prior = requests.get(idempotency_key)
        if prior:
            if prior[0] != fingerprint:
                raise HTTPException(409, "card network idempotency key payload mismatch")
            return prior[1]
        if current.mode == "http_500":
            raise HTTPException(503, "simulated card network outage")
        if not current.vault_network_key:
            raise HTTPException(503, "vault connection is not configured")
        client: httpx.AsyncClient = app.state.http_client
        try:
            response = await client.post(
                f"{current.vault_url}/internal/v1/tokens/{body.payment_method_token}/detokenize",
                headers={"x-vault-network-key": current.vault_network_key},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(503, "vault authorization is unavailable") from exc
        pan = response.json().get("pan")
        if not isinstance(pan, str) or not luhn_valid(pan):
            raise HTTPException(422, "vault returned an invalid test card")
        result = AuthorizationResponse(
            status="declined" if current.mode == "decline" else "approved",
            network_reference=f"net_{body.payment_id}",
        )
        requests[idempotency_key] = (fingerprint, result)
        app.state.statuses[body.payment_id] = result.status
        if current.mode in {"timeout", "late_success"}:
            raise HTTPException(504, "simulated network response lost after authorization")
        return result

    @app.post("/internal/v1/modes")
    async def set_mode(
        body: dict[str, str | None], x_simulator_admin: str = Header(default="")
    ) -> dict[str, str]:
        expected = os.environ.get("TALLY_SIMULATOR_ADMIN_KEY", "")
        if not expected or not hmac.compare_digest(expected, x_simulator_admin):
            raise HTTPException(401, "simulator admin authentication failed")
        mode = body.get("mode")
        current: CardNetworkConfig = app.state.config
        if mode is not None:
            if mode not in {"approve", "decline", "http_500", "timeout", "late_success"}:
                raise HTTPException(422, "unknown simulator mode")
            app.state.config = CardNetworkConfig(
                current.vault_url, current.vault_network_key, current.service_key, mode
            )
        return {"mode": app.state.config.mode}

    @app.get("/v1/authorizations/{payment_id}/status")
    async def authorization_status(payment_id: str) -> dict[str, str]:
        return {
            "payment_id": payment_id,
            "status": app.state.statuses.get(payment_id, "not_found"),
        }

    return app


app = create_app()
