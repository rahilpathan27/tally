"""Run the Tally services locally on real ports with seeded demo data.

Starts the in-process stack (fresh ``tally_dev_*`` databases, real FastAPI services, simulators,
risk, recon, back office) and serves:

* core merchant API  http://127.0.0.1:8000  (HMAC-signed; used by the demo merchant server)
* vault              http://127.0.0.1:8002  (browser checkout posts cards to /public/v1/tokens)
* back office (BFF)  http://127.0.0.1:8040  (the Next.js console proxies /bff and /auth here)

Background workers (recovery, refunds, payouts, outbox, webhooks) run every two seconds.
Credentials for the demo merchant and console users are written to ``.data/dev-stack.json``.
All credentials are local development values.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import logging
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import uvicorn
from chaos.stack import (
    MERCHANT_SECRET,
    PUBLISHABLE_KEY,
    Backoffice,
    LocalStack,
    build_backoffice,
    build_stack,
    provision_databases,
)
from libs.common.business_time import business_date, cutoff_instant
from libs.common.object_store import FilesystemObjectStore
from libs.observability.logging import configure_logging
from services.core.money_routes import run_money_workers
from services.core.recovery_worker import process_recovery_batch, sweep_stalled_payments
from services.recon.api import create_app as create_recon_app
from services.recon.engine import BankRecord, Kind, ReconConfig
from services.recon.formats import write_statement
from services.recon.service import ReconContext, bank_records_from_journal, ingest_file, run_recon

PASSWORD = "correct horse battery staple"
USERS = {
    "admin@demo.test": (["merchant_admin"], True),
    "dev@demo.test": (["merchant_developer"], True),
    "viewer@demo.test": (["merchant_viewer"], True),
    "ops@tally.test": (["ops_analyst"], False),
    "risk@tally.test": (["risk_analyst"], False),
    "approver@tally.test": (["approver"], False),
    "operator@tally.test": (["operator"], False),
}
MERCHANT_ID = "demo-merchant"
KEY_ID = "tly_test_demo_key"
log = logging.getLogger("dev_stack")


async def _pay(stack: LocalStack, body: dict[str, Any], tag: str) -> dict[str, Any]:
    created = await stack.send("POST", "/v1/payment_intents", body, f"seed-{tag}-c")
    payment_id = created.json()["payment_id"]
    confirmed = await stack.send(
        "POST", f"/v1/payment_intents/{payment_id}/confirm", {}, f"seed-{tag}-f"
    )
    result: dict[str, Any] = confirmed.json()
    if result.get("status") == "authorized":
        captured = await stack.send(
            "POST", f"/v1/payment_intents/{payment_id}/capture", {}, f"seed-{tag}-cap"
        )
        result = captured.json()
    return {"payment_id": payment_id, **result}


async def seed(stack: LocalStack, bo: Backoffice, recon: ReconContext) -> dict[str, Any]:
    for email, (roles, is_merchant) in USERS.items():
        await bo.user(email, roles, MERCHANT_ID if is_merchant else None, password=PASSWORD)
    pool = stack.general_pool
    await pool.execute(
        """INSERT INTO merchant_settlement_configs(merchant_id, settlement_delay_days, fee_rate,
               fixed_fee_minor, gst_rate, reserve_rate, reserve_hold_days,
               refund_approval_threshold_minor)
           VALUES ($1, 1, 0.0200, 300, 0.18, 0.05, 7, 500000)""",
        MERCHANT_ID,
    )
    await pool.execute(
        """INSERT INTO core_vpas(vpa, bank_id) VALUES ('asha@bank-a', 'bank-a'),
               ('ravi@bank-c', 'bank-c'), ('meera@bank-a', 'bank-a')
           ON CONFLICT DO NOTHING"""
    )
    home = {"device_id": "phone-asha", "ip_address": "49.36.1.10", "ip_country": "IN"}
    paid: list[dict[str, Any]] = []
    amounts = [49_900, 129_900, 7_500, 250_000, 18_000, 99_900, 64_000, 3_200, 450_000, 12_500]
    for i, amount in enumerate(amounts * 3):
        payer = ("asha@bank-a", "ravi@bank-c", "meera@bank-a")[i % 3]
        if i % 4 == 0:
            body: dict[str, Any] = {
                "amount_minor": amount,
                "currency": "INR",
                "payment_method_type": "card",
                "payment_method_token": stack.card_token,
                "risk_context": {"device_id": "laptop-7", "ip_country": "IN"},
            }
        else:
            body = {
                "amount_minor": amount,
                "currency": "INR",
                "payment_method_type": "upi",
                "payer_vpa": payer,
                "payee_vpa": "merchant@bank-b",
                "risk_context": {**home, "device_id": f"phone-{payer.split('@')[0]}"},
            }
        paid.append(await _pay(stack, body, str(i)))
    # A large transfer from a first-time device lands in the risk review queue.
    paid.append(
        await _pay(
            stack,
            {
                "amount_minor": 4_800_000,
                "currency": "INR",
                "payment_method_type": "upi",
                "payer_vpa": "meera@bank-a",
                "payee_vpa": "merchant@bank-b",
                "risk_context": {
                    "device_id": "unknown-phone",
                    "ip_address": "91.10.2.3",
                    "ip_country": "RU",
                },
            },
            "review",
        )
    )
    succeeded = [p["payment_id"] for p in paid if p.get("status") == "succeeded"]
    mix: dict[str, int] = {}
    for p in paid:
        mix[str(p.get("status"))] = mix.get(str(p.get("status")), 0) + 1
    log.info("seed payment outcomes %s", mix)
    if len(succeeded) < 8:
        raise RuntimeError(f"too few successful seed payments: {mix}")
    await pool.execute(
        "UPDATE payment_intents SET succeeded_at = succeeded_at - interval '2 days', "
        "created_at = created_at - interval '2 days' WHERE payment_id = ANY($1::uuid[])",
        [UUID(p) for p in succeeded[: len(succeeded) // 2]],
    )
    for i, payment_id in enumerate(succeeded[len(succeeded) // 2 :][:3]):
        await stack.send(
            "POST",
            "/v1/refunds",
            {
                "payment_id": payment_id,
                "amount_minor": 1_000 * (i + 1),
                "reason": "customer request",
            },
            f"seed-refund-{i}",
        )
    state = stack.core_app.state  # type: ignore[attr-defined]
    await run_money_workers(state)
    today = business_date(datetime.now(UTC))
    await stack.client.post(
        "/internal/v1/settlements/run",
        json={"merchant_id": MERCHANT_ID, "business_date": today.isoformat()},
        headers={"x-internal-key": state.recovery_key},
    )
    await run_money_workers(state)
    await stack.client.post(
        "/internal/v1/disputes",
        json={
            "payment_id": succeeded[-1],
            "amount_minor": 1_000,
            "reason_code": "fraudulent",
            "network_reference": "CB-DEMO-0001",
        },
        headers={"x-internal-key": state.recovery_key},
    )
    # Today's statement with a few realistic breaks for the reconciliation workbench.
    end = cutoff_instant(today)
    journal = list(stack.bank_app.state.config.journal)  # type: ignore[attr-defined]
    lines = bank_records_from_journal(journal, end - timedelta(days=1), end)
    upi = [line for line in lines if line.kind == Kind.UPI_TRANSFER]
    mutated: list[BankRecord] = []
    for line in lines:
        if upi and line is upi[0]:
            continue  # missing at bank
        if len(upi) > 1 and line is upi[1]:
            line = replace(line, amount_minor=line.amount_minor + 100)
        mutated.append(line)
    mutated.append(
        BankRecord(
            0,
            "7f1c2d3e-0000-4000-8000-000000000042",
            "UTR9990001112223",
            Kind.UPI_TRANSFER,
            21_000,
            "success",
            end - timedelta(hours=2),
        )
    )
    numbered = [replace(line, line_no=n) for n, line in enumerate(mutated, start=1)]
    await ingest_file(
        recon, "sponsor-bank", today, "csv_rupees_ist", write_statement(numbered, "csv_rupees_ist")
    )
    await run_recon(recon, "sponsor-bank", today)
    return {"payments": len(paid), "business_date": today.isoformat()}


async def workers(stack: LocalStack, stop: asyncio.Event) -> None:
    state = stack.core_app.state  # type: ignore[attr-defined]
    while not stop.is_set():
        try:
            await process_recovery_batch(
                state.payment_repository,
                state.bank_http,
                state.ledger_http,
                state.bank_breaker,
                network_http=state.card_network_http,
                network_breaker=state.card_network_breaker,
            )
            await sweep_stalled_payments(state.payment_repository, state.ledger_http)
            await run_money_workers(state)
        except Exception:  # noqa: BLE001 - keep the demo running and log the failure
            log.exception("worker cycle failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=2)


async def main(args: argparse.Namespace) -> None:
    configure_logging("dev-stack")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    origin = args.console_origin
    os.environ.setdefault("TALLY_VAULT_CORS_ORIGINS", origin)
    os.environ.setdefault("TALLY_SIMULATOR_ADMIN_KEY", "stack-sim-admin")
    data = Path(".data")
    data.mkdir(exist_ok=True)
    store = FilesystemObjectStore(data / "objects")
    general, ledger, vault = await provision_databases(args.prefix)
    stack = await build_stack(
        general,
        ledger,
        vault,
        os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        merchant_id=MERCHANT_ID,
        key_id=KEY_ID,
        merchant_name="Demo Store",
        risk=True,
        object_store=store,
        rate_limit=600,
    )
    state = stack.core_app.state  # type: ignore[attr-defined]
    recon_ctx = ReconContext(stack.general_pool, state.ledger_http, store, ReconConfig())
    recon_app = create_recon_app(recon_ctx, internal_key="recon-key")
    bo = await build_backoffice(stack, recon_app=recon_app, chaos_enabled=True)
    bo.app.state.secure_cookies = args.secure_cookies
    summary = await seed(stack, bo, recon_ctx)
    credentials = {
        "core_url": f"http://127.0.0.1:{args.core_port}",
        "vault_url": f"http://127.0.0.1:{args.vault_port}",
        "bff_url": f"http://127.0.0.1:{args.bff_port}",
        "merchant_id": MERCHANT_ID,
        "key_id": KEY_ID,
        "secret_b64": base64.b64encode(MERCHANT_SECRET).decode(),
        "publishable_key": PUBLISHABLE_KEY,
        "internal_key": state.recovery_key,
        "risk_key": state.risk_key,
        "users": {email: PASSWORD for email in USERS},
        "seed": summary,
    }
    (data / "dev-stack.json").write_text(json.dumps(credentials, indent=2))
    log.info("dev stack ready", extra={"seed": summary})
    servers = [
        uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning")
        )
        for app, port in cast(
            list[tuple[Any, int]],
            [
                (stack.core_app, args.core_port),
                (stack.vault_app, args.vault_port),
                (bo.app, args.bff_port),
                (create_risk_proxy(stack), args.risk_port),
            ],
        )
    ]
    stop = asyncio.Event()
    worker = asyncio.create_task(workers(stack, stop))
    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        stop.set()
        await worker
        await stack.close()


def create_risk_proxy(stack: LocalStack) -> Any:
    """Expose the in-process risk app so the payer simulator can read step-up codes."""
    return stack.risk_app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default="tally_dev")
    parser.add_argument("--core-port", type=int, default=8000)
    parser.add_argument("--vault-port", type=int, default=8002)
    parser.add_argument("--risk-port", type=int, default=8030)
    parser.add_argument("--bff-port", type=int, default=8040)
    parser.add_argument("--console-origin", default="http://localhost:3000")
    parser.add_argument(
        "--secure-cookies",
        action="store_true",
        help="mark cookies Secure (needs HTTPS in front of the console)",
    )
    asyncio.run(main(parser.parse_args()))
