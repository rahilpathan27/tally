"""Run every failure mode and edge case against the running dev stack and check the outcome.

    make dev            # or make demo, in another terminal
    uv run python -m scripts.edge_cases            # everything (about 6 minutes)
    uv run python -m scripts.edge_cases upi card   # only some groups

Each scenario sets the simulators (as an operator would from the console's chaos page), makes a
payment through the signed merchant API, waits until the payment is final, and checks two
things: the final status is the expected one, and the ledger holds exactly the expected effect
(one transfer, a posted hold, a voided hold, or nothing). Simulators are reset to `approve`
after every scenario, also on failure.

Groups: upi, card, psp, api, refunds, risk, approvals.
"""

from __future__ import annotations

import base64
import json
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from libs.security.hmac_auth import sign_request

CREDS = Path(".data/dev-stack.json")
TRUSTED_PAYER = "asha@bank-a"  # allow-listed in the demo rules, so risk does not interfere
FINAL = {"succeeded", "failed", "reversed", "cancelled", "expired", "authorized", "risk_review"}


@dataclass
class Result:
    group: str
    name: str
    ok: bool
    detail: str


class Tally:
    def __init__(self) -> None:
        if not CREDS.exists():
            raise SystemExit("Start the dev stack first: make dev (or make demo)")
        self.creds = json.loads(CREDS.read_text())
        self.key_id = self.creds["key_id"]
        self.secret = base64.b64decode(self.creds["secret_b64"])
        self.core = httpx.Client(base_url=self.creds["core_url"], timeout=30)
        self.ops = self.staff("ops@tally.test")
        self.operator = self.staff("operator@tally.test")

    def staff(self, email: str) -> httpx.Client:
        client = httpx.Client(base_url=self.creds["bff_url"], timeout=30)
        login = client.post(
            "/auth/login", json={"email": email, "password": self.creds["users"][email]}
        )
        login.raise_for_status()
        client.headers["x-csrf-token"] = login.json()["csrf_token"]
        return client

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        key: str | None = None,
        secret: bytes | None = None,
        timestamp: int | None = None,
        nonce: str | None = None,
        tamper: bool = False,
    ) -> httpx.Response:
        raw = json.dumps(body).encode() if body is not None else b""
        ts = int(time.time()) if timestamp is None else timestamp
        nonce = nonce or uuid.uuid4().hex
        signature = sign_request(secret or self.secret, method, path, raw, ts, nonce)
        headers = {
            "x-tally-key-id": self.key_id,
            "x-tally-timestamp": str(ts),
            "x-tally-nonce": nonce,
            "x-tally-signature": signature,
        }
        if method == "POST":
            headers["idempotency-key"] = key or str(uuid.uuid4())
            headers["content-type"] = "application/json"
        if tamper:
            raw = raw.replace(b"1", b"2", 1)
        return self.core.request(method, path, content=raw, headers=headers)

    def chaos(
        self, banks: dict[str, str] | None = None, network: str = "approve", psp: str = "approve"
    ) -> None:
        response = self.operator.post(
            "/bff/v1/ops/chaos",
            json={"bank_modes": banks or {}, "card_network_mode": network, "payer_psp_mode": psp},
        )
        response.raise_for_status()

    def reset(self) -> None:
        self.chaos()

    def upi(self, amount: int, payer: str = TRUSTED_PAYER, device: str | None = None) -> str:
        body: dict[str, Any] = {
            "amount_minor": amount,
            "currency": "INR",
            "payment_method_type": "upi",
            "payer_vpa": payer,
            "payee_vpa": "merchant@bank-b",
        }
        if device:
            body["risk_context"] = {
                "device_id": device,
                "ip_address": "49.36.1.10",
                "ip_country": "IN",
            }
        created = self.call("POST", "/v1/payment_intents", body)
        created.raise_for_status()
        pid: str = created.json()["payment_id"]
        self.call("POST", f"/v1/payment_intents/{pid}/confirm", {})
        return pid

    def card(self, amount: int, pan: str = "4242424242424242") -> str:
        token = httpx.post(
            f"{self.creds['vault_url']}/public/v1/tokens",
            json={"pan": pan, "expiry_month": 12, "expiry_year": 2030},
            headers={"x-publishable-key": self.creds["publishable_key"]},
            timeout=30,
        ).json()["payment_method_token"]
        created = self.call(
            "POST",
            "/v1/payment_intents",
            {
                "amount_minor": amount,
                "currency": "INR",
                "payment_method_type": "card",
                "payment_method_token": token,
                # a fresh device per payment, as a real checkout would send
                "risk_context": {
                    "device_id": f"edge-{uuid.uuid4().hex[:10]}",
                    "ip_address": "49.36.1.10",
                    "ip_country": "IN",
                },
            },
        )
        created.raise_for_status()
        pid: str = created.json()["payment_id"]
        self.call("POST", f"/v1/payment_intents/{pid}/confirm", {})
        return pid

    def status(self, pid: str) -> str:
        return str(self.call("GET", f"/v1/payment_intents/{pid}").json()["status"])

    def wait_final(self, pid: str, timeout: float = 90) -> str:
        """Wait out unknown outcomes: status-check deadlines are 30 s in the dev data."""
        deadline = time.monotonic() + timeout
        status = self.status(pid)
        while status not in FINAL and time.monotonic() < deadline:
            time.sleep(2)
            status = self.status(pid)
        return status

    def ledger(self, pid: str) -> list[str]:
        """Ledger effects for a payment, e.g. ['transfer'], ['hold:posted'], ['hold:void']."""
        detail = self.ops.get(f"/bff/v1/ops/payments/{pid}").json()
        effects = []
        for item in detail.get("ledger", []):
            key = str(item.get("idempotency_key", ""))
            if item.get("kind") == "hold" or key.endswith(":card:authorization:hold"):
                effects.append(f"hold:{item.get('status', '?')}")
            elif key.endswith(":upi:transfer:post"):
                effects.append("transfer")
            elif "late-success-correction" in key:
                effects.append("suspense-correction")
            elif item.get("kind") == "entry":
                effects.append("entry")
        return sorted(effects)


Scenario = Callable[[Tally], tuple[bool, str]]


def flow(
    make: Callable[[Tally], str],
    expect_status: set[str],
    expect_ledger: list[str],
    after: Callable[[Tally, str], None] | None = None,
) -> Scenario:
    def run(t: Tally) -> tuple[bool, str]:
        pid = make(t)
        t.reset()  # the bank "recovers" so status checks can resolve
        if after is not None:
            after(t, pid)
        status = t.wait_final(pid)
        effects = t.ledger(pid)
        ok = status in expect_status and effects == expect_ledger
        return ok, f"status={status} ledger={effects or ['none']} (payment {pid[:8]})"

    return run


def with_modes(
    banks: dict[str, str] | None = None,
    network: str = "approve",
    psp: str = "approve",
    make: Callable[[Tally], str] | None = None,
) -> Callable[[Tally], str]:
    def go(t: Tally) -> str:
        t.chaos(banks, network, psp)
        assert make is not None
        return make(t)

    return go


def capture(t: Tally, pid: str) -> None:
    if t.wait_final(pid) == "authorized":
        t.call("POST", f"/v1/payment_intents/{pid}/capture", {})


def cancel(t: Tally, pid: str) -> None:
    if t.wait_final(pid) == "authorized":
        t.call("POST", f"/v1/payment_intents/{pid}/cancel", {})


def upi_payment(amount: int = 12_300) -> Callable[[Tally], str]:
    return lambda t: t.upi(amount)


def card_payment(amount: int = 45_000) -> Callable[[Tally], str]:
    return lambda t: t.card(amount)


# --- API edge cases -------------------------------------------------------------------------
def idempotent_replay(t: Tally) -> tuple[bool, str]:
    body = {
        "amount_minor": 5_100,
        "currency": "INR",
        "payment_method_type": "upi",
        "payer_vpa": TRUSTED_PAYER,
        "payee_vpa": "merchant@bank-b",
    }
    key = str(uuid.uuid4())
    a = t.call("POST", "/v1/payment_intents", body, key=key)
    b = t.call("POST", "/v1/payment_intents", body, key=key)
    changed = t.call("POST", "/v1/payment_intents", {**body, "amount_minor": 5_200}, key=key)
    ok = a.json()["payment_id"] == b.json()["payment_id"] and changed.status_code == 422
    return ok, f"same key → same payment; changed payload → HTTP {changed.status_code}"


def double_confirm(t: Tally) -> tuple[bool, str]:
    pid = t.call(
        "POST",
        "/v1/payment_intents",
        {
            "amount_minor": 5_300,
            "currency": "INR",
            "payment_method_type": "upi",
            "payer_vpa": TRUSTED_PAYER,
            "payee_vpa": "merchant@bank-b",
        },
    ).json()["payment_id"]
    first = t.call("POST", f"/v1/payment_intents/{pid}/confirm", {})
    second = t.call("POST", f"/v1/payment_intents/{pid}/confirm", {})
    effects = t.ledger(pid)
    ok = effects == ["transfer"] and second.status_code in (200, 409, 422)
    return ok, (
        f"confirm twice (new keys) → HTTP {first.status_code}, {second.status_code}; "
        f"ledger={effects}"
    )


def bad_signature(t: Tally) -> tuple[bool, str]:
    wrong = t.call("GET", "/v1/payment_intents/" + str(uuid.uuid4()), secret=b"x" * 32)
    tampered = t.call(
        "POST",
        "/v1/payment_intents",
        {
            "amount_minor": 1_000,
            "currency": "INR",
            "payment_method_type": "upi",
            "payer_vpa": TRUSTED_PAYER,
            "payee_vpa": "merchant@bank-b",
        },
        tamper=True,
    )
    stale = t.call(
        "GET", "/v1/payment_intents/" + str(uuid.uuid4()), timestamp=int(time.time()) - 3_600
    )
    nonce = uuid.uuid4().hex
    t.call("GET", "/v1/payment_intents/" + str(uuid.uuid4()), nonce=nonce)
    replay = t.call("GET", "/v1/payment_intents/" + str(uuid.uuid4()), nonce=nonce)
    codes = [wrong.status_code, tampered.status_code, stale.status_code, replay.status_code]
    # A replayed nonce is reported as 409 REQUEST_REPLAYED rather than a bad signature.
    return codes == [401, 401, 401, 409], (
        f"wrong secret, tampered body, 1-hour-old timestamp → {codes[:3]}; "
        f"replayed nonce → {codes[3]} (REQUEST_REPLAYED)"
    )


def invalid_amounts(t: Tally) -> tuple[bool, str]:
    codes = []
    for amount in (0, -100, 10.5, "100", 2**60):
        r = t.call(
            "POST",
            "/v1/payment_intents",
            {
                "amount_minor": amount,
                "currency": "INR",
                "payment_method_type": "upi",
                "payer_vpa": TRUSTED_PAYER,
                "payee_vpa": "merchant@bank-b",
            },
        )
        codes.append(r.status_code)
    usd = t.call(
        "POST",
        "/v1/payment_intents",
        {
            "amount_minor": 100,
            "currency": "USD",
            "payment_method_type": "upi",
            "payer_vpa": TRUSTED_PAYER,
            "payee_vpa": "merchant@bank-b",
        },
    )
    unknown_vpa = t.call(
        "POST",
        "/v1/payment_intents",
        {
            "amount_minor": 100,
            "currency": "INR",
            "payment_method_type": "upi",
            "payer_vpa": "nobody@bank-z",
            "payee_vpa": "merchant@bank-b",
        },
    )
    codes += [usd.status_code, unknown_vpa.status_code]
    return all(c == 422 for c in codes), (
        f"0, negative, 10.5 (float), '100' (string), 2^60, USD, unknown VPA → {codes}"
    )


def rate_limit(t: Tally) -> tuple[bool, str]:
    # The dev stack allows 600 requests/minute per merchant (deployments default to 120).
    codes = [t.call("GET", f"/v1/payment_intents/{uuid.uuid4()}").status_code for _ in range(650)]
    limited = codes.count(429)
    time.sleep(61)  # let the fixed window reset so later groups are not limited
    return limited > 0, f"650 requests in a minute → {limited} answered 429 (dev limit 600/min)"


def non_test_card(t: Tally) -> tuple[bool, str]:
    r = httpx.post(
        f"{t.creds['vault_url']}/public/v1/tokens",
        json={"pan": "4111111111111111", "expiry_month": 12, "expiry_year": 2030},
        headers={"x-publishable-key": t.creds["publishable_key"]},
        timeout=30,
    )
    cvv = httpx.post(
        f"{t.creds['vault_url']}/public/v1/tokens",
        json={"pan": "4242424242424242", "expiry_month": 12, "expiry_year": 2030, "cvv": "123"},
        headers={"x-publishable-key": t.creds["publishable_key"]},
        timeout=30,
    )
    return r.status_code == 422 and cvv.status_code == 422, (
        f"non-allow-listed card → {r.status_code}; CVV field → {cvv.status_code} (never accepted)"
    )


# --- Refunds --------------------------------------------------------------------------------
def refunds(t: Tally) -> tuple[bool, str]:
    pid = t.upi(20_000)
    t.wait_final(pid)
    part = t.call("POST", "/v1/refunds", {"payment_id": pid, "amount_minor": 5_000})
    rest = t.call("POST", "/v1/refunds", {"payment_id": pid, "amount_minor": 15_000})
    over = t.call("POST", "/v1/refunds", {"payment_id": pid, "amount_minor": 1})
    t.chaos({"bank-a": "decline"})
    failed_pid = t.upi(3_000)
    t.reset()
    t.wait_final(failed_pid)
    on_failed = t.call("POST", "/v1/refunds", {"payment_id": failed_pid, "amount_minor": 100})
    codes = [part.status_code, rest.status_code, over.status_code, on_failed.status_code]
    ok = codes[0] == 201 and codes[1] == 201 and codes[2] == 422 and codes[3] in (409, 422)
    return ok, (
        "₹50 then ₹150 of ₹200 → 201, 201; one more paisa → "
        f"{codes[2]}; refund of a failed payment → {codes[3]}"
    )


# --- Risk -----------------------------------------------------------------------------------
def risk_review(t: Tally) -> tuple[bool, str]:
    pid = t.upi(4_500_000, payer="ravi@bank-c", device=f"edge-{uuid.uuid4().hex[:8]}")
    status = t.status(pid)
    trusted = t.upi(4_500_000)
    trusted_status = t.wait_final(trusted)
    ok = status == "risk_review" and trusted_status == "succeeded"
    return ok, (
        f"₹45,000 from ravi@bank-c → {status}; same from allow-listed {TRUSTED_PAYER} → "
        f"{trusted_status}"
    )


def step_up(t: Tally) -> tuple[bool, str]:
    pid = t.card(600_000)
    status = t.status(pid)
    wrong = t.call("POST", f"/v1/payment_intents/{pid}/step_up", {"code": "000000"})
    return status == "risk_review" and wrong.status_code == 422, (
        f"card ₹6,000 → {status} (one-time code required); wrong code → HTTP {wrong.status_code}"
    )


# --- Maker-checker --------------------------------------------------------------------------
def approvals(t: Tally) -> tuple[bool, str]:
    admin = t.staff("admin@demo.test")
    pid = t.upi(900_000)
    t.wait_final(pid)
    big = admin.post(
        "/bff/v1/refunds",
        json={"payment_id": pid, "amount_minor": 600_000},
        headers={"idempotency-key": str(uuid.uuid4())},
    ).json()
    request_id = big.get("request_id")
    approver = t.staff("approver@tally.test")
    decided = approver.post(
        f"/bff/v1/ops/approvals/{request_id}/approve",
        json={"reason": "edge-case run"},
        headers={"idempotency-key": str(uuid.uuid4())},
    )
    ok = big.get("status") == "pending_approval" and decided.json().get("status") == "executed"
    return ok, (
        f"₹6,000 dashboard refund → {big.get('status')}; approver → {decided.json().get('status')}"
    )


def self_approval(t: Tally) -> tuple[bool, str]:
    ops = t.staff("ops@tally.test")
    proposed = ops.post(
        "/bff/v1/ops/ledger/adjustments",
        json={
            "postings": [
                {"account_id": "platform:suspense:INR", "direction": "debit", "amount_minor": 100},
                {"account_id": "platform:writeoff:INR", "direction": "credit", "amount_minor": 100},
            ],
            "reason": "edge-case self approval",
        },
        headers={"idempotency-key": str(uuid.uuid4())},
    )
    request_id = proposed.json().get("request_id")
    mine = ops.post(
        f"/bff/v1/ops/approvals/{request_id}/approve",
        json={"reason": "me"},
        headers={"idempotency-key": str(uuid.uuid4())},
    )
    viewer = t.staff("viewer@demo.test").post(
        "/bff/v1/refunds",
        json={"payment_id": str(uuid.uuid4()), "amount_minor": 100},
        headers={"idempotency-key": str(uuid.uuid4())},
    )
    ok = proposed.status_code == 201 and mine.status_code == 403 and viewer.status_code == 403
    return ok, (
        f"ops proposes an adjustment → {proposed.status_code}; approves own → "
        f"{mine.status_code}; viewer creates a refund → {viewer.status_code}"
    )


GROUPS: dict[str, list[tuple[str, Scenario]]] = {
    "upi": [
        ("bank approves", flow(upi_payment(), {"succeeded"}, ["transfer"])),
        (
            "bank declines",
            flow(with_modes({"bank-a": "decline"}, make=upi_payment()), {"failed"}, []),
        ),
        (
            "bank outage (HTTP 500) → unknown → deemed not transferred",
            flow(with_modes({"bank-a": "http_500"}, make=upi_payment()), {"reversed"}, []),
        ),
        (
            "approval response lost → status check finds success",
            flow(
                with_modes({"bank-a": "late_success"}, make=upi_payment()),
                {"succeeded"},
                ["transfer"],
            ),
        ),
        (
            # The bank reverses the payer's debit, so no money moved: the payment fails.
            "debit taken, credit failed → debit reversed, payment failed",
            flow(with_modes({"bank-a": "credit_failure"}, make=upi_payment()), {"failed"}, []),
        ),
        (
            "credit failed, reversal response lost → recovery confirms the reversal",
            flow(
                with_modes(
                    {"bank-a": "credit_failure", "bank-b": "reverse_timeout"}, make=upi_payment()
                ),
                {"failed"},
                [],
            ),
        ),
        (
            "bank status unknown until deadline → auto-reverse",
            flow(with_modes({"bank-a": "status_unknown"}, make=upi_payment()), {"reversed"}, []),
        ),
        (
            "bank approved but its status API never shows it → reversed; recon must flag it",
            flow(with_modes({"bank-a": "timeout"}, make=upi_payment()), {"reversed"}, []),
        ),
    ],
    "card": [
        (
            "authorise and capture",
            flow(card_payment(), {"succeeded"}, ["hold:posted"], after=capture),
        ),
        ("authorise and cancel", flow(card_payment(), {"cancelled"}, ["hold:void"], after=cancel)),
        (
            "network declines",
            flow(with_modes(network="decline", make=card_payment()), {"failed"}, []),
        ),
        (
            "network outage",
            flow(with_modes(network="http_500", make=card_payment()), {"failed", "reversed"}, []),
        ),
        (
            "approval response lost → status check → authorised, then captured",
            flow(
                with_modes(network="timeout", make=card_payment()),
                {"succeeded"},
                ["hold:posted"],
                after=capture,
            ),
        ),
    ],
    "psp": [
        ("payer PSP declines", flow(with_modes(psp="decline", make=upi_payment()), {"failed"}, [])),
        (
            "payer PSP outage",
            flow(with_modes(psp="http_500", make=upi_payment()), {"failed", "reversed"}, []),
        ),
    ],
    "api": [
        ("idempotent replay and key reuse", idempotent_replay),
        ("double confirm moves money once", double_confirm),
        ("authentication failures", bad_signature),
        ("invalid amounts and inputs", invalid_amounts),
        ("vault refuses real cards and CVV", non_test_card),
        ("rate limit", rate_limit),
    ],
    "refunds": [("partial, full, over-refund, refund of failed", refunds)],
    "risk": [("analyst review vs allow-list", risk_review), ("card step-up", step_up)],
    "approvals": [
        ("high-value refund needs an approver", approvals),
        ("no self-approval; viewer cannot refund", self_approval),
    ],
}


def main() -> int:
    wanted = sys.argv[1:] or list(GROUPS)
    t = Tally()
    results: list[Result] = []
    try:
        for group in wanted:
            print(f"\n\033[1m{group}\033[0m")
            for name, scenario in GROUPS[group]:
                try:
                    ok, detail = scenario(t)
                except Exception as exc:  # noqa: BLE001 - report and continue with the next case
                    ok, detail = False, f"error: {type(exc).__name__}: {exc}"
                finally:
                    t.reset()
                results.append(Result(group, name, ok, detail))
                mark = "\033[32m✔\033[0m" if ok else "\033[31m✘\033[0m"
                print(f"  {mark} {name}: {detail}", flush=True)
    finally:
        t.reset()
    failed = [r for r in results if not r.ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    integrity = t.ops.get("/bff/v1/ops/ledger/integrity").json()
    print("ledger integrity:", ", ".join(f"{c['check_name']}={c['ok']}" for c in integrity))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
