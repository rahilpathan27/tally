"""Phase 10: real-time risk decisions inside the payment flow."""

from __future__ import annotations

import asyncio
import copy
import os
import statistics
from typing import Any
from uuid import UUID

import httpx
import pytest
from chaos.stack import LocalStack, build_stack, provision_databases
from services.risk.rules import DEFAULT_RULES

RISK = {"x-internal-key": "stack-risk-key", "x-actor": "alice"}
BOB = {**RISK, "x-actor": "bob"}


async def _upi(
    stack: LocalStack,
    amount: int,
    tag: str,
    context: dict[str, Any] | None = None,
    payer: str = "payer@bank-a",
) -> tuple[str, httpx.Response]:
    body: dict[str, Any] = {
        "amount_minor": amount,
        "currency": "INR",
        "payment_method_type": "upi",
        "payer_vpa": payer,
        "payee_vpa": "merchant@bank-b",
    }
    if context:
        body["risk_context"] = context
    created = await stack.send("POST", "/v1/payment_intents", body, f"{tag}-c")
    assert created.status_code == 201, created.text
    payment_id = str(created.json()["payment_id"])
    confirmed = await stack.send(
        "POST", f"/v1/payment_intents/{payment_id}/confirm", {}, f"{tag}-f"
    )
    return payment_id, confirmed


async def _card(
    stack: LocalStack, token: str, amount: int, tag: str, context: dict[str, Any]
) -> tuple[str, httpx.Response]:
    created = await stack.send(
        "POST",
        "/v1/payment_intents",
        {
            "amount_minor": amount,
            "currency": "INR",
            "payment_method_type": "card",
            "payment_method_token": token,
            "risk_context": context,
        },
        f"{tag}-c",
    )
    assert created.status_code == 201, created.text
    payment_id = str(created.json()["payment_id"])
    confirmed = await stack.send(
        "POST", f"/v1/payment_intents/{payment_id}/confirm", {}, f"{tag}-f"
    )
    return payment_id, confirmed


async def _status(stack: LocalStack, payment_id: str) -> str:
    return str(
        await stack.general_pool.fetchval(
            "SELECT status FROM payment_intents WHERE payment_id = $1", UUID(payment_id)
        )
    )


def test_risk_decisions_drive_the_payment_flow() -> None:
    if os.environ.get("TALLY_STACK_TESTS") != "1":
        pytest.skip("set TALLY_STACK_TESTS=1 with local Compose services to run stack tests")

    async def exercise() -> None:
        general, ledger, vault = await provision_databases("tally_it_risk")
        stack = await build_stack(
            general,
            ledger,
            vault,
            os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
            risk=True,
        )
        state = stack.core_app.state  # type: ignore[attr-defined]
        risk = state.risk_http
        try:
            home = {"device_id": "phone-1", "ip_address": "49.1.2.3", "ip_country": "IN"}
            payment_id, allowed = await _upi(stack, 25_000, "ok", home)
            assert allowed.json()["status"] == "succeeded", allowed.text
            decision = (
                await risk.get("/v1/decisions", params={"payment_id": payment_id}, headers=RISK)
            ).json()[0]
            assert decision["decision"] == "allow" and decision["model_version"]
            assert len(decision["features"]) == 19

            # Card testing: one device tries many cards; rule R002 blocks from the sixth card.
            vault_repo = stack.vault_app.state.vault_repository  # type: ignore[attr-defined]
            outcomes = []
            for i in range(7):
                token = (await vault_repo.tokenize("4242424242424242", 12, 2099)).token
                pid, response = await _card(
                    stack, token, 150, f"ct{i}", {"device_id": "fraud-rig", "ip_country": "IN"}
                )
                outcomes.append(response.json())
            # The model may challenge or block the burst early; the first card is never blocked
            # and the rule fires once five distinct cards have been seen on the device.
            assert outcomes[0]["status"] in {"authorized", "risk_review"}, outcomes[0]
            print("card-testing outcomes:", [(o["status"], o["reason_codes"]) for o in outcomes])
            assert outcomes[5]["status"] == "failed"
            assert "CARD_TESTING" in outcomes[5]["reason_codes"]

            # Rule change under maker-checker: alice proposes, alice cannot approve, bob can.
            rules = copy.deepcopy(DEFAULT_RULES)
            rules["rules"].insert(
                0,
                {
                    "rule_id": "R900",
                    "description": "Very large UPI transfer",
                    "reason_code": "LARGE_TRANSFER",
                    "action": "review",
                    "condition": {
                        "all": [
                            {"fact": "amount_minor", "op": ">=", "value": 9_000_000},
                            {"fact": "is_card", "op": "==", "value": 0},
                        ]
                    },
                },
            )
            rules["rules"].insert(
                0,
                {
                    "rule_id": "R901",
                    "description": "Card used from another country",
                    "reason_code": "CARD_GEO_MISMATCH",
                    "action": "step_up",
                    "condition": {
                        "all": [
                            {"fact": "geo_mismatch", "op": "==", "value": 1},
                            {"fact": "is_card", "op": "==", "value": 1},
                        ]
                    },
                },
            )
            bad = await risk.post(
                "/v1/rules/proposals", json={"definition": {"rules": [{}]}}, headers=RISK
            )
            assert bad.status_code == 422
            proposal = await risk.post(
                "/v1/rules/proposals", json={"definition": rules}, headers=RISK
            )
            assert proposal.status_code == 201, proposal.text
            request_id = proposal.json()["request_id"]
            assert (
                await risk.post(
                    f"/v1/approvals/{request_id}/approve", json={"reason": "self"}, headers=RISK
                )
            ).status_code == 403
            approved = await risk.post(
                f"/v1/approvals/{request_id}/approve", json={"reason": "reviewed"}, headers=BOB
            )
            assert approved.json()["result"]["rule_version"] == 2
            assert (await risk.get("/v1/rules", headers=RISK)).json()["version"] == 2

            # Review: payment waits; a second confirm cannot bypass; analyst approval resumes it.
            review_id, review = await _upi(stack, 9_500_000, "big", home)
            assert review.json()["status"] == "risk_review"
            assert review.json()["next_action"] == "await_review"
            bypass = await stack.send(
                "POST", f"/v1/payment_intents/{review_id}/confirm", {}, "big-f2"
            )
            assert bypass.status_code == 409
            case = next(
                c
                for c in (await risk.get("/v1/reviews", headers=RISK)).json()
                if c["payment_id"] == review_id
            )
            assert "LARGE_TRANSFER" in {r["code"] for r in case["reason_codes"]}
            resolved = await risk.post(
                f"/v1/reviews/{case['case_id']}/resolve",
                json={"outcome": "approve", "note": "customer confirmed by phone"},
                headers=RISK,
            )
            assert resolved.json()["status"] == "approved"
            assert await _status(stack, review_id) == "succeeded"
            # Back-to-back large transfers from one payer are blocked outright by the model.
            _, rapid = await _upi(stack, 9_600_000, "big-rapid", home)
            assert (
                rapid.json()["status"] == "failed"
                and "LARGE_TRANSFER" in rapid.json()["reason_codes"]
            )
            await stack.general_pool.execute(
                "INSERT INTO core_vpas(vpa, bank_id) VALUES ('payer2@bank-a', 'bank-a')"
            )
            phone2 = {**home, "device_id": "phone-2"}
            _, warm = await _upi(stack, 25_000, "warm2", phone2, "payer2@bank-a")
            assert warm.json()["status"] == "succeeded", warm.text
            declined_id, second = await _upi(stack, 9_600_000, "big2", phone2, "payer2@bank-a")
            assert second.json()["status"] == "risk_review", second.text
            case2 = next(
                c
                for c in (await risk.get("/v1/reviews", headers=RISK)).json()
                if c["payment_id"] == declined_id
            )
            await risk.post(
                f"/v1/reviews/{case2['case_id']}/resolve",
                json={"outcome": "decline", "note": "mule pattern"},
                headers=RISK,
            )
            assert await _status(stack, declined_id) == "failed"
            label = await stack.general_pool.fetchval(
                "SELECT label FROM risk_labels WHERE payment_id = $1", UUID(declined_id)
            )
            assert label == "fraud"

            # Step-up: wrong code rejected, right code authorizes.
            token = (await vault_repo.tokenize("4242424242424242", 12, 2099)).token
            stepped_id, stepped = await _card(
                stack, token, 80_000, "su", {"device_id": "phone-9", "ip_country": "SG"}
            )
            assert stepped.json()["next_action"] == "step_up", stepped.text
            challenge = stepped.json()["challenge_id"]
            wrong = await stack.send(
                "POST",
                f"/v1/payment_intents/{stepped_id}/step_up",
                {"challenge_id": challenge, "code": "000000"},
                "su-wrong",
            )
            assert wrong.status_code == 422
            code = (await risk.get(f"/internal/v1/step_up/{challenge}/code", headers=RISK)).json()
            ok = await stack.send(
                "POST",
                f"/v1/payment_intents/{stepped_id}/step_up",
                {"challenge_id": challenge, "code": code["code"]},
                "su-right",
            )
            assert ok.json()["status"] == "authorized", ok.text

            # Risk outage: fail-open allows, fail-closed blocks (per merchant policy).
            class Down(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                    raise httpx.ConnectTimeout("risk unavailable")

            state.risk_http = httpx.AsyncClient(transport=Down(), base_url="http://risk")
            _, open_result = await _upi(stack, 12_000, "fo", home)
            assert open_result.json()["status"] == "succeeded"
            await stack.general_pool.execute(
                "INSERT INTO merchant_risk_policies(merchant_id, tier, fail_mode) "
                "VALUES ($1, 'high_risk', 'closed')",
                stack.merchant_id,
            )
            _, closed_result = await _upi(stack, 12_000, "fc", home)
            assert closed_result.json()["status"] == "failed"
            assert closed_result.json()["reason_codes"] == ["RISK_UNAVAILABLE"]
            state.risk_http = risk

            # Latency of the decision path (features + rules + model + SHAP + logging).
            await stack.general_pool.execute(
                "DELETE FROM merchant_risk_policies WHERE merchant_id = $1", stack.merchant_id
            )
            for i in range(60):
                await _upi(stack, 1_000 + i, f"lat{i}", home)
            latencies = [
                row["latency_us"]
                for row in await stack.general_pool.fetch(
                    "SELECT latency_us FROM risk_decisions ORDER BY created_at DESC LIMIT 60"
                )
            ]
            p99 = statistics.quantiles(latencies, n=100)[98]
            print(f"risk decision latency us: p50={statistics.median(latencies)} p99={p99}")
            assert p99 < 50_000

            # Shadow challenger: scored and logged, never changes the outcome.
            models = (await risk.get("/v1/models", headers=RISK)).json()
            champion = next(m for m in models if m["stage"] == "champion")["version"]
            await stack.general_pool.execute(
                """INSERT INTO risk_model_versions(version, stage, artifact_path, artifact_sha256,
                       metrics, registered_by)
                   SELECT 'shadow-copy', 'registered', artifact_path, artifact_sha256, metrics,
                          'test' FROM risk_model_versions WHERE version = $1""",
                champion,
            )
            promoted = await risk.post("/v1/models/shadow-copy/challenger", headers=RISK)
            assert promoted.json()["stage"] == "challenger", promoted.text
            shadow_id, _ = await _upi(stack, 3_333, "shadow", home)
            shadow = (
                await risk.get("/v1/decisions", params={"payment_id": shadow_id}, headers=RISK)
            ).json()[0]
            assert shadow["challenger_version"] == "shadow-copy"
            assert shadow["challenger_score"] == pytest.approx(shadow["model_score"])

            drift = await risk.post("/v1/drift/run", params={"sample": 50}, headers=RISK)
            assert drift.status_code == 200 and drift.json()["sample_size"] >= 50
            perf = (await risk.get("/v1/model-performance", headers=RISK)).json()
            assert perf["labelled_count"] >= 1
        finally:
            await stack.close()

    asyncio.run(exercise())
