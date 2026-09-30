"""Synthetic payments with labelled fraud patterns for training and evaluation.

Legitimate customers pay from a home device and country, mostly to a few favourite payees, with
log-normal amounts and daytime-heavy timing. Three fraud patterns are injected:

* **card testing**: a fraud device tries many freshly stolen cards with tiny amounts in minutes;
* **account takeover**: a real customer's instrument suddenly pays new payees at high value from
  a new device in another country;
* **mule network**: many compromised payers each send one or two transfers to a few mule VPAs.

The data is synthetic; patterns and rates are illustrative, not calibrated to any real portfolio.
"""

from __future__ import annotations

import math
import random
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from services.risk.features import RiskEvent

START = datetime(2026, 6, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class LabelledEvent:
    event: RiskEvent
    fraud: bool
    pattern: str  # legit | card_testing | account_takeover | mule


@dataclass(frozen=True, slots=True)
class Customer:
    instrument_id: str
    method: str
    device_id: str
    ip_address: str
    country: str
    account_age_days: int
    typical_minor: int
    payees: tuple[str, ...]


def _customers(rng: random.Random, count: int) -> list[Customer]:
    customers = []
    for i in range(count):
        method = "card" if rng.random() < 0.45 else "upi"
        instrument = f"tok_{i:07d}" if method == "card" else f"user{i}@bank-{rng.choice('abc')}"
        customers.append(
            Customer(
                instrument_id=instrument,
                method=method,
                device_id=f"dev_{i:07d}",
                ip_address=f"49.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}",
                country="IN" if rng.random() < 0.97 else rng.choice(["AE", "SG", "US"]),
                account_age_days=int(rng.expovariate(1 / 700)) + 1,
                typical_minor=int(math.exp(rng.gauss(math.log(80_000), 1.0))),
                payees=tuple(f"merchant{rng.randint(1, 400)}@bank-b" for _ in range(3)),
            )
        )
    return customers


def _daytime(rng: random.Random, day: datetime) -> datetime:
    # IST daytime-heavy: 04:30-16:30 UTC is 10:00-22:00 IST.
    seconds = (
        int(rng.triangular(0, 86_399, 36_000)) if rng.random() < 0.85 else rng.randint(0, 86_399)
    )
    return day + timedelta(seconds=seconds)


def generate(
    days: int = 60, customers: int = 20_000, seed: int = 7, fraud_scale: float = 1.0
) -> list[LabelledEvent]:
    rng = random.Random(seed)
    people = _customers(rng, customers)
    events: list[LabelledEvent] = []
    counter = 0

    def emit(
        at: datetime,
        amount: int,
        customer: Customer,
        *,
        payee: str,
        device: str,
        ip: str,
        ip_country: str,
        fraud: bool,
        pattern: str,
        instrument: str | None = None,
        method: str | None = None,
        instrument_country: str | None = None,
        merchant: str | None = None,
    ) -> None:
        nonlocal counter
        counter += 1
        events.append(
            LabelledEvent(
                RiskEvent(
                    payment_id=f"pay_{counter:09d}",
                    merchant_id=merchant or f"m{zlib.crc32(payee.encode()) % 50:02d}",
                    occurred_at=at,
                    amount_minor=max(100, amount),
                    method=method or customer.method,
                    instrument_id=instrument or customer.instrument_id,
                    payee_id=payee,
                    device_id=device,
                    ip_address=ip,
                    ip_country=ip_country,
                    instrument_country=instrument_country or customer.country,
                    account_age_days=customer.account_age_days,
                ),
                fraud,
                pattern,
            )
        )

    for day_index in range(days):
        day = START + timedelta(days=day_index)
        for customer in rng.sample(people, k=customers // 6):
            for _ in range(1 + int(rng.random() < 0.25)):
                favourite = rng.random() < 0.8
                payee = (
                    rng.choice(customer.payees)
                    if favourite
                    else f"merchant{rng.randint(1, 400)}@bank-b"
                )
                traveling = rng.random() < 0.01
                emit(
                    _daytime(rng, day),
                    int(customer.typical_minor * math.exp(rng.gauss(0, 0.5))),
                    customer,
                    payee=payee,
                    device=customer.device_id if rng.random() < 0.97 else f"dev_new_{counter}",
                    ip=customer.ip_address,
                    ip_country=rng.choice(["AE", "GB"]) if traveling else customer.country,
                    fraud=False,
                    pattern="legit",
                )

        # Legitimate behaviour that looks like fraud keeps the problem honest.
        for customer in rng.sample(people, k=max(1, customers // 400)):
            # Micro-payment bursts (transit, top-ups) resemble card testing.
            start = _daytime(rng, day)
            for step in range(rng.randint(4, 12)):
                emit(
                    start + timedelta(seconds=step * rng.randint(20, 240)),
                    rng.randint(100, 3_000),
                    customer,
                    payee=f"transit{rng.randint(1, 5)}@bank-b",
                    device=customer.device_id,
                    ip=customer.ip_address,
                    ip_country=customer.country,
                    fraud=False,
                    pattern="legit",
                )
        for customer in rng.sample(people, k=max(1, customers // 300)):
            # One-off large purchases or rent to a new payee, sometimes on a new phone.
            emit(
                _daytime(rng, day),
                int(customer.typical_minor * rng.uniform(4, 20)),
                customer,
                payee=f"landlord{rng.randint(1, 10_000)}@bank-a",
                device=customer.device_id if rng.random() < 0.7 else f"dev_upgrade_{counter}",
                ip=customer.ip_address,
                ip_country=customer.country if rng.random() < 0.9 else "AE",
                fraud=False,
                pattern="legit",
            )

        # Card testing: one device, many stolen cards, tiny amounts, minutes apart.
        for _ in range(rng.randint(1, max(1, int(3 * fraud_scale)))):
            start = day + timedelta(seconds=rng.randint(0, 80_000))
            device = f"fraud_dev_{day_index}_{rng.randint(0, 9999)}"
            ip = f"185.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"
            merchant_payee = f"merchant{rng.randint(1, 400)}@bank-b"
            victim = rng.choice(people)
            slow = rng.random() < 0.3  # slower, larger probes evade simple velocity rules
            for attempt in range(rng.randint(3 if slow else 8, 10 if slow else 25)):
                emit(
                    start
                    + timedelta(
                        seconds=attempt * rng.randint(300 if slow else 5, 1_800 if slow else 40)
                    ),
                    rng.randint(100, 20_000 if slow else 1_500),
                    victim,
                    payee=merchant_payee,
                    device=device,
                    ip=ip,
                    ip_country=rng.choice(["RU", "NG", "BR", "IN"]),
                    fraud=True,
                    pattern="card_testing",
                    instrument=f"stolen_{day_index}_{attempt}_{rng.randint(0, 10**6)}",
                    method="card",
                    instrument_country="IN",
                )

        # Account takeover: a real instrument, new device and country, several big payments.
        for _ in range(rng.randint(0, max(1, int(3 * fraud_scale)))):
            victim = rng.choice(people)
            start = day + timedelta(seconds=rng.randint(0, 80_000))
            device = f"ato_dev_{day_index}_{rng.randint(0, 9999)}"
            country = rng.choice(["RU", "NG", "VN", "US", "IN"])
            # Some takeovers happen on the victim's own (compromised) phone at home.
            stealthy = rng.random() < 0.35
            for burst in range(rng.randint(1, 6)):
                emit(
                    start + timedelta(minutes=burst * rng.randint(1, 30)),
                    int(victim.typical_minor * rng.uniform(1.5 if stealthy else 3, 15)),
                    victim,
                    payee=f"cashout{rng.randint(1, 60)}@bank-z",
                    device=victim.device_id if stealthy else device,
                    ip=victim.ip_address
                    if stealthy
                    else f"91.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}",
                    ip_country=victim.country if stealthy else country,
                    fraud=True,
                    pattern="account_takeover",
                )

        # Mule network: many compromised payers fan in to a few mule VPAs.
        if rng.random() < 0.6 * fraud_scale:
            mules = [f"mule{day_index}_{m}@bank-z" for m in range(rng.randint(1, 3))]
            for payer in rng.sample(people, k=rng.randint(4, 30)):
                emit(
                    _daytime(rng, day),
                    rng.randint(500_000, 4_900_000),
                    payer,
                    payee=rng.choice(mules),
                    device=payer.device_id
                    if rng.random() < 0.5
                    else f"mule_dev_{rng.randint(0, 99)}",
                    ip=payer.ip_address,
                    ip_country=payer.country,
                    fraud=True,
                    pattern="mule",
                    method="upi",
                    instrument=payer.instrument_id
                    if payer.method == "upi"
                    else f"{payer.device_id}@bank-a",
                )
    events.sort(key=lambda item: (item.event.occurred_at, item.event.payment_id))
    # Label noise: some fraud is never reported, and some legitimate payments are charged back
    # ("friendly fraud"). The pattern keeps the truth for per-pattern evaluation.
    noisy = []
    for item in events:
        if item.fraud and rng.random() < 0.03:
            noisy.append(LabelledEvent(item.event, False, item.pattern))
        elif not item.fraud and rng.random() < 0.0005:
            noisy.append(LabelledEvent(item.event, True, "friendly_fraud"))
        else:
            noisy.append(item)
    return noisy
