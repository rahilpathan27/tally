from __future__ import annotations

import asyncio

import httpx
from services.simulators.bank.api import BankSimulatorConfig
from services.simulators.bank.api import create_app as create_bank_app
from services.simulators.network.api import CardNetworkConfig
from services.simulators.network.api import create_app as create_network_app
from services.simulators.payer_psp.api import PayerPspConfig
from services.simulators.payer_psp.api import create_app as create_psp_app


def test_simulator_failure_modes_and_message_idempotency() -> None:
    async def exercise() -> None:
        bank_app = create_bank_app(
            BankSimulatorConfig(
                modes={
                    "bank-a": "decline",
                    "bank-b": "timeout",
                    "bank-c": "http_500",
                    "bank-e": "late_success",
                }
            )
        )
        bank = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=bank_app), base_url="http://bank"
        )
        transfer = {
            "payment_id": "payment-1",
            "remitter_bank": "bank-a",
            "beneficiary_bank": "bank-d",
            "amount_minor": 500,
            "currency": "INR",
        }
        declined = await bank.post(
            "/v1/transfers", json=transfer, headers={"Idempotency-Key": "message-1"}
        )
        assert declined.status_code == 200
        assert declined.json()["status"] == "declined"
        replay = await bank.post(
            "/v1/transfers", json=transfer, headers={"Idempotency-Key": "message-1"}
        )
        assert replay.json() == declined.json()
        conflict = await bank.post(
            "/v1/transfers",
            json={**transfer, "amount_minor": 501},
            headers={"Idempotency-Key": "message-1"},
        )
        assert conflict.status_code == 409
        timeout = await bank.post(
            "/v1/transfers",
            json={**transfer, "payment_id": "payment-2", "remitter_bank": "bank-b"},
            headers={"Idempotency-Key": "message-2"},
        )
        assert timeout.status_code == 504
        late = await bank.post(
            "/v1/transfers",
            json={**transfer, "payment_id": "payment-late", "remitter_bank": "bank-e"},
            headers={"Idempotency-Key": "message-late"},
        )
        assert late.status_code == 504
        late_status = await bank.get("/v1/transfers/payment-late/status")
        assert late_status.json()["status"] == "approved"
        unavailable = await bank.post(
            "/v1/transfers",
            json={**transfer, "payment_id": "payment-3", "beneficiary_bank": "bank-c"},
            headers={"Idempotency-Key": "message-3"},
        )
        assert unavailable.status_code == 503

        psp_app = create_psp_app(PayerPspConfig(mode="decline"))
        psp = httpx.AsyncClient(transport=httpx.ASGITransport(app=psp_app), base_url="http://psp")
        approval = await psp.post(
            "/v1/approvals",
            json={"payment_id": "payment-4", "payer_vpa": "payer@bank-a", "amount_minor": 100},
            headers={"Idempotency-Key": "psp-message-1"},
        )
        assert approval.status_code == 200
        assert approval.json()["status"] == "declined"

        network_app = create_network_app(
            CardNetworkConfig("http://unused", "vault-key", "network-key", mode="http_500")
        )
        network = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=network_app), base_url="http://network"
        )
        failed_network = await network.post(
            "/v1/authorizations",
            json={
                "payment_id": "payment-5",
                "payment_method_token": "vlt_test_token",
                "amount_minor": 100,
                "currency": "INR",
            },
            headers={"X-Simulator-Key": "network-key", "Idempotency-Key": "network-message-1"},
        )
        assert failed_network.status_code == 503
        await bank.aclose()
        await psp.aclose()
        await network.aclose()

    asyncio.run(exercise())


def test_bank_circuit_breaker_opens_and_resets() -> None:
    from time import monotonic

    from services.core.recovery import CircuitBreaker, recovery_delay_seconds

    breaker = CircuitBreaker(failure_threshold=2, reset_after_seconds=5)
    assert breaker.allow_request()
    breaker.record_failure()
    assert breaker.allow_request()
    breaker.record_failure()
    assert not breaker.allow_request()
    breaker.opened_at = monotonic() - 6
    assert breaker.allow_request()
    assert recovery_delay_seconds(0) == 1
    assert recovery_delay_seconds(3) == 8
    assert recovery_delay_seconds(20) == 300
