"""Race-condition abuse: capture/cancel races, confirm storms, cross-direction ledger postings."""

from __future__ import annotations

import asyncio
import os
from uuid import UUID

import httpx
import pytest
from chaos.stack import LocalStack, build_stack, provision_databases


async def _authorized_card(stack: LocalStack, amount: int, tag: str) -> str:
    created = await stack.send(
        "POST",
        "/v1/payment_intents",
        {
            "amount_minor": amount,
            "currency": "INR",
            "payment_method_type": "card",
            "payment_method_token": stack.card_token,
        },
        f"{tag}-c",
    )
    payment_id = str(created.json()["payment_id"])
    confirmed = await stack.send(
        "POST", f"/v1/payment_intents/{payment_id}/confirm", {}, f"{tag}-f"
    )
    assert confirmed.json()["status"] == "authorized", confirmed.text
    return payment_id


def test_races_cannot_double_spend_or_deadlock() -> None:
    if os.environ.get("TALLY_STACK_TESTS") != "1":
        pytest.skip("set TALLY_STACK_TESTS=1 with local Compose services to run stack tests")

    async def exercise() -> None:
        general, ledger, vault = await provision_databases("tally_it_races")
        stack = await build_stack(
            general, ledger, vault, os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        )
        try:
            # Simultaneous capture and cancel: exactly one wins, and the hold matches it.
            payments = [await _authorized_card(stack, 1_000 + i, f"cc{i}") for i in range(15)]
            await asyncio.gather(
                *(
                    coroutine
                    for i, pid in enumerate(payments)
                    for coroutine in (
                        stack.send("POST", f"/v1/payment_intents/{pid}/capture", {}, f"cap{i}"),
                        stack.send("POST", f"/v1/payment_intents/{pid}/cancel", {}, f"can{i}"),
                    )
                )
            )
            for pid in payments:
                status = await stack.general_pool.fetchval(
                    "SELECT status FROM payment_intents WHERE payment_id = $1", UUID(pid)
                )
                hold = await stack.ledger_pool.fetchval(
                    "SELECT status::text FROM ledger_holds WHERE idempotency_key = $1",
                    f"{pid}:card:authorization:hold",
                )
                assert (status, hold) in {("succeeded", "posted"), ("cancelled", "void")}, (
                    pid,
                    status,
                    hold,
                )
                terminal = await stack.general_pool.fetchval(
                    """SELECT count(*) FROM payment_transitions WHERE payment_id = $1
                       AND accepted AND to_state IN ('succeeded', 'cancelled')""",
                    UUID(pid),
                )
                assert terminal == 1

            # Confirm storm with distinct idempotency keys: one authorization, one ledger hold.
            created = await stack.send(
                "POST",
                "/v1/payment_intents",
                {
                    "amount_minor": 7_777,
                    "currency": "INR",
                    "payment_method_type": "upi",
                    "payer_vpa": "payer@bank-a",
                    "payee_vpa": "merchant@bank-b",
                },
                "storm-c",
            )
            pid = created.json()["payment_id"]
            responses = await asyncio.gather(
                *(
                    stack.send("POST", f"/v1/payment_intents/{pid}/confirm", {}, f"storm-{i}")
                    for i in range(12)
                )
            )
            assert sorted(r.status_code for r in responses).count(200) == 1
            assert (
                await stack.ledger_pool.fetchval(
                    "SELECT count(*) FROM ledger_journal_entries WHERE idempotency_key LIKE $1",
                    f"{pid}:%",
                )
                == 1
            )

            # Opposite-direction postings between the same accounts must not deadlock
            # (accounts are locked in one global order) and must conserve money.
            ledger_http: httpx.AsyncClient = stack.core_app.state.ledger_http  # type: ignore[attr-defined]
            accounts = ("platform:suspense:INR", "platform:writeoff:INR")

            async def post(i: int) -> int:
                first, second = accounts if i % 2 else accounts[::-1]
                response = await ledger_http.post(
                    "/v1/entries",
                    json={
                        "idempotency_key": f"contention-{i}",
                        "postings": [
                            {"account_id": first, "direction": "debit", "amount_minor": 10},
                            {"account_id": second, "direction": "credit", "amount_minor": 10},
                        ],
                    },
                )
                return response.status_code

            statuses = await asyncio.wait_for(
                asyncio.gather(*(post(i) for i in range(200))), timeout=60
            )
            assert set(statuses) == {201}
            integrity = await stack.ledger_pool.fetch("SELECT ok FROM ledger_verify_integrity()")
            assert all(row["ok"] for row in integrity)
        finally:
            await stack.close()

    asyncio.run(exercise())
