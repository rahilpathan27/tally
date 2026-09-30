from libs.money import Currency, Money
from services.ledger.invariants import check_ledger_invariants
from services.ledger.model import Account, AccountType, Direction, LedgerBook, Posting


def test_invariant_checker_accepts_balanced_posting_and_captured_hold() -> None:
    book = LedgerBook()
    book.add_account(Account("cash", AccountType.ASSET, Currency.INR))
    book.add_account(Account("clearing", AccountType.EQUITY, Currency.INR, allow_negative=True))
    book.post(
        "seed",
        (
            Posting("cash", Direction.DEBIT, Money(1_000, Currency.INR)),
            Posting("clearing", Direction.CREDIT, Money(1_000, Currency.INR)),
        ),
    )
    hold = book.place_hold(
        "auth",
        (
            Posting("cash", Direction.CREDIT, Money(250, Currency.INR)),
            Posting("clearing", Direction.DEBIT, Money(250, Currency.INR)),
        ),
    )
    book.post_hold(hold.hold_id, "capture")

    assert check_ledger_invariants(book, ("cash",)) == []
