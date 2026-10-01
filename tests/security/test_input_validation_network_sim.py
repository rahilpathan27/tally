"""The card-network simulator must not let a token rewrite the vault URL it calls."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from services.simulators.network.api import CardNetworkConfig, create_app


@pytest.mark.parametrize(
    "token",
    ["vlt_../../admin", "vlt_abc/def/ghi", "vlt_abcdefgh?x=1", "../internal/v1", "vlt_%2e%2e%2f"],
)
def test_token_cannot_traverse_vault_paths(token: str) -> None:
    async def exercise() -> int:
        app = create_app(CardNetworkConfig("http://vault", "vault-key", "network-key"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://network"
        ) as client:
            response = await client.post(
                "/v1/authorizations",
                json={
                    "payment_id": "p-1",
                    "payment_method_token": token,
                    "amount_minor": 100,
                    "currency": "INR",
                },
                headers={"X-Simulator-Key": "network-key", "Idempotency-Key": "k-1"},
            )
            return response.status_code

    assert asyncio.run(exercise()) == 422
