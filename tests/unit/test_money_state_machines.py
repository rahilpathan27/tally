from services.core.disputes import DISPUTE_TRANSITIONS, DisputeState
from services.core.refunds import REFUND_TRANSITIONS, RefundState
from services.core.webhooks import event_matches


def test_refund_terminal_states_have_no_exits() -> None:
    for state in (RefundState.SUCCEEDED, RefundState.CANCELLED, RefundState.FAILED):
        assert REFUND_TRANSITIONS[state] == frozenset()
    assert RefundState.SUCCEEDED not in REFUND_TRANSITIONS[RefundState.PENDING]
    assert RefundState.CANCELLED not in REFUND_TRANSITIONS[RefundState.PROCESSING]
    assert set(REFUND_TRANSITIONS) == set(RefundState)


def test_dispute_lifecycle_is_forward_only() -> None:
    assert DISPUTE_TRANSITIONS[DisputeState.WON] == frozenset()
    assert DISPUTE_TRANSITIONS[DisputeState.LOST] == frozenset()
    assert DisputeState.NEEDS_RESPONSE not in DISPUTE_TRANSITIONS[DisputeState.UNDER_REVIEW]
    assert set(DISPUTE_TRANSITIONS) == set(DisputeState)


def test_webhook_event_patterns() -> None:
    assert event_matches(["refund.*"], "refund.succeeded")
    assert event_matches(["*"], "payout.paid")
    assert not event_matches(["refund.*"], "payment_intent.succeeded")
    assert not event_matches(["refund.succeeded"], "refund.failed")
