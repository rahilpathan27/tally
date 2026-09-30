"""Exact money primitives. Amounts are integer minor units throughout."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from enum import StrEnum

MAX_SAFE_INTEGER = 9_007_199_254_740_991


class Currency(StrEnum):
    INR = "INR"
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    JPY = "JPY"
    KWD = "KWD"
    BHD = "BHD"
    CHF = "CHF"
    SGD = "SGD"
    AUD = "AUD"
    CAD = "CAD"


CURRENCY_EXPONENT: dict[Currency, int] = {
    Currency.INR: 2,
    Currency.USD: 2,
    Currency.EUR: 2,
    Currency.GBP: 2,
    Currency.JPY: 0,
    Currency.KWD: 3,
    Currency.BHD: 3,
    Currency.CHF: 2,
    Currency.SGD: 2,
    Currency.AUD: 2,
    Currency.CAD: 2,
}


@dataclass(frozen=True, slots=True)
class Money:
    amount_minor: int
    currency: Currency

    def __post_init__(self) -> None:
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise TypeError("amount_minor must be an integer; floats and booleans are rejected")
        if abs(self.amount_minor) > MAX_SAFE_INTEGER:
            raise ValueError("amount_minor exceeds the documented JSON safe-integer limit")
        if not isinstance(self.currency, Currency):
            raise TypeError("currency must be a supported Currency")

    def _same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValueError("currency mismatch")

    def __add__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.amount_minor + other.amount_minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.amount_minor - other.amount_minor, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount_minor, self.currency)

    def format(self) -> str:
        """Format for display without converting through a binary floating-point value."""
        exponent = CURRENCY_EXPONENT[self.currency]
        sign = "-" if self.amount_minor < 0 else ""
        digits = str(abs(self.amount_minor)).zfill(exponent + 1)
        if exponent == 0:
            return f"{sign}{digits} {self.currency.value}"
        return f"{sign}{digits[:-exponent]}.{digits[-exponent:]} {self.currency.value}"


def round_to_minor(amount_major: Decimal, currency: Currency) -> Money:
    """Convert a decimal major-unit amount once, using documented half-even rounding."""
    if not isinstance(amount_major, Decimal):
        raise TypeError("amount_major must be Decimal")
    if not amount_major.is_finite():
        raise ValueError("amount_major must be finite")
    exponent = CURRENCY_EXPONENT[currency]
    quantum = Decimal(1).scaleb(-exponent)
    try:
        with localcontext() as context:
            context.prec = max(28, len(amount_major.as_tuple().digits) + exponent + 2)
            rounded = amount_major.quantize(quantum, rounding=ROUND_HALF_EVEN)
            minor = int(rounded.scaleb(exponent))
    except InvalidOperation as exc:
        raise ValueError("amount cannot be represented") from exc
    return Money(minor, currency)


def allocate_largest_remainder(total: Money, weights: Iterable[int]) -> tuple[Money, ...]:
    """Allocate a non-negative amount proportionally using non-negative integer weights."""
    weight_list = tuple(weights)
    if not weight_list:
        raise ValueError("at least one weight is required")
    if any(isinstance(w, bool) or not isinstance(w, int) or w < 0 for w in weight_list):
        raise ValueError("weights must be non-negative integers")
    weight_sum = sum(weight_list)
    if weight_sum == 0:
        raise ValueError("at least one weight must be positive")
    if total.amount_minor < 0:
        raise ValueError("allocation total must be non-negative")

    quotients: list[int] = []
    remainders: list[int] = []
    for weight in weight_list:
        quotient, remainder = divmod(total.amount_minor * weight, weight_sum)
        quotients.append(quotient)
        remainders.append(remainder)
    left = total.amount_minor - sum(quotients)
    order = sorted(range(len(weight_list)), key=lambda i: (-remainders[i], i))
    for index in order[:left]:
        quotients[index] += 1
    return tuple(Money(amount, total.currency) for amount in quotients)
