from chaos.chaos_sim import run_scenarios


def test_seeded_failure_harness_covers_failure_classes_and_is_reproducible() -> None:
    first = run_scenarios(500, 731_002)
    second = run_scenarios(500, 731_002)

    assert first == second
    assert first.scenarios == 500
    assert first.dropped_before_commit > 0
    assert first.lost_ack_replayed > 0
    assert first.duplicate_delivery > 0
    assert first.hold_voided > 0
    assert first.hold_captured > 0
