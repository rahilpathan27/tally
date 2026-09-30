"""Exact fee, GST, reserve and settlement netting arithmetic.

Money is integer minor units. Rates are ``Decimal`` and every conversion back to minor units goes
through ``libs.money.round_to_minor`` (half-even), the only rounding point. Nothing here touches
binary floating point; ``libs.money.float_ban`` scans this module.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from libs.money import (
    CURRENCY_EXPONENT,
    Currency,
    Money,
    allocate_largest_remainder,
    round_to_minor,
)

FEE_SHARDS = 8


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Per-merchant pricing. ``fee_rate`` 0.02 means 2% of the payment amount."""

    fee_rate: Decimal
    fixed_fee_minor: int
    gst_rate: Decimal
    reserve_rate: Decimal

    def __post_init__(self) -> None:
        for name in ("fee_rate", "gst_rate", "reserve_rate"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise TypeError(f"{name} must be a finite Decimal")
            if not Decimal(0) <= value <= Decimal(1):
                raise ValueError(f"{name} must be between 0 and 1")
        if isinstance(self.fixed_fee_minor, bool) or not isinstance(self.fixed_fee_minor, int):
            raise TypeError("fixed_fee_minor must be an integer")
        if self.fixed_fee_minor < 0:
            raise ValueError("fixed_fee_minor must be non-negative")


def payment_fee(amount_minor: int, schedule: FeeSchedule, currency: Currency) -> int:
    """Per-payment fee: rate × amount (half-even) plus fixed fee, capped at the amount."""
    variable = round_to_minor(
        Decimal(amount_minor) * schedule.fee_rate / _scale(currency), currency
    ).amount_minor
    return min(amount_minor, variable + schedule.fixed_fee_minor)


def gst_on(fee_minor: int, schedule: FeeSchedule, currency: Currency) -> int:
    return round_to_minor(
        Decimal(fee_minor) * schedule.gst_rate / _scale(currency), currency
    ).amount_minor


def reserve_on(gross_minor: int, schedule: FeeSchedule, currency: Currency) -> int:
    return round_to_minor(
        Decimal(gross_minor) * schedule.reserve_rate / _scale(currency), currency
    ).amount_minor


def _scale(currency: Currency) -> Decimal:
    return Decimal(10) ** CURRENCY_EXPONENT[currency]


@dataclass(frozen=True, slots=True)
class SettlementInputs:
    payment_amounts: Sequence[int]
    """Captured/succeeded sales in the settlement window (credit merchant payable)."""
    payable_debits: int
    """Refund and dispute debits already taken from payable and not yet settled."""
    payable_credits: int
    """Refund cancellations, dispute wins and payout returns credited back to payable."""
    reserve_release: int
    receivable_balance: int
    """What the merchant owes the platform (refund/chargeback shortfalls) before this run."""


@dataclass(frozen=True, slots=True)
class SettlementBreakdown:
    gross: int
    payable_debits: int
    payable_credits: int
    fees: int
    gst: int
    reserve_held: int
    reserve_released: int
    recovered: int
    shortfall: int
    net_payout: int

    @property
    def payable_reduction(self) -> int:
        """Net amount this settlement removes from merchant payable."""
        return (
            self.fees
            + self.gst
            + self.reserve_held
            + self.recovered
            + self.net_payout
            - self.reserve_released
            - self.shortfall
        )


def compute_settlement(
    inputs: SettlementInputs, schedule: FeeSchedule, currency: Currency
) -> SettlementBreakdown:
    """Net a settlement window so payable drops by exactly its in-scope contribution.

    ``X = gross - debits + credits + release - fees - gst - reserve``. A positive ``X`` first
    recovers any receivable, the rest is paid out; a negative ``X`` becomes a new receivable
    (shortfall) so merchant payable never goes negative.
    """
    for value in (
        inputs.payable_debits,
        inputs.payable_credits,
        inputs.reserve_release,
        inputs.receivable_balance,
        *inputs.payment_amounts,
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("settlement inputs must be non-negative integers")
    gross = sum(inputs.payment_amounts)
    fees = sum(payment_fee(amount, schedule, currency) for amount in inputs.payment_amounts)
    gst = gst_on(fees, schedule, currency)
    reserve = reserve_on(gross, schedule, currency)
    available = (
        gross
        - inputs.payable_debits
        + inputs.payable_credits
        + inputs.reserve_release
        - fees
        - gst
        - reserve
    )
    recovered = min(inputs.receivable_balance, max(0, available))
    net = max(0, available - recovered)
    shortfall = max(0, -available)
    breakdown = SettlementBreakdown(
        gross=gross,
        payable_debits=inputs.payable_debits,
        payable_credits=inputs.payable_credits,
        fees=fees,
        gst=gst,
        reserve_held=reserve,
        reserve_released=inputs.reserve_release,
        recovered=recovered,
        shortfall=shortfall,
        net_payout=net,
    )
    expected = gross - inputs.payable_debits + inputs.payable_credits
    if breakdown.payable_reduction != expected:
        raise AssertionError("settlement netting does not conserve merchant payable")
    return breakdown


def fee_shard(settlement_key: str) -> int:
    """Deterministic hot-account shard, stable across retries of the same settlement."""
    return int.from_bytes(hashlib.sha256(settlement_key.encode()).digest()[:4], "big") % FEE_SHARDS


def settlement_postings(
    merchant_id: str,
    breakdown: SettlementBreakdown,
    settlement_key: str,
    currency: Currency,
) -> list[dict[str, object]]:
    """Balanced ledger postings for a settlement, netted per account and direction."""
    c = currency.value
    payable = f"merchant:{merchant_id}:payable:{c}"
    reserve = f"merchant:{merchant_id}:reserve:{c}"
    receivable = f"merchant:{merchant_id}:receivable:{c}"
    in_transit = f"merchant:{merchant_id}:payout_in_transit:{c}"
    fee_income = f"platform:fee_income:{c}:shard:{fee_shard(settlement_key)}"
    tax = f"platform:tax_payable:{c}"
    lines: list[tuple[str, str, int]] = [
        (payable, "debit", breakdown.fees + breakdown.gst),
        (fee_income, "credit", breakdown.fees),
        (tax, "credit", breakdown.gst),
        (payable, "debit", breakdown.reserve_held),
        (reserve, "credit", breakdown.reserve_held),
        (reserve, "debit", breakdown.reserve_released),
        (payable, "credit", breakdown.reserve_released),
        (payable, "debit", breakdown.recovered),
        (receivable, "credit", breakdown.recovered),
        (receivable, "debit", breakdown.shortfall),
        (payable, "credit", breakdown.shortfall),
        (payable, "debit", breakdown.net_payout),
        (in_transit, "credit", breakdown.net_payout),
    ]
    return net_postings(lines)


def net_postings(lines: Iterable[tuple[str, str, int]]) -> list[dict[str, object]]:
    """Collapse lines to one net posting per account, dropping zero amounts."""
    totals: dict[str, int] = {}
    for account, direction, amount in lines:
        if amount < 0:
            raise ValueError("posting amounts must be non-negative")
        signed = amount if direction == "debit" else -amount
        totals[account] = totals.get(account, 0) + signed
    postings: list[dict[str, object]] = [
        {
            "account_id": account,
            "direction": "debit" if value > 0 else "credit",
            "amount_minor": abs(value),
        }
        for account, value in sorted(totals.items())
        if value != 0
    ]
    debit_total = sum(int(str(p["amount_minor"])) for p in postings if p["direction"] == "debit")
    credit_total = sum(int(str(p["amount_minor"])) for p in postings if p["direction"] == "credit")
    if debit_total != credit_total:
        raise AssertionError("netted postings are unbalanced")
    return postings


def split_debit(amount_minor: int, payable_available: int) -> tuple[int, int]:
    """Take what merchant payable can cover; the remainder becomes a merchant receivable."""
    if amount_minor < 0:
        raise ValueError("amount must be non-negative")
    from_payable = max(0, min(amount_minor, payable_available))
    return from_payable, amount_minor - from_payable


def apportion(total: int, weights: Sequence[int], currency: Currency) -> tuple[int, ...]:
    """Split ``total`` by integer weights so the parts always sum exactly to ``total``."""
    return tuple(
        part.amount_minor for part in allocate_largest_remainder(Money(total, currency), weights)
    )


def entry_lines_total(postings: Iterable[dict[str, object]]) -> int:
    """Total debit side of an entry (equal to its credit side when balanced)."""
    return sum(int(str(p["amount_minor"])) for p in postings if p["direction"] == "debit")
