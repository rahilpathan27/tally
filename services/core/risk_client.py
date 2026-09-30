"""Synchronous risk check inside the payment flow, with a hard timeout and fail policy."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import httpx
from libs.observability.metrics import RISK_FAIL_POLICY

RISK_TIMEOUT_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class RiskOutcome:
    decision: str  # allow | step_up | review | block
    source: str  # risk | fail_open | fail_closed | disabled
    reason_codes: list[str] = field(default_factory=list)
    challenge_id: str | None = None
    review_case_id: str | None = None
    decision_id: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "source": self.source,
            "reason_codes": self.reason_codes,
            "challenge_id": self.challenge_id,
            "review_case_id": self.review_case_id,
            "decision_id": self.decision_id,
        }


async def fail_mode(pool: asyncpg.Pool, merchant_id: str) -> str:
    try:
        value = await pool.fetchval(
            "SELECT fail_mode FROM merchant_risk_policies WHERE merchant_id = $1", merchant_id
        )
    except asyncpg.UndefinedTableError:
        value = None
    return str(value or "open")


def decision_request(payment: asyncpg.Record) -> dict[str, Any]:
    context = payment["risk_context"]
    if isinstance(context, str):
        context = json.loads(context)
    is_card = payment["payment_method_type"] == "card"
    return {
        "payment_id": str(payment["payment_id"]),
        "merchant_id": payment["merchant_id"],
        "amount_minor": int(payment["amount_minor"]),
        "currency": "INR",
        "method": payment["payment_method_type"],
        "instrument_id": payment["payment_method_token"] if is_card else payment["payer_vpa"],
        "payee_id": payment["merchant_id"] if is_card else payment["payee_vpa"],
        **{key: value for key, value in (context or {}).items() if value is not None},
    }


async def assess_payment_risk(state: Any, payment: asyncpg.Record) -> RiskOutcome:
    """Ask the risk service; on timeout or error apply the merchant's fail-open/closed policy."""
    client: httpx.AsyncClient | None = getattr(state, "risk_http", None)
    if client is None:
        return RiskOutcome("allow", "disabled")
    try:
        response = await client.post(
            "/v1/decisions",
            json=decision_request(payment),
            headers={"x-internal-key": state.risk_key, "x-actor": "core"},
            timeout=getattr(state, "risk_timeout_seconds", RISK_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError):
        mode = await fail_mode(state.pool, str(payment["merchant_id"]))
        RISK_FAIL_POLICY.labels(mode).inc()
        if mode == "closed":
            return RiskOutcome("block", "fail_closed", ["RISK_UNAVAILABLE"])
        return RiskOutcome("allow", "fail_open", ["RISK_UNAVAILABLE"])
    return RiskOutcome(
        decision=str(body["decision"]),
        source="risk",
        reason_codes=[str(reason["code"]) for reason in body.get("reasons", [])],
        challenge_id=body.get("challenge_id"),
        review_case_id=body.get("review_case_id"),
        decision_id=body.get("decision_id"),
    )
