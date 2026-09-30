import pytest
from pydantic import ValidationError
from services.ledger.api import PlaceHoldRequest, PostEntryRequest


def test_post_request_accepts_minor_unit_integer_and_direction_strings() -> None:
    request = PostEntryRequest.model_validate_json(
        '{"idempotency_key":"payment-1","postings":['
        '{"account_id":"cash","direction":"debit","amount_minor":12},'
        '{"account_id":"payable","direction":"credit","amount_minor":12}]}'
    )
    assert request.postings[0].amount_minor == 12
    assert request.postings[0].direction.value == "debit"


@pytest.mark.parametrize("amount", ['"12"', "12.0", "true", "-1", "0"])
def test_post_request_rejects_non_positive_or_non_integer_minor_units(amount: str) -> None:
    payload = (
        '{"idempotency_key":"payment-1","postings":['
        '{"account_id":"cash","direction":"debit","amount_minor":'
        f"{amount}" + '},{"account_id":"payable","direction":"credit","amount_minor":12}]}'
    )
    with pytest.raises(ValidationError):
        PostEntryRequest.model_validate_json(payload)


def test_hold_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        PlaceHoldRequest.model_validate(
            {
                "idempotency_key": "auth-1",
                "postings": [
                    {"account_id": "cash", "direction": "debit", "amount_minor": 12},
                    {"account_id": "payable", "direction": "credit", "amount_minor": 12},
                ],
                "amount": 0.12,
            }
        )
