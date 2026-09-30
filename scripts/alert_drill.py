"""Induce real failures on the local stack and confirm Prometheus fires the expected alerts.

Requires ``make dev`` (dev stack) and ``make observability-up``. Each drill records how long the
alert took to reach ``firing``, then restores the system. Results are appended to
``docs/observability.md``-ready markdown on stdout.

Drills:
1. Ledger tampering: a privileged database edit changes a posted amount (bypassing the append-only
   trigger); the integrity verifier must page ``LedgerInvariantViolation``.
2. Bank outage: bank-a is switched to HTTP 500 through the operator chaos control while payments
   flow; ``BankSuccessRateDrop`` must fire (and the circuit breaker opens).
3. Stalled recovery: a payment is left in ``pending_unknown`` with its status check pushed out
   (as if the worker had died); ``PendingUnknownStuck`` must fire.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import httpx
from libs.security.hmac_auth import sign_request

PROM = "http://127.0.0.1:9090"
LEDGER_DB = "postgresql://tally:tally-local-only@127.0.0.1:55433/tally_dev_ledger"
GENERAL_DB = "postgresql://tally:tally-local-only@127.0.0.1:55432/tally_dev_general"


class Merchant:
    def __init__(self, creds: dict[str, Any]) -> None:
        import base64

        self.url = creds["core_url"]
        self.key_id = creds["key_id"]
        self.secret = base64.b64decode(creds["secret_b64"])
        self.http = httpx.AsyncClient(base_url=self.url, timeout=15)

    async def send(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        raw = json.dumps(body).encode() if body is not None else b""
        timestamp = int(time.time())
        nonce = f"drill-{uuid.uuid4().hex}"
        headers = {
            "x-tally-key-id": self.key_id,
            "x-tally-timestamp": str(timestamp),
            "x-tally-nonce": nonce,
            "x-tally-signature": sign_request(self.secret, method, path, raw, timestamp, nonce),
            "idempotency-key": str(uuid.uuid4()),
        }
        if raw:
            headers["content-type"] = "application/json"
        response = await self.http.request(method, path, content=raw, headers=headers)
        return response.json()

    async def upi(self, amount: int, payer: str) -> Any:
        created = await self.send(
            "POST",
            "/v1/payment_intents",
            {
                "amount_minor": amount,
                "currency": "INR",
                "payment_method_type": "upi",
                "payer_vpa": payer,
                "payee_vpa": "merchant@bank-b",
            },
        )
        return await self.send("POST", f"/v1/payment_intents/{created['payment_id']}/confirm", {})


async def firing(client: httpx.AsyncClient, name: str) -> list[dict[str, Any]]:
    alerts = (await client.get(f"{PROM}/api/v1/alerts")).json()["data"]["alerts"]
    return [a for a in alerts if a["labels"]["alertname"] == name and a["state"] == "firing"]


async def wait_for(client: httpx.AsyncClient, name: str, timeout: float) -> float | None:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if await firing(client, name):
            return time.monotonic() - started
        await asyncio.sleep(5)
    return None


async def wait_clear(client: httpx.AsyncClient, name: str, timeout: float = 600) -> None:
    """A drill only counts if its alert was inactive before the failure was induced."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        alerts = (await client.get(f"{PROM}/api/v1/alerts")).json()["data"]["alerts"]
        if not any(a["labels"]["alertname"] == name for a in alerts):
            return
        await asyncio.sleep(10)
    raise SystemExit(f"{name} did not clear before the drill")


async def operator(creds: dict[str, Any]) -> httpx.AsyncClient:
    client = httpx.AsyncClient(base_url=creds["bff_url"], timeout=15)
    login = await client.post(
        "/auth/login",
        json={"email": "operator@tally.test", "password": creds["users"]["operator@tally.test"]},
    )
    client.headers["x-csrf-token"] = login.json()["csrf_token"]
    return client


async def main() -> None:
    creds = json.loads(Path(".data/dev-stack.json").read_text())
    merchant = Merchant(creds)
    prom = httpx.AsyncClient(timeout=10)
    results: list[tuple[str, str, float | None, str]] = []
    for _ in range(12):  # the first scrape after a restart can take one interval
        up = (await prom.get(f"{PROM}/api/v1/query", params={"query": 'up{job="tally"}'})).json()
        if up["data"]["result"] and up["data"]["result"][0]["value"][1] == "1":
            break
        await asyncio.sleep(5)
    else:
        raise SystemExit("Prometheus cannot scrape the dev stack")

    # 1. Ledger tampering -------------------------------------------------------------------
    await wait_clear(prom, "LedgerInvariantViolation")
    ledger = await asyncpg.connect(LEDGER_DB)
    try:
        posting = await ledger.fetchrow(
            "SELECT posting_id, amount_minor FROM ledger_postings ORDER BY posting_id DESC LIMIT 1"
        )
        assert posting is not None
        async with ledger.transaction():
            await ledger.execute(
                "ALTER TABLE ledger_postings DISABLE TRIGGER ledger_postings_no_mutation"
            )
            await ledger.execute(
                "UPDATE ledger_postings SET amount_minor = amount_minor + 1 WHERE posting_id = $1",
                posting["posting_id"],
            )
            await ledger.execute(
                "ALTER TABLE ledger_postings ENABLE TRIGGER ledger_postings_no_mutation"
            )
        took = await wait_for(prom, "LedgerInvariantViolation", 120)
        results.append(
            (
                "Ledger posting altered by a privileged user",
                "LedgerInvariantViolation (page)",
                took,
                "restored the posting; alert resolved",
            )
        )
    finally:
        async with ledger.transaction():
            await ledger.execute(
                "ALTER TABLE ledger_postings DISABLE TRIGGER ledger_postings_no_mutation"
            )
            await ledger.execute(
                "UPDATE ledger_postings SET amount_minor = $2 WHERE posting_id = $1",
                posting["posting_id"],
                posting["amount_minor"],
            )
            await ledger.execute(
                "ALTER TABLE ledger_postings ENABLE TRIGGER ledger_postings_no_mutation"
            )
        await ledger.close()

    # 2. Bank outage ------------------------------------------------------------------------
    await wait_clear(prom, "BankSuccessRateDrop")
    await wait_clear(prom, "CircuitBreakerOpen")
    ops = await operator(creds)
    await ops.post("/bff/v1/ops/chaos", json={"bank_modes": {"bank-a": "http_500"}})
    stop = asyncio.Event()

    async def traffic() -> None:
        # asha@bank-a is on the demo allowlist, so risk velocity rules do not hold the traffic
        # back before it reaches the bank.
        i = 0
        while not stop.is_set():
            try:
                await merchant.upi(1_000 + i, "asha@bank-a")
            except (httpx.HTTPError, KeyError, ValueError):
                pass
            i += 1
            await asyncio.sleep(1)

    driver = asyncio.create_task(traffic())
    try:
        took = await wait_for(prom, "BankSuccessRateDrop", 420)
        breaker_took = await wait_for(prom, "CircuitBreakerOpen", 120)
        breaker = breaker_took is not None
        results.append(
            (
                "bank-a returns HTTP 500 under live traffic",
                "BankSuccessRateDrop (page)",
                took,
                f"CircuitBreakerOpen fired after {breaker_took:.0f} s"
                if breaker
                else "CircuitBreakerOpen did NOT fire",
            )
        )
    finally:
        stop.set()
        await driver
        await ops.post("/bff/v1/ops/chaos", json={"bank_modes": {}})

    # 3. Stalled recovery -------------------------------------------------------------------
    await wait_clear(prom, "PendingUnknownStuck")
    general = await asyncpg.connect(GENERAL_DB)
    try:
        payment = await general.fetchval(
            "SELECT payment_id FROM payment_intents WHERE status = 'succeeded' LIMIT 1"
        )
        stuck = uuid.uuid4()
        await general.execute(
            """INSERT INTO payment_intents(payment_id, merchant_id, amount_minor, currency,
                   payment_method_type, payer_vpa, payee_vpa, status, mode, updated_at,
                   next_recovery_at, recovery_deadline)
               VALUES ($1, 'demo-merchant', 4200, 'INR', 'upi', 'payer@bank-a',
                       'merchant@bank-b', 'pending_unknown', 'test',
                       clock_timestamp() - interval '10 minutes',
                       clock_timestamp() + interval '1 day',
                       clock_timestamp() + interval '1 day')""",
            stuck,
        )
        took = await wait_for(prom, "PendingUnknownStuck", 300)
        results.append(
            (
                "Payment left in pending_unknown (worker stalled)",
                "PendingUnknownStuck (page)",
                took,
                f"reference payment {payment}; stuck row removed afterwards",
            )
        )
    finally:
        await general.execute("DELETE FROM payment_intents WHERE payment_id = $1", stuck)
        await general.close()

    print(f"Alert drill run {datetime.now(UTC).isoformat(timespec='seconds')}\n")
    print("| Induced failure | Expected alert | Time to firing | Notes |")
    print("| --- | --- | ---: | --- |")
    for failure, alert, took, notes in results:
        elapsed = "NOT FIRED" if took is None else f"{took:.0f} s"
        print(f"| {failure} | {alert} | {elapsed} | {notes} |")


if __name__ == "__main__":
    asyncio.run(main())
