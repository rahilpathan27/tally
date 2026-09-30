"""Table-driven payment intent state transitions."""

from __future__ import annotations

from enum import StrEnum


class PaymentState(StrEnum):
    CREATED = "created"
    RISK_REVIEW = "risk_review"
    AUTHORIZING = "authorizing"
    AUTHORIZED = "authorized"
    CAPTURING = "capturing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PENDING_UNKNOWN = "pending_unknown"
    REVERSAL_PENDING = "reversal_pending"
    REVERSED = "reversed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


ALLOWED_TRANSITIONS: dict[PaymentState, frozenset[PaymentState]] = {
    PaymentState.CREATED: frozenset(
        {
            PaymentState.RISK_REVIEW,
            PaymentState.AUTHORIZING,
            PaymentState.FAILED,  # blocked by risk before any external call
            PaymentState.CANCELLED,
            PaymentState.EXPIRED,
        }
    ),
    PaymentState.RISK_REVIEW: frozenset(
        {PaymentState.AUTHORIZING, PaymentState.FAILED, PaymentState.CANCELLED}
    ),
    PaymentState.AUTHORIZING: frozenset(
        {
            PaymentState.AUTHORIZED,
            PaymentState.SUCCEEDED,
            PaymentState.FAILED,
            PaymentState.PENDING_UNKNOWN,
        }
    ),
    PaymentState.AUTHORIZED: frozenset(
        {PaymentState.CAPTURING, PaymentState.CANCELLED, PaymentState.EXPIRED}
    ),
    PaymentState.CAPTURING: frozenset(
        {PaymentState.SUCCEEDED, PaymentState.FAILED, PaymentState.PENDING_UNKNOWN}
    ),
    PaymentState.PENDING_UNKNOWN: frozenset(
        {
            PaymentState.AUTHORIZED,
            PaymentState.SUCCEEDED,
            PaymentState.FAILED,
            PaymentState.REVERSAL_PENDING,
        }
    ),
    PaymentState.REVERSAL_PENDING: frozenset({PaymentState.REVERSED, PaymentState.SUCCEEDED}),
    PaymentState.SUCCEEDED: frozenset(),
    PaymentState.FAILED: frozenset(),
    PaymentState.REVERSED: frozenset(),
    PaymentState.CANCELLED: frozenset(),
    PaymentState.EXPIRED: frozenset(),
}


def transition_allowed(current: PaymentState, target: PaymentState) -> bool:
    return target in ALLOWED_TRANSITIONS[current]
