"""Phase 11 security suite: auth, authorization matrix, tenant isolation, dual control, audit."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from chaos.stack import Backoffice, LocalStack, build_backoffice, build_stack, provision_databases
from fastapi.routing import APIRoute
from libs.common.object_store import FilesystemObjectStore
from libs.security import totp
from services.backoffice_api.rbac import PERMISSION_ATTRIBUTE, PERMISSIONS
from services.recon.api import create_app as create_recon_app
from services.recon.engine import ReconConfig
from services.recon.service import ReconContext
from services.workers import aml
from services.workers.audit_anchor import anchor, verify_anchors

ROLES = [
    "merchant_admin",
    "merchant_developer",
    "merchant_viewer",
    "ops_analyst",
    "risk_analyst",
    "approver",
    "operator",
]
MERCHANT_ROLES = {"merchant_admin", "merchant_developer", "merchant_viewer"}


def _all_routes(routes: list[Any]) -> list[APIRoute]:
    """Flatten routes, including FastAPI's wrappers around included routers."""
    found: list[APIRoute] = []
    for route in routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            found.extend(_all_routes(list(nested.routes)))
        elif isinstance(route, APIRoute):
            found.append(route)
    return found


def _permission(route: APIRoute) -> str | None:
    stack = list(route.dependant.dependencies)
    while stack:
        dependency = stack.pop()
        permission = getattr(dependency.call, PERMISSION_ATTRIBUTE, None)
        if permission:
            return str(permission)
        stack.extend(dependency.dependencies)
    return None


def _concrete(path: str) -> str:
    path = path.replace("{path:path}", "accounts")
    path = re.sub(r"\{verb\}", "approve", path)
    path = re.sub(r"\{version\}", "fraud-gbm-v1", path)
    path = re.sub(r"\{key_id\}", "tly_test_missing", path)
    return re.sub(r"\{[a-z_]+\}", str(uuid.uuid4()), path)


async def _upi(stack: LocalStack, amount: int, tag: str, payer: str = "payer@bank-a") -> str:
    created = await stack.send(
        "POST",
        "/v1/payment_intents",
        {
            "amount_minor": amount,
            "currency": "INR",
            "payment_method_type": "upi",
            "payer_vpa": payer,
            "payee_vpa": "merchant@bank-b",
        },
        f"{tag}-c",
    )
    assert created.status_code == 201, created.text
    payment_id = str(created.json()["payment_id"])
    await stack.send("POST", f"/v1/payment_intents/{payment_id}/confirm", {}, f"{tag}-f")
    return payment_id


def test_backoffice_security(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get("TALLY_STACK_TESTS") != "1":
        pytest.skip("set TALLY_STACK_TESTS=1 with local Compose services to run stack tests")
    monkeypatch.setenv("TALLY_SIMULATOR_ADMIN_KEY", "stack-sim-admin")

    async def exercise() -> None:
        general, ledger, vault = await provision_databases("tally_it_security")
        store = FilesystemObjectStore(tmp_path)
        stack = await build_stack(
            general,
            ledger,
            vault,
            os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
            object_store=store,
            risk=True,
        )
        state = stack.core_app.state  # type: ignore[attr-defined]
        recon_app = create_recon_app(
            ReconContext(stack.general_pool, state.ledger_http, store, ReconConfig()),
            internal_key="recon-key",
        )
        bo = await build_backoffice(stack, recon_app=recon_app, chaos_enabled=True)
        bo.app.state.sse_max_events = 1
        bo.app.state.sse_interval_seconds = 0
        try:
            await _auth_flows(stack, bo)
            await _authorization_matrix(stack, bo)
            # Later sections need deterministic payment outcomes; risk has its own suite.
            state.risk_http = None
            await _tenant_isolation(stack, bo)
            await _dual_control(stack, bo)
            await _aml(stack, bo)
            await _audit_chain(stack, bo, store)
        finally:
            await stack.close()

    asyncio.run(exercise())


async def _auth_flows(stack: LocalStack, bo: Backoffice) -> None:
    secret = await bo.user("mfa.user@tally.test", ["ops_analyst"], mfa=True)
    assert secret is not None
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=bo.app), base_url="https://console.test"
    )
    wrong = await client.post("/auth/login", json={"email": "nobody@tally.test", "password": "x"})
    assert wrong.status_code == 401
    step = await client.post(
        "/auth/login",
        json={"email": "mfa.user@tally.test", "password": "correct horse battery staple"},
    )
    body = step.json()
    assert body["mfa_required"] and "tally_access" not in step.cookies
    # The MFA token is not a session.
    denied = await client.get(
        "/bff/v1/ops/switch", headers={"authorization": f"Bearer {body['mfa_token']}"}
    )
    assert denied.status_code == 401
    bad = await client.post("/auth/mfa", json={"mfa_token": body["mfa_token"], "code": "000000"})
    assert bad.status_code == 401
    code = totp.totp(secret)
    ok = await client.post("/auth/mfa", json={"mfa_token": body["mfa_token"], "code": code})
    assert ok.status_code == 200, ok.text
    access_cookie = ok.headers.get_list("set-cookie")
    assert any("HttpOnly" in c and "Secure" in c and "SameSite=strict" in c for c in access_cookie)
    replay = await client.post("/auth/mfa", json={"mfa_token": body["mfa_token"], "code": code})
    assert replay.status_code == 401  # a TOTP code is single-use
    client.headers["x-csrf-token"] = ok.json()["csrf_token"]
    assert (await client.get("/bff/v1/ops/switch")).status_code == 200
    # CSRF: a cookie-authenticated mutation without the matching header is refused.
    no_csrf = await client.post("/bff/v1/ops/aml/run", headers={"x-csrf-token": "forged"})
    assert no_csrf.status_code == 403
    # Refresh rotation and reuse detection.
    first_refresh = client.cookies.get("tally_refresh")
    rotated = await client.post("/auth/refresh")
    assert rotated.status_code == 200 and client.cookies.get("tally_refresh") != first_refresh
    thief = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=bo.app), base_url="https://console.test"
    )
    thief.cookies.set("tally_refresh", first_refresh or "", path="/auth")
    reused = await thief.post("/auth/refresh")
    assert reused.status_code == 401
    after = await client.post("/auth/refresh")
    assert after.status_code == 401  # the whole family was revoked
    # Lockout after repeated failures.
    await bo.user("lock.user@tally.test", ["operator"])
    for _ in range(5):
        await client.post("/auth/login", json={"email": "lock.user@tally.test", "password": "nope"})
    locked = await client.post(
        "/auth/login",
        json={"email": "lock.user@tally.test", "password": "correct horse battery staple"},
    )
    assert locked.status_code == 401 and "locked" in locked.text
    for c in (client, thief):
        await c.aclose()


async def _authorization_matrix(stack: LocalStack, bo: Backoffice) -> None:
    routes = [r for r in _all_routes(list(bo.app.routes)) if r.path.startswith("/bff/")]
    assert len(routes) >= 30
    missing = [r.path for r in routes if _permission(r) is None]
    assert missing == [], f"routes without a permission (deny-by-default violated): {missing}"
    anonymous = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=bo.app), base_url="https://console.test"
    )
    clients: dict[str, httpx.AsyncClient] = {}
    for role in ROLES:
        email = f"matrix.{role}@tally.test"
        await bo.user(email, [role], stack.merchant_id if role in MERCHANT_ROLES else None)
        clients[role] = await bo.login(email)
    checked = 0
    for route in routes:
        permission = _permission(route)
        assert permission is not None
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            path = _concrete(route.path)
            kwargs: dict[str, Any] = {"json": {}} if method == "POST" else {}
            unauthenticated = await anonymous.request(method, path, **kwargs)
            assert unauthenticated.status_code == 401, (method, path)
            for role, client in clients.items():
                response = await client.request(method, path, **kwargs)
                expected = role in PERMISSIONS[permission]
                if expected:
                    assert response.status_code not in (401, 403), (
                        role,
                        method,
                        path,
                        response.text,
                    )
                else:
                    assert response.status_code == 403, (role, method, path, response.status_code)
                checked += 1
    assert checked >= len(ROLES) * 30
    for client in (anonymous, *clients.values()):
        await client.aclose()


async def _tenant_isolation(stack: LocalStack, bo: Backoffice) -> None:
    own = await _upi(stack, 12_345, "iso-a")
    other_payment = uuid.uuid4()
    pool = stack.general_pool
    await pool.execute("INSERT INTO merchants(merchant_id, display_name) VALUES ('rival', 'Rival')")
    await pool.execute(
        """INSERT INTO payment_intents(payment_id, merchant_id, amount_minor, currency,
               payment_method_type, payer_vpa, payee_vpa, status, mode)
           VALUES ($1, 'rival', 999, 'INR', 'upi', 'payer@bank-a', 'merchant@bank-b',
                   'succeeded', 'test')""",
        other_payment,
    )
    await pool.execute(
        """INSERT INTO merchant_api_keys(key_id, merchant_id, secret_ciphertext, scopes, mode)
           VALUES ('rival-key', 'rival', '\\x00', ARRAY['payments:read'], 'test')"""
    )
    await bo.user("iso.viewer@tally.test", ["merchant_admin"], stack.merchant_id)
    client = await bo.login("iso.viewer@tally.test")
    listed = (await client.get("/bff/v1/payments", params={"limit": 200})).json()["items"]
    ids = {item["payment_id"] for item in listed}
    assert own in ids and str(other_payment) not in ids
    assert (await client.get(f"/bff/v1/payments/{other_payment}")).status_code == 404
    searched = await client.get("/bff/v1/payments", params={"q": str(other_payment)})
    assert searched.json()["items"] == []
    keys = (await client.get("/bff/v1/api-keys")).json()
    assert "rival-key" not in {k["key_id"] for k in keys}
    assert all("secret_ciphertext" not in k for k in keys)
    revoke = await client.post("/bff/v1/api-keys/rival-key/revoke")
    assert revoke.status_code == 404
    await client.aclose()

    # Database-level attacks as the application role.
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute("SET LOCAL ROLE tally_app")
            await connection.execute(
                "SELECT set_config('app.merchant_id', $1, true)", stack.merchant_id
            )
            rival_rows = await connection.fetchval(
                "SELECT count(*) FROM payment_intents WHERE merchant_id = 'rival'"
            )
            assert rival_rows == 0
            assert await connection.fetchval("SELECT count(*) FROM payment_intents") >= 1
        async with connection.transaction():
            await connection.execute("SET LOCAL ROLE tally_app")
            # No tenant context set: nothing is visible.
            assert await connection.fetchval("SELECT count(*) FROM payment_intents") == 0
        for statement in (
            "SELECT secret_ciphertext FROM merchant_api_keys",
            "SELECT secret_ciphertext FROM webhook_endpoints",
            "UPDATE payment_intents SET status = 'succeeded'",
            "DELETE FROM refunds",
            "INSERT INTO merchants(merchant_id, display_name) VALUES ('x', 'x')",
            "SELECT * FROM backoffice_users",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with connection.transaction():
                    await connection.execute("SET LOCAL ROLE tally_app")
                    await connection.execute(statement)


async def _dual_control(stack: LocalStack, bo: Backoffice) -> None:
    await bo.user("maker.ops@tally.test", ["operator"])
    await bo.user("dual.person@tally.test", ["ops_analyst", "approver"])
    await bo.user("checker@tally.test", ["approver"])
    await bo.user("viewer.ops@tally.test", ["ops_analyst"])
    maker = await bo.login("maker.ops@tally.test")
    dual = await bo.login("dual.person@tally.test")
    checker = await bo.login("checker@tally.test")
    viewer = await bo.login("viewer.ops@tally.test")

    proposal = await maker.post(
        "/bff/v1/ops/limits", json={"merchant_id": stack.merchant_id, "per_txn_max_minor": 50_000}
    )
    assert proposal.status_code == 201, proposal.text
    request_id = proposal.json()["request_id"]
    duplicate = await maker.post(
        "/bff/v1/ops/limits", json={"merchant_id": stack.merchant_id, "per_txn_max_minor": 1}
    )
    assert duplicate.status_code == 409
    assert (
        await viewer.post(f"/bff/v1/ops/approvals/{request_id}/approve", json={"reason": "x"})
    ).status_code == 403
    approved = await checker.post(
        f"/bff/v1/ops/approvals/{request_id}/approve", json={"reason": "risk review done"}
    )
    assert approved.json()["status"] == "executed", approved.text
    again = await checker.post(
        f"/bff/v1/ops/approvals/{request_id}/approve", json={"reason": "again"}
    )
    assert again.status_code == 409
    blocked = await stack.send(
        "POST",
        "/v1/payment_intents",
        {
            "amount_minor": 60_000,
            "currency": "INR",
            "payment_method_type": "upi",
            "payer_vpa": "payer@bank-a",
            "payee_vpa": "merchant@bank-b",
        },
        "limit-blocked",
    )
    assert (
        blocked.status_code == 422 and blocked.json()["detail"]["code"] == "MERCHANT_LIMIT_EXCEEDED"
    )

    # A person holding both roles still cannot approve their own manual ledger adjustment.
    adjustment = await dual.post(
        "/bff/v1/ops/ledger/adjustments",
        json={
            "reason": "correct suspense misposting",
            "postings": [
                {"account_id": "platform:suspense:INR", "direction": "debit", "amount_minor": 100},
                {"account_id": "platform:writeoff:INR", "direction": "credit", "amount_minor": 100},
            ],
        },
    )
    assert adjustment.status_code == 201, adjustment.text
    adjust_id = adjustment.json()["request_id"]
    own = await dual.post(f"/bff/v1/ops/approvals/{adjust_id}/approve", json={"reason": "mine"})
    assert own.status_code == 403 and own.json()["detail"]["code"] == "MAKER_CANNOT_APPROVE"
    unbalanced = await dual.post(
        "/bff/v1/ops/ledger/adjustments",
        json={
            "reason": "unbalanced attempt",
            "postings": [
                {"account_id": "platform:suspense:INR", "direction": "debit", "amount_minor": 100},
                {"account_id": "platform:writeoff:INR", "direction": "credit", "amount_minor": 99},
            ],
        },
    )
    assert unbalanced.status_code == 422
    executed = await checker.post(
        f"/bff/v1/ops/approvals/{adjust_id}/approve", json={"reason": "matches ticket"}
    )
    assert executed.json()["status"] == "executed" and executed.json()["result"]["entry_id"]

    # High-value refunds from the dashboard wait for an approver.
    await stack.general_pool.execute(
        """INSERT INTO merchant_settlement_configs(merchant_id, refund_approval_threshold_minor)
           VALUES ($1, 10_000) ON CONFLICT (merchant_id) DO UPDATE
           SET refund_approval_threshold_minor = 10_000""",
        stack.merchant_id,
    )
    await stack.general_pool.execute("DELETE FROM merchant_limits")
    payment_id = await _upi(stack, 40_000, "hv")
    await bo.user("refund.admin@tally.test", ["merchant_admin"], stack.merchant_id)
    admin = await bo.login("refund.admin@tally.test")
    small = await admin.post(
        "/bff/v1/refunds", json={"payment_id": payment_id, "amount_minor": 500}
    )
    assert small.status_code == 201 and small.json()["status"] in {"processing", "pending"}, (
        small.text
    )
    large = await admin.post(
        "/bff/v1/refunds", json={"payment_id": payment_id, "amount_minor": 20_000}
    )
    assert large.json()["status"] == "pending_approval"
    decided = await checker.post(
        f"/bff/v1/ops/approvals/{large.json()['request_id']}/approve",
        json={"reason": "customer ticket 42"},
    )
    assert decided.json()["status"] == "executed", decided.text
    refunds = await stack.general_pool.fetch(
        "SELECT amount_minor FROM refunds WHERE payment_id = $1", uuid.UUID(payment_id)
    )
    assert sorted(r["amount_minor"] for r in refunds) == [500, 20_000]

    # Chaos control is operator-only and changes the simulator.
    chaos = await maker.post("/bff/v1/ops/chaos", json={"bank_modes": {"bank-a": "timeout"}})
    assert chaos.status_code == 200, chaos.text
    assert stack.bank_app.state.config.modes == {"bank-a": "timeout"}  # type: ignore[attr-defined]
    stack.bank_app.state.config.modes = {}  # type: ignore[attr-defined]
    for c in (maker, dual, checker, viewer, admin):
        await c.aclose()


async def _aml(stack: LocalStack, bo: Backoffice) -> None:
    await stack.general_pool.execute(
        "INSERT INTO core_vpas(vpa, bank_id) VALUES ('blockedperson@bank-a', 'bank-a')"
    )
    for i in range(3):
        await _upi(stack, 4_900_000 - i, f"struct{i}")
    await _upi(stack, 1_000, "sanction", payer="blockedperson@bank-a")
    first = await aml.run_detectors(stack.general_pool)
    assert first["structuring"] == 1 and first["sanctions_match"] >= 1
    second = await aml.run_detectors(stack.general_pool)
    assert sum(second.values()) == 0  # idempotent
    await bo.user("aml.analyst@tally.test", ["risk_analyst"])
    analyst = await bo.login("aml.analyst@tally.test")
    alerts = (await analyst.get("/bff/v1/ops/aml/alerts")).json()
    case = await analyst.post(
        "/bff/v1/ops/aml/cases",
        json={
            "alert_ids": [a["alert_id"] for a in alerts],
            "summary": "structuring near threshold",
        },
    )
    assert case.status_code == 201
    assert (await analyst.get("/bff/v1/ops/aml/alerts")).json() == []
    await analyst.aclose()


async def _audit_chain(stack: LocalStack, bo: Backoffice, store: FilesystemObjectStore) -> None:
    pool = stack.general_pool
    verified = await pool.fetchrow("SELECT * FROM audit_verify()")
    assert verified["ok"] and verified["entries"] > 20
    anchored = await anchor(pool, store)
    assert anchored is not None
    assert await verify_anchors(pool, store) == []
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
        await pool.execute("UPDATE audit_log SET actor = 'mallory' WHERE seq = 1")
    # A privileged operator bypassing the trigger is still detected by the chain and the anchor.
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_mutation")
        await connection.execute("UPDATE audit_log SET actor = 'mallory' WHERE seq = 2")
        await connection.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_mutation")
    broken = await pool.fetchrow("SELECT * FROM audit_verify()")
    assert not broken["ok"] and broken["broken_at"] == 2
    assert await verify_anchors(pool, store)
