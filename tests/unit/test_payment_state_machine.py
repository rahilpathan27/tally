from __future__ import annotations

import pytest
from services.core.state_machine import PaymentState, transition_allowed


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (PaymentState.CREATED, PaymentState.AUTHORIZING),
        (PaymentState.CREATED, PaymentState.CANCELLED),
        (PaymentState.AUTHORIZING, PaymentState.AUTHORIZED),
        (PaymentState.AUTHORIZING, PaymentState.SUCCEEDED),
        (PaymentState.AUTHORIZED, PaymentState.CAPTURING),
        (PaymentState.CAPTURING, PaymentState.SUCCEEDED),
    ],
)
def test_allowed_payment_transitions(current: PaymentState, target: PaymentState) -> None:
    assert transition_allowed(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (PaymentState.SUCCEEDED, PaymentState.CAPTURING),
        (PaymentState.FAILED, PaymentState.AUTHORIZED),
        (PaymentState.CANCELLED, PaymentState.AUTHORIZING),
        (PaymentState.CREATED, PaymentState.SUCCEEDED),
    ],
)
def test_illegal_payment_transitions(current: PaymentState, target: PaymentState) -> None:
    assert not transition_allowed(current, target)
