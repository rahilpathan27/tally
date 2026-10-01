"""Merchant traffic for the Kubernetes canary drill (scripts/k8s_local.sh).

``seed``    run inside a core pod: creates a test merchant and API key, prints them as JSON.
``traffic`` run as an in-cluster Job: signed create+confirm UPI payments against the core
            Service. Every request opens a new connection so kube-proxy spreads load across
            stable and canary pods (a kept-alive connection would pin one pod).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import sys
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
from libs.security.hmac_auth import sign_request
from libs.security.key_encryption import ApiKeyCipher

SCOPES = ["payments:write", "payments:read"]
PAYERS = 200  # distinct payers keep risk velocity rules out of the way


async def seed(
    merchants: int = 1, payers: int = PAYERS, prefix: str = "drill"
) -> list[dict[str, str]]:
    """Create ``merchants`` test merchants with one API key each, plus ``payers`` payer VPAs."""
    key = base64.b64decode(os.environ["TALLY_API_KEY_ENCRYPTION_KEY"], altchars=b"-_")
    cipher = ApiKeyCipher(key)
    created = []
    connection = await asyncpg.connect(os.environ["TALLY_DATABASE_URL"])
    try:
        # A fresh cluster has no VPA directory; register the payers and the payee.
        await connection.execute(
            """INSERT INTO core_vpas(vpa, bank_id)
               SELECT $2 || n || '@bank-a', 'bank-a' FROM generate_series(0, $1 - 1) n
               UNION ALL SELECT 'merchant@bank-b', 'bank-b'
               ON CONFLICT (vpa) DO NOTHING""",
            payers,
            prefix,
        )
        for i in range(merchants):
            merchant_id = f"{prefix}-merchant" if merchants == 1 else f"{prefix}-merchant-{i}"
            key_id = f"key_{prefix}_{secrets.token_hex(6)}"
            secret = secrets.token_bytes(32)
            await connection.execute(
                """INSERT INTO merchants(merchant_id, display_name) VALUES ($1, $2)
                   ON CONFLICT (merchant_id) DO NOTHING""",
                merchant_id,
                f"{prefix.title()} merchant {i}",
            )
            await connection.execute(
                """INSERT INTO merchant_api_keys(key_id, merchant_id, secret_ciphertext,
                       scopes, mode, expires_at) VALUES ($1, $2, $3, $4, 'test', $5)""",
                key_id,
                merchant_id,
                cipher.encrypt(key_id, secret),
                SCOPES,
                datetime.now(UTC) + timedelta(days=1),
            )
            created.append({"key_id": key_id, "secret_b64": base64.b64encode(secret).decode()})
    finally:
        await connection.close()
    return created


async def signed(
    base_url: str, key_id: str, secret: bytes, path: str, body: dict[str, Any]
) -> httpx.Response:
    raw = json.dumps(body).encode()
    timestamp = int(time.time())
    nonce = f"drill-{uuid.uuid4().hex}"
    headers = {
        "content-type": "application/json",
        "x-tally-key-id": key_id,
        "x-tally-timestamp": str(timestamp),
        "x-tally-nonce": nonce,
        "x-tally-signature": sign_request(secret, "POST", path, raw, timestamp, nonce),
        "idempotency-key": str(uuid.uuid4()),
        "connection": "close",
    }
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        return await client.post(path, content=raw, headers=headers)


async def traffic(seconds: float, rate: float) -> None:
    base_url = os.environ["TALLY_CORE_URL"]
    key_id = os.environ["DRILL_KEY_ID"]
    secret = base64.b64decode(os.environ["DRILL_SECRET_B64"])
    statuses: Counter[str] = Counter()
    deadline = time.monotonic() + seconds
    i = 0

    async def one(n: int) -> None:
        try:
            created = await signed(
                base_url,
                key_id,
                secret,
                "/v1/payment_intents",
                {
                    "amount_minor": 1_000 + n % 500,
                    "currency": "INR",
                    "payment_method_type": "upi",
                    "payer_vpa": f"drill{n % PAYERS}@bank-a",
                    "payee_vpa": "merchant@bank-b",
                },
            )
            statuses[f"create:{created.status_code}"] += 1
            if created.status_code == 201:
                payment_id = created.json()["payment_id"]
                path = f"/v1/payment_intents/{payment_id}/confirm"
                confirmed = await signed(base_url, key_id, secret, path, {})
                statuses[f"confirm:{confirmed.status_code}"] += 1
        except httpx.HTTPError as exc:
            statuses[f"error:{type(exc).__name__}"] += 1

    tasks: set[asyncio.Task[None]] = set()
    last_report = time.monotonic()
    while time.monotonic() < deadline:
        task = asyncio.create_task(one(i))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        i += 1
        if time.monotonic() - last_report > 30:
            print(json.dumps({"sent": i, "statuses": dict(statuses)}), flush=True)
            last_report = time.monotonic()
        await asyncio.sleep(1 / rate)
    await asyncio.gather(*tasks)
    print(json.dumps({"sent": i, "statuses": dict(statuses)}), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    seed_cmd = sub.add_parser("seed")
    seed_cmd.add_argument("--merchants", type=int, default=1)
    seed_cmd.add_argument("--payers", type=int, default=PAYERS)
    seed_cmd.add_argument("--prefix", default="drill")
    run = sub.add_parser("traffic")
    run.add_argument("--seconds", type=float, default=600)
    # The gateway allows 120 requests/min per merchant; each payment is create + confirm.
    run.add_argument("--rate", type=float, default=0.8, help="payments per second")
    args = parser.parse_args()
    if args.command == "seed":
        created = asyncio.run(seed(args.merchants, args.payers, args.prefix))
        # The drill reads one object; load tests read the list.
        print(json.dumps(created[0] if args.merchants == 1 else created))
    else:
        asyncio.run(traffic(args.seconds, args.rate))
    return 0


if __name__ == "__main__":
    sys.exit(main())
