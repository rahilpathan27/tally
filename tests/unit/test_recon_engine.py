from datetime import UTC, datetime, timedelta

from services.recon.engine import (
    BankRecord,
    BreakType,
    Kind,
    LedgerRecord,
    ReconResult,
    SwitchRecord,
    reconcile,
)

T = datetime(2026, 9, 29, 6, 0, tzinfo=UTC)
END = datetime(2026, 9, 29, 18, 30, tzinfo=UTC)


def _run(
    ledger: list[LedgerRecord],
    switch: list[SwitchRecord],
    bank: list[BankRecord],
    next_day: list[BankRecord] | None = None,
) -> ReconResult:
    return reconcile(
        ledger,
        switch,
        bank,
        business_date="2026-09-29",
        source="s",
        day_end=END,
        next_day_bank=next_day,
    )


def _ok(ref: str, amount: int = 1_000) -> tuple[LedgerRecord, SwitchRecord, BankRecord]:
    return (
        LedgerRecord(ref, Kind.UPI_TRANSFER, amount, "in", T, "e"),
        SwitchRecord(ref, Kind.UPI_TRANSFER, amount, "success", T, "p@a"),
        BankRecord(1, ref, f"UTR-{ref}", Kind.UPI_TRANSFER, amount, "success", T, "p@a"),
    )


def test_clean_three_way_match_has_no_breaks() -> None:
    ledger, switch, bank = _ok("r1")
    result = _run([ledger], [switch], [bank])
    assert not result.breaks and result.matches[0].match_type == "exact"


def test_each_break_type_is_classified() -> None:
    l1, s1, _ = _ok("gone")
    l2, s2, b2 = _ok("amt")
    l3, s3, b3 = _ok("dup")
    l4, s4, b4 = _ok("late")
    bank = [
        BankRecord(1, "amt", b2.bank_reference, Kind.UPI_TRANSFER, 1_013, "success", T),
        b3,
        BankRecord(3, "dup", b3.bank_reference, Kind.UPI_TRANSFER, 1_000, "success", T),
        BankRecord(4, "stranger", "UTR-x", Kind.UPI_TRANSFER, 5, "success", T),
        BankRecord(5, None, "UTR-y", Kind.UPI_TRANSFER, 77, "success", T, "who@z"),
        b4,
    ]
    s4_late = SwitchRecord("late", Kind.UPI_TRANSFER, 1_000, "late_success", T)
    l4_late = LedgerRecord("late", Kind.UPI_TRANSFER, 1_000, "in", T, "e", suspense=True)
    result = _run([l1, l2, l3, l4_late], [s1, s2, s3, s4_late], bank)
    kinds = {(b.break_type, b.reference or b.bank_reference) for b in result.breaks}
    assert kinds == {
        (BreakType.MISSING_AT_BANK, "gone"),
        (BreakType.AMOUNT_MISMATCH, "amt"),
        (BreakType.DUPLICATE, "dup"),
        (BreakType.MISSING_INTERNALLY, "stranger"),
        (BreakType.UNKNOWN, "UTR-y"),
        (BreakType.STATUS_MISMATCH, "late"),
    }


def test_fuzzy_group_and_fee_and_timing() -> None:
    l1, s1, b1 = _ok("fz", 4_321)
    fuzzy = BankRecord(
        1, None, "UTR-f", Kind.UPI_TRANSFER, 4_321, "success", T + timedelta(minutes=3), "p@a"
    )
    refunds = [SwitchRecord(f"rf{i}", Kind.REFUND, 100 * (i + 1), "success", T) for i in range(3)]
    batch = BankRecord(2, None, "BATCH", Kind.REFUND_BATCH, 600, "success", T, batch_id="b")
    payout = SwitchRecord("po", Kind.PAYOUT, 50_000, "success", T)
    payout_l = LedgerRecord("po", Kind.PAYOUT, 50_000, "out", T, "e")
    payout_b = BankRecord(3, "po", "UTR-po", Kind.PAYOUT, 49_410, "success", T)
    near = SwitchRecord("near", Kind.UPI_TRANSFER, 10, "success", END - timedelta(minutes=5))
    near_l = LedgerRecord("near", Kind.UPI_TRANSFER, 10, "in", END - timedelta(minutes=5), "e")
    ledger = [l1, payout_l, near_l] + [
        LedgerRecord(r.reference, Kind.REFUND, r.amount_minor, "out", T, "e") for r in refunds
    ]
    result = _run(ledger, [s1, payout, near, *refunds], [fuzzy, batch, payout_b])
    types = {(b.break_type, b.reference) for b in result.breaks}
    assert types == {(BreakType.FEE_TAX_MISMATCH, "po"), (BreakType.TIMING_DIFFERENCE, "near")}
    assert {m.match_type for m in result.matches} >= {"fuzzy", "group"}
    # With the next day's statement available, the timing break resolves automatically.
    nxt = [BankRecord(1, "near", "UTR-n", Kind.UPI_TRANSFER, 10, "success", END)]
    rerun = _run(ledger, [s1, payout, near, *refunds], [fuzzy, batch, payout_b], nxt)
    timing = [b for b in rerun.breaks if b.break_type == BreakType.TIMING_DIFFERENCE]
    assert timing[0].status == "auto_resolved"


def test_break_ids_are_deterministic_across_runs() -> None:
    l1, s1, _ = _ok("gone")
    first = _run([l1], [s1], [])
    second = _run([l1], [s1], [])
    assert [b.break_id for b in first.breaks] == [b.break_id for b in second.breaks]
