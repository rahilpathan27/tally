from concurrent.futures import ThreadPoolExecutor

import pytest
from libs.money import Currency, Money
from services.ledger.model import (
    Account,
    AccountType,
    Direction,
    HoldStatus,
    LedgerBook,
    Posting,
)


def seeded_book() -> LedgerBook:
    book = LedgerBook()
    book.add_account(Account("cash", AccountType.ASSET, Currency.INR, allow_negative=False))
    book.add_account(Account("merchant", AccountType.LIABILITY, Currency.INR))
    book.add_account(Account("funding", AccountType.EQUITY, Currency.INR, allow_negative=True))
    book.post(
        "seed-cash",
        (
            Posting("cash", Direction.DEBIT, Money(100_000, Currency.INR)),
            Posting("funding", Direction.CREDIT, Money(100_000, Currency.INR)),
        ),
    )
    return book


def transfer(amount: int) -> tuple[Posting, ...]:
    return (
        Posting("funding", Direction.DEBIT, Money(amount, Currency.INR)),
        Posting("merchant", Direction.CREDIT, Money(amount, Currency.INR)),
    )


def test_balanced_posting_idempotency_and_hash_chain() -> None:
    book = seeded_book()
    first = book.post("pay-1", transfer(2500))
    replay = book.post("pay-1", transfer(2500))
    assert replay is first
    assert book.balance("cash") == Money(100_000, Currency.INR)
    assert book.balance("merchant") == Money(2500, Currency.INR)
    assert book.balances_net_to_zero(Currency.INR)
    assert book.verify_hash_chain()
    with pytest.raises(ValueError, match="different payload"):
        book.post("pay-1", transfer(2600))


def test_unbalanced_and_negative_postings_do_not_append() -> None:
    book = seeded_book()
    with pytest.raises(ValueError, match="balance per currency"):
        book.post(
            "bad",
            (
                Posting("cash", Direction.DEBIT, Money(20, Currency.INR)),
                Posting("merchant", Direction.CREDIT, Money(19, Currency.INR)),
            ),
        )
    with pytest.raises(ValueError, match="negative"):
        book.post(
            "overdraw",
            (
                Posting("cash", Direction.CREDIT, Money(100_001, Currency.INR)),
                Posting("merchant", Direction.DEBIT, Money(100_001, Currency.INR)),
            ),
        )
    assert len(book.entries) == 1


def test_parallel_replays_create_one_entry() -> None:
    book = seeded_book()
    with ThreadPoolExecutor(max_workers=16) as executor:
        entries = list(executor.map(lambda _: book.post("same-key", transfer(100)), range(100)))
    assert len(book.entries) == 2
    assert len({entry.entry_id for entry in entries}) == 1


def test_concurrent_distinct_posts_preserve_balance_and_non_negative() -> None:
    book = seeded_book()

    def transfer_at(index: int) -> object:
        return book.post(f"payment-{index}", transfer(1000))

    with ThreadPoolExecutor(max_workers=20) as executor:
        list(executor.map(transfer_at, range(100)))
    assert book.balance("funding") == Money(0, Currency.INR)
    assert book.balance("merchant") == Money(100_000, Currency.INR)
    assert book.balances_net_to_zero(Currency.INR)
    assert book.verify_hash_chain()


def test_concurrent_holds_never_reserve_more_than_available() -> None:
    book = seeded_book()
    account_rows = (
        Posting("cash", Direction.CREDIT, Money(10_000, Currency.INR)),
        Posting("funding", Direction.DEBIT, Money(10_000, Currency.INR)),
    )

    def place(index: int) -> str | None:
        try:
            return book.place_hold(f"auth-race-{index}", account_rows).hold_id
        except ValueError as exc:
            if "negative" in str(exc):
                return None
            raise

    with ThreadPoolExecutor(max_workers=20) as executor:
        hold_ids = list(executor.map(place, range(30)))
    accepted = [hold_id for hold_id in hold_ids if hold_id is not None]
    assert len(accepted) == 10
    assert book.available_balance("cash") == Money(0, Currency.INR)
    for index, hold_id in enumerate(accepted):
        if index % 2:
            book.void_hold(hold_id)
        else:
            book.post_hold(hold_id, f"capture-race-{index}")
    assert book.available_balance("cash") == Money(50_000, Currency.INR)
    assert book.balance("cash") == Money(50_000, Currency.INR)
    assert book.balances_net_to_zero(Currency.INR)
    assert book.verify_hash_chain()


def test_pending_credits_do_not_offset_another_holds_reservation() -> None:
    book = seeded_book()
    adverse_hold = book.place_hold(
        "adverse-hold",
        (
            Posting("cash", Direction.CREDIT, Money(80_000, Currency.INR)),
            Posting("funding", Direction.DEBIT, Money(80_000, Currency.INR)),
        ),
    )
    positive_hold = book.place_hold(
        "positive-hold",
        (
            Posting("cash", Direction.DEBIT, Money(90_000, Currency.INR)),
            Posting("funding", Direction.CREDIT, Money(90_000, Currency.INR)),
        ),
    )
    assert book.available_balance("cash") == Money(20_000, Currency.INR)
    with pytest.raises(ValueError, match="negative"):
        book.place_hold(
            "would-exceed-available",
            (
                Posting("cash", Direction.CREDIT, Money(30_000, Currency.INR)),
                Posting("funding", Direction.DEBIT, Money(30_000, Currency.INR)),
            ),
        )
    book.void_hold(adverse_hold.hold_id)
    book.void_hold(positive_hold.hold_id)
    assert book.available_balance("cash") == Money(100_000, Currency.INR)

    netted_hold = book.place_hold(
        "netted-hold",
        (
            Posting("cash", Direction.CREDIT, Money(80_000, Currency.INR)),
            Posting("cash", Direction.DEBIT, Money(30_000, Currency.INR)),
            Posting("funding", Direction.DEBIT, Money(50_000, Currency.INR)),
        ),
    )
    assert book.available_balance("cash") == Money(50_000, Currency.INR)
    book.void_hold(netted_hold.hold_id)


def test_holds_reserve_available_funds_then_post_or_void() -> None:
    book = seeded_book()
    hold_rows = (
        Posting("cash", Direction.CREDIT, Money(70_000, Currency.INR)),
        Posting("funding", Direction.DEBIT, Money(70_000, Currency.INR)),
    )
    hold = book.place_hold("auth-1", hold_rows)
    assert book.balance("cash") == Money(100_000, Currency.INR)
    assert book.available_balance("cash") == Money(30_000, Currency.INR)
    assert book.pending_totals("cash") == (
        Money(0, Currency.INR),
        Money(70_000, Currency.INR),
    )
    cash = book.account_balance("cash")
    assert cash.posted == Money(100_000, Currency.INR)
    assert cash.available == Money(30_000, Currency.INR)
    assert cash.version == 1
    with pytest.raises(ValueError, match="negative"):
        book.place_hold(
            "auth-too-much",
            (
                Posting("cash", Direction.CREDIT, Money(40_000, Currency.INR)),
                Posting("funding", Direction.DEBIT, Money(40_000, Currency.INR)),
            ),
        )
    entry = book.post_hold(hold.hold_id, "capture-1")
    assert book.post_hold(hold.hold_id, "capture-1") is entry
    assert book.holds[0].status is HoldStatus.POSTED
    assert book.balance("cash") == Money(30_000, Currency.INR)
    assert book.available_balance("cash") == Money(30_000, Currency.INR)

    second = book.place_hold(
        "auth-2",
        (
            Posting("cash", Direction.CREDIT, Money(10_000, Currency.INR)),
            Posting("funding", Direction.DEBIT, Money(10_000, Currency.INR)),
        ),
    )
    assert book.void_hold(second.hold_id).status is HoldStatus.VOID
    assert book.available_balance("cash") == Money(30_000, Currency.INR)
    with pytest.raises(ValueError, match="void hold"):
        book.post_hold(second.hold_id, "capture-2")


def test_snapshots_as_of_balance_and_integrity_verifier() -> None:
    book = seeded_book()
    before = book.create_snapshot()
    book.post("pay-after-snapshot", transfer(2500))
    assert book.balance_as_of("merchant", before.created_at) == Money(0, Currency.INR)
    assert book.balance("merchant") == Money(2500, Currency.INR)
    assert book.verify_snapshot(before)
    assert book.verify()


def test_closed_account_and_wrong_currency_are_rejected() -> None:
    book = seeded_book()
    book.add_account(Account("closed", AccountType.LIABILITY, Currency.INR, closed=True))
    with pytest.raises(ValueError, match="closed"):
        book.post(
            "closed-post",
            (
                Posting("funding", Direction.DEBIT, Money(1, Currency.INR)),
                Posting("closed", Direction.CREDIT, Money(1, Currency.INR)),
            ),
        )
    with pytest.raises(ValueError, match="cross-currency"):
        book.post(
            "wrong-currency",
            (
                Posting("funding", Direction.DEBIT, Money(1, Currency.USD)),
                Posting("merchant", Direction.CREDIT, Money(1, Currency.USD)),
            ),
        )
