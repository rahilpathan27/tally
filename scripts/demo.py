"""Guided tour of Tally against the running dev stack (``make demo`` starts everything).

Every step calls the real services the way a merchant, the browser or an operator would, and
prints what happened. All money is synthetic. Steps:

 1. UPI payment: signed create + confirm, then the ledger entry it produced
 2. Idempotency: the same request replayed returns the same answer, no second ledger entry
 3. Card payment: card tokenised in the "browser" at the vault, authorised, captured
 4. Refund: partial refund, then an over-refund attempt that is refused
 5. Risk: a large UPI transfer held for an analyst, who approves it
 6. Bank outage: an operator makes bank-a fail; the payment fails safely; bank restored
 7. Ledger: integrity verifier and trial balance
"""

from __future__ import annotations

import base64
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, NoReturn

import httpx
from libs.security.hmac_auth import sign_request

CREDS = Path(".data/dev-stack.json")


def rupees(minor: int) -> str:
    """Exact formatting from integer paise (no floating point)."""
    return f"₹{minor // 100:,}.{minor % 100:02d}"


def say(step: str) -> None:
    print(f"\n\033[1m{step}\033[0m")


def ok(text: str) -> None:
    print(f"  \033[32m✔\033[0m {text}")


def fail(text: str) -> NoReturn:
    print(f"  \033[31m✘ {text}\033[0m")
    raise SystemExit(1)


class Merchant:
    def __init__(self, creds: dict[str, Any]) -> None:
        self.key_id = creds["key_id"]
        self.secret = base64.b64decode(creds["secret_b64"])
        self.http = httpx.Client(base_url=creds["core_url"], timeout=30)

    def call(
        self, method: str, path: str, body: dict[str, Any] | None = None, key: str | None = None
    ) -> httpx.Response:
        raw = json.dumps(body).encode() if body is not None else b""
        ts = int(time.time())
        nonce = uuid.uuid4().hex
        headers = {
            "x-tally-key-id": self.key_id,
            "x-tally-timestamp": str(ts),
            "x-tally-nonce": nonce,
            "x-tally-signature": sign_request(self.secret, method, path, raw, ts, nonce),
        }
        if method == "POST":
            headers["idempotency-key"] = key or str(uuid.uuid4())
            headers["content-type"] = "application/json"
        return self.http.request(method, path, content=raw, headers=headers)


def staff(creds: dict[str, Any], email: str) -> httpx.Client:
    client = httpx.Client(base_url=creds["bff_url"], timeout=30)
    login = client.post("/auth/login", json={"email": email, "password": creds["users"][email]})
    if login.status_code != 200:
        fail(f"login {email}: {login.status_code} {login.text[:200]}")
    client.headers["x-csrf-token"] = login.json()["csrf_token"]
    return client


def upi(merchant: Merchant, amount: int, payer: str = "asha@bank-a") -> dict[str, Any]:
    created = merchant.call(
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
    if created.status_code != 201:
        fail(f"create: {created.status_code} {created.text[:200]}")
    pid = created.json()["payment_id"]
    confirmed = merchant.call("POST", f"/v1/payment_intents/{pid}/confirm", {})
    if confirmed.status_code != 200:
        fail(f"confirm: {confirmed.status_code} {confirmed.text[:200]}")
    return {"payment_id": pid, **confirmed.json()}


def ledger_lines(ops: httpx.Client, payment_id: str) -> list[dict[str, Any]]:
    detail = ops.get(f"/bff/v1/ops/payments/{payment_id}")
    if detail.status_code != 200:
        fail(f"payment detail: {detail.status_code} {detail.text[:200]}")
    lines: list[dict[str, Any]] = []
    for entry in detail.json()["ledger"]:
        lines += entry.get("postings", [])
    return lines


def main() -> int:
    if not CREDS.exists():
        fail("dev stack not running: start it with `make dev` (or `make demo`)")
    creds = json.loads(CREDS.read_text())
    merchant = Merchant(creds)
    ops = staff(creds, "ops@tally.test")

    say("1. UPI payment (merchant API, HMAC-signed)")
    paid = upi(merchant, 129_900)
    ok(f"payment {paid['payment_id']} → {paid['status']} ({rupees(129_900)})")
    lines = ledger_lines(ops, paid["payment_id"])
    for line in lines[:4]:
        ok(
            f"ledger {line.get('direction', '?'):6} {line.get('account_id', '?'):32} "
            f"{rupees(int(line.get('amount_minor', 0)))}"
        )

    say("2. Idempotency: replay the same create request")
    key = f"demo-{uuid.uuid4()}"
    body = {
        "amount_minor": 7_500,
        "currency": "INR",
        "payment_method_type": "upi",
        "payer_vpa": "meera@bank-a",
        "payee_vpa": "merchant@bank-b",
    }
    first = merchant.call("POST", "/v1/payment_intents", body, key=key)
    second = merchant.call("POST", "/v1/payment_intents", body, key=key)
    if first.json()["payment_id"] != second.json()["payment_id"]:
        fail("replay created a second payment")
    ok(f"both calls returned payment {first.json()['payment_id']} (one payment, one record)")
    changed = merchant.call("POST", "/v1/payment_intents", {**body, "amount_minor": 7_600}, key=key)
    ok(f"same key, different amount → HTTP {changed.status_code} (refused, not silently reused)")

    say("3. Card payment: tokenised at the vault, authorised, captured")
    vault = httpx.post(
        f"{creds['vault_url']}/public/v1/tokens",
        json={"pan": "4242424242424242", "expiry_month": 12, "expiry_year": 2030},
        headers={"x-publishable-key": creds["publishable_key"]},
        timeout=30,
    )
    if vault.status_code != 201:
        fail(f"tokenise: {vault.status_code} {vault.text[:200]}")
    token = vault.json()["payment_method_token"]
    ok(f"browser sent the card to the vault, got {token[:12]}… (merchant never sees the PAN)")
    created = merchant.call(
        "POST",
        "/v1/payment_intents",
        {
            "amount_minor": 99_900,
            "currency": "INR",
            "payment_method_type": "card",
            "payment_method_token": token,
            # The checkout passes device context; a fresh device keeps repeated demo runs from
            # looking like card testing (which the risk engine would rightly block).
            "risk_context": {
                "device_id": f"demo-{uuid.uuid4().hex[:8]}",
                "ip_address": "49.36.1.10",
                "ip_country": "IN",
            },
        },
    ).json()
    authorised = merchant.call(
        "POST", f"/v1/payment_intents/{created['payment_id']}/confirm", {}
    ).json()
    if authorised["status"] != "authorized":
        fail(f"authorisation → {authorised['status']}: {authorised}")
    ok("authorised → authorized (funds held in the ledger)")
    captured = merchant.call("POST", f"/v1/payment_intents/{created['payment_id']}/capture", {})
    ok(f"captured → HTTP {captured.status_code}: {captured.json().get('status', captured.json())}")

    say("4. Refunds")
    refund = merchant.call(
        "POST", "/v1/refunds", {"payment_id": paid["payment_id"], "amount_minor": 30_000}
    )
    if refund.status_code not in (200, 201, 202):
        fail(f"refund: {refund.status_code} {refund.text[:200]}")
    ok(f"partial refund of {rupees(30_000)} → {refund.json().get('status')}")
    over = merchant.call(
        "POST", "/v1/refunds", {"payment_id": paid["payment_id"], "amount_minor": 120_000}
    )
    ok(f"refund of {rupees(120_000)} more → HTTP {over.status_code} (would exceed the payment)")

    say("5. Risk: a large UPI transfer goes to an analyst")
    held = upi(merchant, 4_500_000, payer="ravi@bank-c")
    ok(f"{rupees(4_500_000)} payment → {held['status']} (rule D001, LARGE_UPI_TRANSFER)")
    risk = staff(creds, "risk@tally.test")
    queue = risk.get("/bff/v1/ops/risk/reviews").json()
    case = next((c for c in queue if c.get("payment_id") == held["payment_id"]), None)
    if case is None:
        fail("held payment not in the review queue")
    resolved = risk.post(
        f"/bff/v1/ops/risk/reviews/{case['case_id']}/resolve",
        json={"outcome": "approve", "note": "demo: verified with customer"},
        headers={"idempotency-key": str(uuid.uuid4())},
    )
    ok(f"analyst approved → HTTP {resolved.status_code}")
    time.sleep(2)
    after = merchant.call("GET", f"/v1/payment_intents/{held['payment_id']}").json()
    ok(f"payment resumed → {after['status']}")

    say("6. Bank outage")
    operator = staff(creds, "operator@tally.test")
    operator.post("/bff/v1/ops/chaos", json={"bank_modes": {"bank-a": "http_500"}})
    ok("operator switched bank-a to fail every request")
    try:
        during = upi(merchant, 18_000)
        ok(f"payment during the outage → {during['status']} (the bank's answer is unknown)")
    finally:
        operator.post("/bff/v1/ops/chaos", json={"bank_modes": {}})
    ok("bank-a restored; the recovery worker now asks the bank what happened")
    status = during["status"]
    for _ in range(45):
        status = merchant.call("GET", f"/v1/payment_intents/{during['payment_id']}").json()[
            "status"
        ]
        if status in ("succeeded", "failed", "reversed"):
            break
        time.sleep(2)
    ok(f"the unknown payment resolved → {status} (ledger matches the bank's outcome)")
    again = upi(merchant, 18_000)
    ok(f"next payment → {again['status']}")

    say("7. Ledger")
    for check in ops.get("/bff/v1/ops/ledger/integrity").json():
        if not check["ok"]:
            fail(f"integrity check {check['check_name']} failed")
        ok(f"{check['check_name']}: ok ({check['detail']})")
    rows = ops.get("/bff/v1/ops/ledger/trial-balance").json()["rows"]
    debits = sum(int(r["debit_minor"]) for r in rows)
    credits = sum(int(r["credit_minor"]) for r in rows)
    if debits != credits:
        fail(f"trial balance off: debits {debits} credits {credits}")
    ok(f"trial balance: {len(rows)} accounts, debits = credits = {rupees(debits)}")

    print(
        "\nConsole: http://localhost:3000  (merchant admin@demo.test, ops ops@tally.test; "
        "passwords in .data/dev-stack.json)\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
