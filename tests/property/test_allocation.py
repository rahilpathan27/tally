from hypothesis import given
from hypothesis import strategies as st
from libs.money import Currency, Money, allocate_largest_remainder
from services.ledger.model import Account, AccountType, Direction, LedgerBook, Posting


@given(
    amount=st.integers(min_value=0, max_value=10**12),
    weights=st.lists(st.integers(min_value=0, max_value=10**6), min_size=1, max_size=30),
)
def test_allocation_always_conserves_exactly(amount: int, weights: list[int]) -> None:
    if not any(weights):
        weights[0] = 1
    parts = allocate_largest_remainder(Money(amount, Currency.INR), weights)
    assert sum(part.amount_minor for part in parts) == amount
    assert len(parts) == len(weights)


@given(amounts=st.lists(st.integers(min_value=1, max_value=1000), max_size=100))
def test_random_balanced_ledger_sequences_preserve_invariants(amounts: list[int]) -> None:
    total = sum(amounts)
    book = LedgerBook()
    book.add_account(Account("cash", AccountType.ASSET, Currency.INR))
    book.add_account(Account("merchant", AccountType.LIABILITY, Currency.INR))
    book.add_account(Account("equity", AccountType.EQUITY, Currency.INR, allow_negative=True))
    if total:
        book.post(
            "seed",
            (
                Posting("cash", Direction.DEBIT, Money(total, Currency.INR)),
                Posting("equity", Direction.CREDIT, Money(total, Currency.INR)),
            ),
        )
    for index, amount in enumerate(amounts):
        book.post(
            f"payment-{index}",
            (
                Posting("equity", Direction.DEBIT, Money(amount, Currency.INR)),
                Posting("merchant", Direction.CREDIT, Money(amount, Currency.INR)),
            ),
        )
    assert book.balances_net_to_zero(Currency.INR)
    assert book.verify_hash_chain()
    assert book.balance("cash") == Money(total, Currency.INR)
    assert book.balance("equity") == Money(0, Currency.INR)
    assert book.balance("merchant") == Money(total, Currency.INR)
