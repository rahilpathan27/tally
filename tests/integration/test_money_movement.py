"""Phase 8 end-to-end: refunds, disputes, settlement, payouts, fees and webhooks."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from chaos.stack import LocalStack, build_stack, provision_databases
from fastapi import FastAPI, Request
from libs.common.business_time import business_date
from libs.common.object_store import FilesystemObjectStore
from libs.money import Currency
from libs.security.webhook_signing import verify_signature
from services.core.fees import FeeSchedule, payment_fee
from services.core.money_routes import run_money_workers

INTERNAL = {"x-internal-key": "stack-recovery-key"}


def _receiver() -> tuple[FastAPI, list[dict[str, Any]]]:
    app = FastAPI()
    received: list[dict[str, Any]] = []
    app.state.fail = False

    @app.post("/hook")
    async def hook(request: Request) -> dict[str, str]:
        body = await request.body()
        received.append(
            {
                "body": body,
                "signature": request.headers.get("tally-signature", ""),
                "host": request.headers.get("host"),
            }
        )
        if app.state.fail:
            from fastapi import HTTPException

            raise HTTPException(500, "receiver down")
        return {"ok": "yes"}

    return app, received


async def _pay_upi(stack: LocalStack, amount: int, tag: str) -> str:
    created = await stack.send(
        "POST",
        "/v1/payment_intents",
        {
            "amount_minor": amount,
            "currency": "INR",
            "payment_method_type": "upi",
            "payer_vpa": "payer@bank-a",
            "payee_vpa": "merchant@bank-b",
        },
        f"{tag}-create",
    )
    assert created.status_code == 201, created.text
    payment_id = str(created.json()["payment_id"])
    confirmed = await stack.send(
        "POST", f"/v1/payment_intents/{payment_id}/confirm", {}, f"{tag}-confirm"
    )
    assert confirmed.json()["status"] == "succeeded", confirmed.text
    return payment_id


async def _balance(stack: LocalStack, account: str) -> int:
    return int(
        await stack.ledger_pool.fetchval(
            "SELECT posted_minor FROM ledger_account_balances WHERE account_id = $1", account
        )
        or 0
    )


async def _age(stack: LocalStack, payment_ids: list[str], days: int) -> None:
    await stack.general_pool.execute(
        """UPDATE payment_intents SET succeeded_at = clock_timestamp() - make_interval(days => $2)
           WHERE payment_id = ANY($1::uuid[])""",
        [UUID(p) for p in payment_ids],
        days,
    )


async def _unsettled_sales(stack: LocalStack) -> int:
    return int(
        await stack.general_pool.fetchval(
            """SELECT coalesce(sum(amount_minor), 0) FROM payment_intents p
               WHERE merchant_id = $1 AND status = 'succeeded' AND NOT EXISTS (
                   SELECT 1 FROM settlement_items i
                   WHERE i.item_type = 'payment' AND i.item_id = p.payment_id)""",
            stack.merchant_id,
        )
    )


async def _integrity(stack: LocalStack) -> None:
    rows = await stack.ledger_pool.fetch("SELECT ok FROM ledger_verify_integrity()")
    assert all(row["ok"] for row in rows)


def test_refunds_settlement_payouts_disputes_and_webhooks(tmp_path: Path) -> None:
    if os.environ.get("TALLY_STACK_TESTS") != "1":
        pytest.skip("set TALLY_STACK_TESTS=1 with local Compose services to run stack tests")

    async def exercise() -> None:
        general, ledger, vault = await provision_databases("tally_it_money")
        receiver, received = _receiver()
        webhook_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=receiver))
        stack = await build_stack(
            general,
            ledger,
            vault,
            os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
            object_store=FilesystemObjectStore(tmp_path),
            webhook_http=webhook_http,
            trusted_webhook_hosts=("webhooks.test",),
        )
        state = stack.core_app.state  # type: ignore[attr-defined]
        bank = stack.bank_app.state.config  # type: ignore[attr-defined]
        m = stack.merchant_id
        payable = f"merchant:{m}:payable:INR"
        receivable = f"merchant:{m}:receivable:INR"
        try:
            # Webhook endpoint registration: SSRF targets are rejected, the secret shown once.
            unsafe = await stack.send(
                "POST",
                "/v1/webhook_endpoints",
                {"url": "https://169.254.169.254/latest", "enabled_events": ["*"]},
                "wh-unsafe",
            )
            assert unsafe.status_code == 422
            endpoint = await stack.send(
                "POST",
                "/v1/webhook_endpoints",
                {
                    "url": "http://webhooks.test/hook",
                    "enabled_events": [
                        "payment_intent.*",
                        "refund.*",
                        "settlement.*",
                        "dispute.*",
                        "payout.*",
                    ],
                },
                "wh-create",
            )
            assert endpoint.status_code == 201, endpoint.text
            secret = endpoint.json()["secret"].encode()
            listed = await stack.send("GET", "/v1/webhook_endpoints")
            assert listed.json()[0]["secret"] is None

            await stack.general_pool.execute(
                """INSERT INTO merchant_settlement_configs(
                       merchant_id, settlement_delay_days, fee_rate, fixed_fee_minor, gst_rate,
                       reserve_rate, reserve_hold_days
                   ) VALUES ($1, 2, 0.02, 300, 0.18, 0.05, 0)""",
                m,
            )
            old = [
                await _pay_upi(stack, amount, f"old{i}")
                for i, amount in enumerate((100_000, 250_050, 75_025))
            ]
            fresh = await _pay_upi(stack, 40_000, "fresh")
            await _age(stack, old, 3)

            # Refunds: partial, over-refund rejected, concurrent storm yields one winner.
            partial = await stack.send(
                "POST", "/v1/refunds", {"payment_id": old[0], "amount_minor": 30_000}, "r1"
            )
            assert partial.status_code == 201 and partial.json()["status"] == "processing"
            over = await stack.send(
                "POST", "/v1/refunds", {"payment_id": old[0], "amount_minor": 70_001}, "r2"
            )
            assert over.status_code == 422
            assert over.json()["detail"]["code"] == "REFUND_EXCEEDS_REFUNDABLE"
            replay = await stack.send(
                "POST", "/v1/refunds", {"payment_id": old[0], "amount_minor": 70_001}, "r2"
            )
            assert replay.json() == over.json()
            storm = await asyncio.gather(
                *(
                    stack.send(
                        "POST",
                        "/v1/refunds",
                        {"payment_id": old[1], "amount_minor": 150_000},
                        f"storm-{i}",
                    )
                    for i in range(8)
                )
            )
            assert sorted(r.status_code for r in storm).count(201) == 1
            assert all(r.status_code in (201, 422) for r in storm)

            # A refund the bank keeps declining escalates, then ops cancels it.
            await run_money_workers(state)
            bank.refund_mode = "decline"
            doomed = await stack.send(
                "POST", "/v1/refunds", {"payment_id": old[2], "amount_minor": 5_000}, "r3"
            )
            doomed_id = doomed.json()["refund_id"]
            for _ in range(6):
                await stack.general_pool.execute(
                    "UPDATE refunds SET next_attempt_at = clock_timestamp() WHERE refund_id = $1",
                    UUID(doomed_id),
                )
                await run_money_workers(state)
            assert (await stack.send("GET", f"/v1/refunds/{doomed_id}")).json()[
                "status"
            ] == "requires_action"
            cancelled = await stack.client.post(
                f"/internal/v1/refunds/{doomed_id}/cancel", headers=INTERNAL
            )
            assert cancelled.json()["status"] == "cancelled", cancelled.text
            bank.refund_mode = "approve"
            await run_money_workers(state)
            refunds = await stack.general_pool.fetch(
                "SELECT status FROM refunds WHERE merchant_id = $1", m
            )
            assert sorted(r["status"] for r in refunds) == ["cancelled", "succeeded", "succeeded"]
            assert await _balance(stack, "platform:refunds_clearing:INR") == 0

            # Settlement: fees, GST, reserve; payable keeps only not-yet-eligible sales.
            today = business_date(datetime.now(UTC))
            run = await stack.client.post(
                "/internal/v1/settlements/run",
                json={"merchant_id": m, "business_date": today.isoformat()},
                headers=INTERNAL,
            )
            assert run.status_code == 200, run.text
            settlement = run.json()
            schedule = FeeSchedule(Decimal("0.02"), 300, Decimal("0.18"), Decimal("0.05"))
            fees = sum(payment_fee(a, schedule, Currency.INR) for a in (100_000, 250_050, 75_025))
            assert settlement["gross_minor"] == 425_075
            assert settlement["payable_debits_minor"] == 30_000 + 150_000 + 5_000
            assert settlement["payable_credits_minor"] == 5_000
            assert settlement["fee_minor"] == fees
            assert fees == 2_300 + 5_301 + 1_800  # 1,500.5 paise rounds half-even to 1,500
            assert settlement["gst_minor"] == 1_692  # 18% of 9,401 = 1,692.18
            assert settlement["reserve_held_minor"] == 21_254  # 5% of 425,075 = 21,253.75
            assert settlement["status"] == "posted"
            assert await _balance(stack, payable) == await _unsettled_sales(stack) == 40_000
            again = await stack.client.post(
                "/internal/v1/settlements/run",
                json={"merchant_id": m, "business_date": today.isoformat()},
                headers=INTERNAL,
            )
            assert again.json()["settlement_id"] == settlement["settlement_id"]
            detail = await stack.send("GET", f"/v1/settlements/{settlement['settlement_id']}")
            assert len(detail.json()["items"]) == 7
            fee_income = await stack.ledger_pool.fetchval(
                "SELECT balance_minor FROM ledger_trial_balance() "
                "WHERE account_id = 'platform:fee_income:INR'"
            )
            assert fee_income == fees

            # Payout: instruction file written, bank pays, in-transit clears.
            await run_money_workers(state)
            payout = (
                await stack.send("GET", f"/v1/settlements/{settlement['settlement_id']}")
            ).json()["payout"]
            assert payout["status"] == "paid"
            assert payout["amount_minor"] == settlement["net_payout_minor"]
            assert (tmp_path / payout["instruction_object_key"]).read_text().count("\n") == 2
            assert await _balance(stack, f"merchant:{m}:payout_in_transit:INR") == 0

            # Refund after settlement exceeds payable; the shortfall becomes receivable.
            late = await stack.send(
                "POST", "/v1/refunds", {"payment_id": old[2], "amount_minor": 60_000}, "r4"
            )
            assert late.json()["payable_portion_minor"] == 40_000
            assert late.json()["receivable_portion_minor"] == 20_000
            assert await _balance(stack, receivable) == 20_000

            # Dispute on the fresh payment: debit, evidence, win returns the funds.
            fresh_2 = await _pay_upi(stack, 90_000, "fresh2")
            opened = await stack.client.post(
                "/internal/v1/disputes",
                json={
                    "payment_id": fresh_2,
                    "amount_minor": 90_000,
                    "reason_code": "fraudulent",
                    "network_reference": "cb-0001",
                },
                headers=INTERNAL,
            )
            assert opened.status_code == 201 and opened.json()["status"] == "needs_response"
            dispute_id = opened.json()["dispute_id"]
            evidence = await stack.send(
                "POST",
                f"/v1/disputes/{dispute_id}/evidence",
                {
                    "text": "Delivered with signature",
                    "document_base64": base64.b64encode(b"%PDF-1.4 receipt").decode(),
                    "document_name": "receipt.pdf",
                },
                "evidence-1",
            )
            assert (
                evidence.json()["status"] == "under_review" and evidence.json()["evidence_sha256"]
            )
            won = await stack.client.post(
                f"/internal/v1/disputes/{dispute_id}/resolve",
                json={"outcome": "won"},
                headers=INTERNAL,
            )
            assert won.json()["status"] == "won", won.text
            label = await stack.general_pool.fetchval(
                "SELECT label FROM risk_labels WHERE source_id = $1", dispute_id
            )
            assert label == "fraud"

            # Next settlement: payout returned by the bank, receivable recovered.
            await _age(stack, [fresh, fresh_2], 3)
            bank.payout_mode = "decline"
            tomorrow = today + timedelta(days=1)
            second = await stack.client.post(
                "/internal/v1/settlements/run",
                json={"merchant_id": m, "business_date": tomorrow.isoformat()},
                headers=INTERNAL,
            )
            second_body = second.json()
            assert second_body["reserve_released_minor"] == 21_254
            assert second_body["recovered_minor"] == 20_000
            await run_money_workers(state)
            returned = await stack.general_pool.fetchrow(
                "SELECT status, amount_minor FROM payouts WHERE settlement_id = $1",
                UUID(second_body["settlement_id"]),
            )
            assert returned["status"] == "returned"
            assert await _balance(stack, receivable) == 0
            assert await _balance(stack, payable) == returned["amount_minor"]
            bank.payout_mode = "approve"
            third = await stack.client.post(
                "/internal/v1/settlements/run",
                json={
                    "merchant_id": m,
                    "business_date": (tomorrow + timedelta(days=1)).isoformat(),
                },
                headers=INTERNAL,
            )
            assert third.json()["payable_credits_minor"] == returned["amount_minor"]
            await run_money_workers(state)
            assert await _balance(stack, payable) == 0
            assert await _balance(stack, f"merchant:{m}:payout_in_transit:INR") == 0
            await _integrity(stack)

            # Webhooks: every delivery signed and verifiable; failures retry; redelivery works.
            await run_money_workers(state)
            assert received, "no webhooks were delivered"
            for item in received:
                assert item["host"] == "webhooks.test"
                assert verify_signature(secret, item["signature"], item["body"])
            types = {json.loads(item["body"])["type"] for item in received}
            assert {
                "payment_intent.succeeded",
                "refund.succeeded",
                "settlement.posted",
                "payout.paid",
                "dispute.won",
            } <= types
            receiver.state.fail = True
            await _pay_upi(stack, 1_000, "wh-fail")
            await run_money_workers(state)
            failed = await stack.general_pool.fetchrow(
                "SELECT delivery_id, status, attempts FROM webhook_deliveries "
                "WHERE status = 'failed' LIMIT 1"
            )
            assert failed is not None and failed["attempts"] == 1
            receiver.state.fail = False
            redelivered = await stack.send(
                "POST",
                f"/v1/webhook_deliveries/{failed['delivery_id']}/redeliver",
                {},
                "redeliver-1",
            )
            assert redelivered.json()["status"] == "pending"
            await run_money_workers(state)
            status = await stack.general_pool.fetchval(
                "SELECT status FROM webhook_deliveries WHERE delivery_id = $1",
                failed["delivery_id"],
            )
            assert status == "succeeded"
            endpoint_id = endpoint.json()["endpoint_id"]
            tested = await stack.send(
                "POST", f"/v1/webhook_endpoints/{endpoint_id}/test", {}, "wh-test"
            )
            assert tested.json()["status"] == "succeeded"
            unpublished = await stack.general_pool.fetchval(
                "SELECT count(*) FROM core_outbox WHERE published_at IS NULL"
            )
            assert unpublished == 0
        finally:
            await stack.close()

    asyncio.run(exercise())


def test_money_operation_crashes_replay_to_a_single_ledger_effect(tmp_path: Path) -> None:
    if os.environ.get("TALLY_STACK_TESTS") != "1":
        pytest.skip("set TALLY_STACK_TESTS=1 with local Compose services to run stack tests")
    from chaos.flow_sim import CrashOnce, _call
    from services.core.money_ops import sweep_money_commands

    async def exercise() -> None:
        general, ledger, vault = await provision_databases("tally_it_money_crash")
        stack = await build_stack(
            general,
            ledger,
            vault,
            os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
            object_store=FilesystemObjectStore(tmp_path),
        )
        state = stack.core_app.state  # type: ignore[attr-defined]
        m = stack.merchant_id

        async def entries(key: str) -> int:
            return int(
                await stack.ledger_pool.fetchval(
                    "SELECT count(*) FROM ledger_journal_entries WHERE idempotency_key = $1", key
                )
            )

        try:
            payments = [await _pay_upi(stack, 50_000, f"c{i}") for i in range(4)]
            await _age(stack, payments, 3)
            for index, point in enumerate(
                ("refund.after_command_committed", "refund_debit.after_ledger_posted")
            ):
                state.fault_injector = CrashOnce({point})
                crashed = await _call(
                    stack.send(
                        "POST",
                        "/v1/refunds",
                        {"payment_id": payments[index], "amount_minor": 10_000},
                        f"crash-refund-{index}",
                    )
                )
                state.fault_injector = None
                assert crashed is None, point
                refund = await stack.general_pool.fetchrow(
                    "SELECT refund_id, status FROM refunds WHERE payment_id = $1",
                    UUID(payments[index]),
                )
                assert refund["status"] == "pending"
                # The next money operation for the merchant replays the pending command first.
                nxt = await stack.send(
                    "POST",
                    "/v1/refunds",
                    {"payment_id": payments[3], "amount_minor": 1_000},
                    f"after-crash-{index}",
                )
                assert nxt.status_code == 201, nxt.text
                replayed = await stack.general_pool.fetchval(
                    "SELECT status FROM refunds WHERE refund_id = $1", refund["refund_id"]
                )
                assert replayed == "processing"
                assert await entries(f"{refund['refund_id']}:refund:debit") == 1

            today = business_date(datetime.now(UTC))
            for offset, point in enumerate(
                ("settlement.after_command_committed", "settlement_post.after_ledger_posted")
            ):
                day = today + timedelta(days=offset)
                await _age(stack, [await _pay_upi(stack, 20_000, f"s{offset}")], 3)
                state.fault_injector = CrashOnce({point})
                result = await _call(
                    stack.client.post(
                        "/internal/v1/settlements/run",
                        json={"merchant_id": m, "business_date": day.isoformat()},
                        headers=INTERNAL,
                    )
                )
                state.fault_injector = None
                assert result is None, point
                await stack.general_pool.execute(
                    "UPDATE core_ledger_commands SET created_at = created_at - interval '1 hour'"
                )
                assert await sweep_money_commands(state.money_ctx) >= 1
                row = await stack.general_pool.fetchrow(
                    "SELECT status FROM settlements WHERE merchant_id = $1 AND business_date = $2",
                    m,
                    day,
                )
                assert row["status"] in ("posted", "empty")
                key = f"settlement:{m}:{day.isoformat()}:post"
                assert await entries(key) == (1 if row["status"] == "posted" else 0)
            await run_money_workers(state)
            assert await _balance(stack, f"merchant:{m}:payable:INR") == await _unsettled_sales(
                stack
            )
            await _integrity(stack)
        finally:
            await stack.close()

    asyncio.run(exercise())
