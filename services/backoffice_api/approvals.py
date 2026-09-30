"""Dual control for sensitive manual actions.

The maker proposes; a different user holding ``approver`` decides. The database enforces
``checker <> maker`` and one pending request per subject. Execution runs only after approval and
is idempotent (ledger adjustments use the request ID as their ledger key). Recon adjustments and
risk rule/model changes live in their own services; the inbox forwards those decisions with the
checker's identity so the owning service applies the same rule.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg
import httpx
from pydantic import BaseModel, ConfigDict, Field

from services.backoffice_api.auth import Principal

LOCAL_ACTIONS = ("ledger_adjustment", "limit_change", "refund_high_value", "merchant_risk_policy")
FORWARDED = {
    "recon_adjustment": "recon",
    "risk_rules": "risk",
    "model_promotion": "risk",
}


class ApprovalError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class LedgerAdjustment(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    postings: list[dict[str, Any]] = Field(min_length=2, max_length=20)
    reason: str = Field(min_length=5, max_length=500)


class LimitChange(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    merchant_id: str = Field(min_length=1, max_length=64)
    per_txn_max_minor: int | None = Field(default=None, gt=0, le=9_007_199_254_740_991)
    daily_max_minor: int | None = Field(default=None, gt=0, le=9_007_199_254_740_991)


class RiskPolicy(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    merchant_id: str = Field(min_length=1, max_length=64)
    tier: str = Field(pattern=r"^(standard|high_risk|enterprise)$")
    fail_mode: str = Field(pattern=r"^(open|closed)$")


async def propose(
    pool: asyncpg.Pool,
    principal: Principal,
    action_type: str,
    subject: str,
    payload: dict[str, Any],
) -> asyncpg.Record:
    if action_type not in LOCAL_ACTIONS:
        raise ApprovalError(422, "UNKNOWN_ACTION", "Unsupported action type.")
    async with pool.acquire() as connection, connection.transaction():
        try:
            row = await connection.fetchrow(
                """INSERT INTO maker_checker_requests(
                       request_id, action_type, subject_id, payload, maker, status
                   ) VALUES ($1, $2, $3, $4::jsonb, $5, 'pending') RETURNING *""",
                uuid.uuid4(),
                action_type,
                subject,
                json.dumps(payload),
                principal.email,
            )
        except asyncpg.UniqueViolationError as exc:
            raise ApprovalError(409, "PROPOSAL_PENDING", "A request is already pending.") from exc
        await connection.execute(
            "SELECT audit_append($1, 'approval_proposed', $2, $3, $4::jsonb)",
            principal.email,
            f"{action_type}:{subject}",
            payload.get("merchant_id"),
            json.dumps({"request_id": str(row["request_id"]), **payload}, default=str),
        )
    assert row is not None
    return row


async def _execute(
    state: Any, request: asyncpg.Record, payload: dict[str, Any], checker: str
) -> dict[str, Any]:
    action = request["action_type"]
    pool: asyncpg.Pool = state.pool
    if action == "ledger_adjustment":
        response = await state.ledger_http.post(
            "/v1/entries",
            json={
                "idempotency_key": f"manual-adjust:{request['request_id']}",
                "postings": payload["postings"],
            },
        )
        if response.status_code >= 400:
            raise ApprovalError(422, "LEDGER_REJECTED", response.text[:300])
        return {"entry_id": response.json()["entry_id"]}
    if action == "limit_change":
        await pool.execute(
            """INSERT INTO merchant_limits(merchant_id, per_txn_max_minor, daily_max_minor,
                                          updated_by)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (merchant_id) DO UPDATE SET
                   per_txn_max_minor = EXCLUDED.per_txn_max_minor,
                   daily_max_minor = EXCLUDED.daily_max_minor,
                   updated_by = EXCLUDED.updated_by, updated_at = clock_timestamp()""",
            payload["merchant_id"],
            payload.get("per_txn_max_minor"),
            payload.get("daily_max_minor"),
            checker,
        )
        return {"merchant_id": payload["merchant_id"]}
    if action == "merchant_risk_policy":
        await pool.execute(
            """INSERT INTO merchant_risk_policies(merchant_id, tier, fail_mode)
               VALUES ($1, $2, $3)
               ON CONFLICT (merchant_id) DO UPDATE SET tier = EXCLUDED.tier,
                   fail_mode = EXCLUDED.fail_mode, updated_at = clock_timestamp()""",
            payload["merchant_id"],
            payload["tier"],
            payload["fail_mode"],
        )
        return {"merchant_id": payload["merchant_id"]}
    if action == "refund_high_value":
        response = await state.core_http.post(
            "/internal/v1/refunds",
            json={
                "merchant_id": payload["merchant_id"],
                "payment_id": payload["payment_id"],
                "amount_minor": payload["amount_minor"],
                "reason": payload.get("reason"),
            },
            headers={"x-internal-key": state.core_key, "x-actor": f"{request['maker']}+{checker}"},
        )
        if response.status_code >= 400:
            raise ApprovalError(response.status_code, "REFUND_REJECTED", response.text[:300])
        return {"refund_id": response.json()["refund_id"], "status": response.json()["status"]}
    raise ApprovalError(422, "UNKNOWN_ACTION", action)


async def decide(
    state: Any, request_id: uuid.UUID, checker: Principal, approve: bool, reason: str
) -> dict[str, Any]:
    pool: asyncpg.Pool = state.pool
    request = await pool.fetchrow(
        "SELECT * FROM maker_checker_requests WHERE request_id = $1", request_id
    )
    if request is None:
        raise ApprovalError(404, "REQUEST_NOT_FOUND", "Approval request was not found.")
    if request["maker"] == checker.email:
        await pool.execute(
            "SELECT audit_append($1, 'approval_self_decision_blocked', $2, NULL, '{}'::jsonb)",
            checker.email,
            str(request_id),
        )
        raise ApprovalError(403, "MAKER_CANNOT_APPROVE", "The maker cannot decide their request.")
    owner = FORWARDED.get(request["action_type"])
    if owner is not None:
        client: httpx.AsyncClient = getattr(state, f"{owner}_http")
        verb = "approve" if approve else "reject"
        response = await client.post(
            f"/v1/approvals/{request_id}/{verb}",
            json={"reason": reason},
            headers={"x-internal-key": getattr(state, f"{owner}_key"), "x-actor": checker.email},
        )
        if response.status_code >= 400:
            detail = response.json().get("detail", {})
            raise ApprovalError(
                response.status_code, detail.get("code", "FORWARD_FAILED"), str(detail)
            )
        await pool.execute(
            "SELECT audit_append($1, $2, $3, NULL, $4::jsonb)",
            checker.email,
            f"approval_{verb}d",
            str(request_id),
            json.dumps({"action_type": request["action_type"], "reason": reason}),
        )
        return dict(response.json())
    if request["status"] != "pending":
        raise ApprovalError(409, "REQUEST_DECIDED", f"Request is {request['status']}.")
    payload = request["payload"]
    payload = json.loads(payload) if isinstance(payload, str) else payload
    if not approve:
        await pool.execute(
            """UPDATE maker_checker_requests SET status = 'rejected', checker = $2,
                   decision_reason = $3, decided_at = clock_timestamp()
               WHERE request_id = $1 AND status = 'pending'""",
            request_id,
            checker.email,
            reason,
        )
        await pool.execute(
            "SELECT audit_append($1, 'approval_rejected', $2, NULL, $3::jsonb)",
            checker.email,
            str(request_id),
            json.dumps({"reason": reason}),
        )
        return {"request_id": str(request_id), "status": "rejected"}
    claimed = await pool.fetchval(
        """UPDATE maker_checker_requests SET status = 'approved', checker = $2,
               decision_reason = $3, decided_at = clock_timestamp()
           WHERE request_id = $1 AND status = 'pending' RETURNING true""",
        request_id,
        checker.email,
        reason,
    )
    if not claimed:
        raise ApprovalError(409, "REQUEST_DECIDED", "Request was decided concurrently.")
    try:
        result = await _execute(state, request, payload, checker.email)
        status = "executed"
    except ApprovalError as exc:
        result = {"error": exc.code, "message": exc.message}
        status = "failed"
    await pool.execute(
        "UPDATE maker_checker_requests SET status = $2, result = $3::jsonb WHERE request_id = $1",
        request_id,
        status,
        json.dumps(result, default=str),
    )
    await pool.execute(
        "SELECT audit_append($1, $2, $3, $4, $5::jsonb)",
        checker.email,
        f"approval_{status}",
        f"{request['action_type']}:{request['subject_id']}",
        payload.get("merchant_id"),
        json.dumps(
            {"request_id": str(request_id), "maker": request["maker"], **result}, default=str
        ),
    )
    return {"request_id": str(request_id), "status": status, "result": result}
