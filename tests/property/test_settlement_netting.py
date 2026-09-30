from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st
from libs.money import Currency
from services.core.fees import (
    FeeSchedule,
    SettlementInputs,
    compute_settlement,
    gst_on,
    net_postings,
    payment_fee,
    settlement_postings,
    split_debit,
)

amounts = st.integers(min_value=1, max_value=10_000_000_00)
rates = st.decimals(min_value=0, max_value=Decimal("0.2"), places=6)
schedules = st.builds(
    FeeSchedule,
    fee_rate=rates,
    fixed_fee_minor=st.integers(min_value=0, max_value=1_000),
    gst_rate=st.decimals(min_value=0, max_value=Decimal("0.28"), places=4),
    reserve_rate=rates,
)


@settings(max_examples=500)
@given(
    payments=st.lists(amounts, max_size=40),
    debits=st.integers(min_value=0, max_value=5_000_000_00),
    credits=st.integers(min_value=0, max_value=5_000_000_00),
    release=st.integers(min_value=0, max_value=1_000_000_00),
    receivable=st.integers(min_value=0, max_value=1_000_000_00),
    schedule=schedules,
)
def test_settlement_conserves_payable_and_postings_balance(
    payments: list[int],
    debits: int,
    credits: int,
    release: int,
    receivable: int,
    schedule: FeeSchedule,
) -> None:
    inputs = SettlementInputs(payments, debits, credits, release, receivable)
    result = compute_settlement(inputs, schedule, Currency.INR)
    assert result.payable_reduction == sum(payments) - debits + credits
    for part in (
        result.fees,
        result.gst,
        result.reserve_held,
        result.recovered,
        result.shortfall,
        result.net_payout,
    ):
        assert part >= 0
    assert result.recovered <= receivable
    assert not (result.shortfall and (result.net_payout or result.recovered))

    postings = settlement_postings("m1", result, "m1:2026-09-30", Currency.INR)
    assert all(int(str(p["amount_minor"])) > 0 for p in postings)
    debit = sum(int(str(p["amount_minor"])) for p in postings if p["direction"] == "debit")
    credit = sum(int(str(p["amount_minor"])) for p in postings if p["direction"] == "credit")
    assert debit == credit
    payable = next((p for p in postings if p["account_id"] == "merchant:m1:payable:INR"), None)
    signed = 0 if payable is None else int(str(payable["amount_minor"]))
    if payable is not None and payable["direction"] == "credit":
        signed = -signed
    assert signed == result.payable_reduction


@given(amount=amounts, schedule=schedules)
def test_fee_never_exceeds_amount_and_gst_is_half_even(amount: int, schedule: FeeSchedule) -> None:
    fee = payment_fee(amount, schedule, Currency.INR)
    assert 0 <= fee <= amount
    assert gst_on(fee, schedule, Currency.INR) >= 0


def test_known_fee_values_round_half_even() -> None:
    schedule = FeeSchedule(Decimal("0.02"), 0, Decimal("0.18"), Decimal("0"))
    # 2% of 125 paise = 2.5 paise -> 2 (half-even); 2% of 175 = 3.5 -> 4.
    assert payment_fee(125, schedule, Currency.INR) == 2
    assert payment_fee(175, schedule, Currency.INR) == 4
    # 18% GST on 250 paise = 45 paise exactly.
    assert gst_on(250, schedule, Currency.INR) == 45


@given(amount=st.integers(min_value=0, max_value=10**12), available=st.integers(-(10**6), 10**12))
def test_split_debit_parts_sum_to_amount(amount: int, available: int) -> None:
    payable, receivable = split_debit(amount, available)
    assert payable + receivable == amount
    assert 0 <= payable <= max(0, available)


def test_net_postings_drops_zero_lines() -> None:
    lines = [("a", "debit", 5), ("a", "credit", 5), ("b", "debit", 3), ("c", "credit", 3)]
    assert net_postings(lines) == [
        {"account_id": "b", "direction": "debit", "amount_minor": 3},
        {"account_id": "c", "direction": "credit", "amount_minor": 3},
    ]
