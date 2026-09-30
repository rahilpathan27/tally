from decimal import Decimal

import pytest
from libs.money import Currency, Money, allocate_largest_remainder, round_to_minor


def test_money_rejects_non_integer_minor_units() -> None:
    with pytest.raises(TypeError):
        Money(1.25, Currency.INR)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money(True, Currency.INR)


def test_currency_exponents_and_formatting() -> None:
    assert Money(1234, Currency.INR).format() == "12.34 INR"
    assert Money(1234, Currency.JPY).format() == "1234 JPY"
    assert Money(1234, Currency.KWD).format() == "1.234 KWD"


def test_half_even_rounding_is_centralized() -> None:
    assert round_to_minor(Decimal("1.225"), Currency.INR) == Money(122, Currency.INR)
    assert round_to_minor(Decimal("1.235"), Currency.INR) == Money(124, Currency.INR)


@pytest.mark.parametrize("weights", [(1, 1, 1), (7, 2), (0, 3, 5), (19, 1, 4, 2)])
@pytest.mark.parametrize("amount", [0, 1, 2, 101, 9999])
def test_largest_remainder_conserves_minor_units(amount: int, weights: tuple[int, ...]) -> None:
    total = Money(amount, Currency.INR)
    parts = allocate_largest_remainder(total, weights)
    assert sum(part.amount_minor for part in parts) == amount
    assert all(part.currency is Currency.INR and part.amount_minor >= 0 for part in parts)


def test_allocation_is_deterministic_for_tied_remainders() -> None:
    assert allocate_largest_remainder(Money(2, Currency.INR), (1, 1, 1)) == (
        Money(1, Currency.INR),
        Money(1, Currency.INR),
        Money(0, Currency.INR),
    )


def test_money_operations_reject_currency_mismatch() -> None:
    with pytest.raises(ValueError, match="currency mismatch"):
        _ = Money(1, Currency.INR) + Money(1, Currency.USD)


def test_subtraction_and_negation_preserve_currency_and_exact_units() -> None:
    amount = Money(9, Currency.INR) - Money(12, Currency.INR)
    assert amount == Money(-3, Currency.INR)
    assert -amount == Money(3, Currency.INR)
    with pytest.raises(ValueError, match="currency mismatch"):
        _ = Money(1, Currency.INR) - Money(1, Currency.USD)


def test_money_bounds_and_decimal_input_validation() -> None:
    from libs.money import MAX_SAFE_INTEGER

    with pytest.raises(ValueError, match="safe-integer"):
        Money(MAX_SAFE_INTEGER + 1, Currency.INR)
    with pytest.raises(TypeError, match="Decimal"):
        round_to_minor(1.5, Currency.INR)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite"):
        round_to_minor(Decimal("NaN"), Currency.INR)
    with pytest.raises(ValueError, match="finite"):
        round_to_minor(Decimal("Infinity"), Currency.INR)


def test_allocation_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="at least one weight"):
        allocate_largest_remainder(Money(1, Currency.INR), ())
    with pytest.raises(ValueError, match="non-negative integers"):
        allocate_largest_remainder(Money(1, Currency.INR), (1, -1))
    with pytest.raises(ValueError, match="positive"):
        allocate_largest_remainder(Money(1, Currency.INR), (0, 0))
    with pytest.raises(ValueError, match="non-negative"):
        allocate_largest_remainder(Money(-1, Currency.INR), (1, 1))
