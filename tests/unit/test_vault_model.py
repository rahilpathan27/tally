from __future__ import annotations

from datetime import date

import pytest
from services.vault.model import PUBLISHED_TEST_PANS, luhn_valid, validate_test_card

TEST_PAN = "4242424242424242"


def test_published_test_pan_set_is_explicit() -> None:
    assert PUBLISHED_TEST_PANS == {
        "4242424242424242",
        "5555555555554444",
        "378282246310005",
    }


@pytest.mark.parametrize("pan", [TEST_PAN, "5555555555554444", "378282246310005"])
def test_published_test_pan_luhn_checks(pan: str) -> None:
    assert luhn_valid(pan)


def test_luhn_rejects_invalid_length_letters_and_check_digit() -> None:
    assert not luhn_valid("4242424242424241")
    assert not luhn_valid("12345678901")
    assert not luhn_valid("424242424242424x")


def test_test_card_validation_requires_allowlist_and_future_expiry() -> None:
    validate_test_card(
        TEST_PAN,
        12,
        2035,
        allowed_test_pans=frozenset({TEST_PAN}),
        today=date(2026, 9, 30),
    )
    with pytest.raises(ValueError, match="allowed test card"):
        validate_test_card(
            TEST_PAN,
            12,
            2035,
            allowed_test_pans=frozenset(),
            today=date(2026, 9, 30),
        )
    with pytest.raises(ValueError, match="expired"):
        validate_test_card(
            TEST_PAN,
            8,
            2026,
            allowed_test_pans=frozenset({TEST_PAN}),
            today=date(2026, 9, 30),
        )
