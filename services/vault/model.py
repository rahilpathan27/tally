"""PAN validation and non-sensitive vault response metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

PUBLISHED_TEST_PANS = frozenset({"4242424242424242", "5555555555554444", "378282246310005"})


def luhn_valid(pan: str) -> bool:
    if not pan.isascii() or not pan.isdigit() or not 12 <= len(pan) <= 19:
        return False
    checksum = 0
    for offset, character in enumerate(reversed(pan)):
        digit = ord(character) - ord("0")
        if offset % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def validate_test_card(
    pan: str,
    expiry_month: int,
    expiry_year: int,
    *,
    allowed_test_pans: frozenset[str],
    today: date,
) -> None:
    if pan not in allowed_test_pans or not luhn_valid(pan):
        raise ValueError("card number is not an allowed test card")
    if not 1 <= expiry_month <= 12 or expiry_year < 2000:
        raise ValueError("card expiry is invalid")
    if (expiry_year, expiry_month) < (today.year, today.month):
        raise ValueError("card has expired")


@dataclass(frozen=True, slots=True)
class TokenMetadata:
    token: str
    last4: str
    expiry_month: int
    expiry_year: int
